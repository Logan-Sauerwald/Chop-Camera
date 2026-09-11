#!/usr/bin/env python3
"""
wall.py -- chopcam aggregator: live preview wall and node status.

Runs on the aggregator Pi 5, driving the monitor. It does three things:

  * serves a grid of live tiles, one per capture node
  * polls every node's /healthz and shows which are actually recording
  * lists the clips that have landed in INCOMING_DIR
  * plays them back in slow motion, with the trigger instant marked
  * keeps the ones somebody marks, where the purge cannot reach them
  * records every trigger in a chop log that outlives the footage

The status polling is done HERE rather than in the browser on purpose. The
nodes' HTTP servers send no CORS headers, so page JavaScript cannot read their
/healthz directly; proxying it server-side and serving the aggregate from our
own origin sidesteps that entirely. MJPEG <img> tags are not CORS-restricted,
so the video tiles do come straight from the nodes -- the aggregator never
touches video, which is what keeps it cheap.

Clips arrive by scp from the nodes (postprocess.sh ships them). This process
does not receive them; it only reports what is on disk -- with one exception,
"Keep this clip", which moves a file into INCOMING_DIR/keep/ so purge.py can
never delete it.

HTTP
  /            the wall, the player and the chop log (one page)
  /log         the same page with the chop log already open
  /status      aggregate node status, polled by the page
  /clips       delivered clips, newest first; ?node= filters to one camera
  /choplog     every trigger any node reported, with what became of it
  /clip/NAME   a clip; ?slowmo=1 or ?download=1 to download it
  /keep/NAME   POST: move a clip where the purge cannot delete it
  /unkeep/NAME POST: move it back
  /healthz     aggregator status; 200 healthy, 503 degraded

USAGE
  wall.py                     run the wall server
  wall.py --check-config      validate the config and exit
  wall.py --config PATH       use a specific config file
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The node half of the repo owns the config format; share the reader rather
# than growing a second one that can drift.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chopcam_config import load_config, parse_config_value   # noqa: E402,F401

CONFIG_SEARCH = [
    os.environ.get("CHOPCAM_AGG_CONF"),
    "/etc/chopcam-agg.conf",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "chopcam-agg.conf"),
]


def _argv_value(flag, default=None):
    if flag not in sys.argv:
        return default
    idx = sys.argv.index(flag) + 1
    if idx >= len(sys.argv) or sys.argv[idx].startswith("--"):
        return default
    return sys.argv[idx]


C = load_config(_argv_value("--config"), search=CONFIG_SEARCH)


def _s(key, default=""):
    return C.get(key, default)


def _i(key, default=0):
    try:
        return int(str(C.get(key, default)).strip())
    except (TypeError, ValueError):
        return int(default)


SITE            = _s("SITE", "")
NODE_PORT       = _i("NODE_PORT", 8080)
POLL_SECONDS    = max(1, _i("POLL_SECONDS", 5))
WALL_PORT       = _i("WALL_PORT", 8090)
WALL_HOST       = _s("WALL_HOST", "0.0.0.0")
INCOMING_DIR    = _s("INCOMING_DIR", "/srv/chopcam/incoming")
CLIP_LIST_LIMIT = max(1, _i("CLIP_LIST_LIMIT", 50))
RETENTION_DAYS  = max(1, _i("RETENTION_DAYS", 7))
STUCK_MINUTES   = max(1, _i("STUCK_MINUTES", 30))
SLOWMO_FACTOR   = max(2, _i("SLOWMO_FACTOR", 4))

# Clips moved here are never purged, by age or by disk pressure. It lives
# INSIDE the clip directory on purpose: same filesystem, so keeping a clip is
# an atomic rename rather than a copy, and the same systemd ReadWritePaths
# already covers it.
KEEP_DIR        = os.path.join(INCOMING_DIR, "keep")

# The chop log: every trigger any node reported, whether a clip came of it or
# not. Also inside the clip directory, for the same two reasons -- and the
# purge ignores it because it is not a .mp4.
CHOPLOG_PATH    = _s("CHOPLOG", os.path.join(INCOMING_DIR, "choplog.jsonl"))
CHOPLOG_MAX     = max(100, _i("CHOPLOG_MAX", 5000))
# How often to re-read a node's /triggers even when its trigger count has not
# moved. A chop goes "recording" -> "recorded" without the count changing, so
# something has to pick that up.
CHOPLOG_SYNC_S  = max(5, _i("CHOPLOG_SYNC_SECONDS", 30))

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_nodes(raw):
    """Parse NODES="name=addr name=addr" into an ordered list of dicts.

    Raises ValueError on anything malformed -- a typo here means a tile that
    silently watches nothing, so it is a startup error rather than a shrug.
    """
    nodes, seen = [], set()
    for token in (raw or "").split():
        if "=" not in token:
            raise ValueError(
                f"NODES entry {token!r} is not name=address "
                '(e.g. NODES="uw1=192.168.0.101 uw2=192.168.0.102")')
        name, _, addr = token.partition("=")
        name, addr = name.strip(), addr.strip()
        if not name or not addr:
            raise ValueError(f"NODES entry {token!r} has an empty name or address")
        if not _NAME_RE.match(name):
            raise ValueError(
                f"NODES name {name!r} must be letters, digits, dot, dash or "
                "underscore -- it has to match the node's NODE_NAME")
        if name in seen:
            raise ValueError(f"NODES lists {name!r} twice")
        seen.add(name)
        nodes.append({"name": name, "address": addr})
    return nodes


NODES = parse_nodes(_s("NODES", ""))


def clip_id(name):
    """SITE-NODE for a node name, matching how the capture side labels clips."""
    return f"{SITE}-{name}" if SITE else name


# ---------------------------------------------------------------------------
# Node status polling
# ---------------------------------------------------------------------------
def blank_status(nodes):
    """A status entry per node, before anything has been polled."""
    return {n["name"]: {"state": "unknown", "reachable": False, "health": None,
                        "error": "", "checked": None}
            for n in nodes}


_status = blank_status(NODES)
_status_lock = threading.Lock()
_stop = threading.Event()


def poll_node(node):
    url = f"http://{node['address']}:{NODE_PORT}/healthz"
    try:
        # /healthz answers 503 when degraded, which is data, not an error.
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
        healthy = bool(body.get("healthy"))
        return {"state": "healthy" if healthy else "degraded",
                "reachable": True, "health": body, "error": "",
                "checked": time.time()}
    except Exception as exc:                              # noqa: BLE001
        return {"state": "unreachable", "reachable": False, "health": None,
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "checked": time.time()}


# name -> (monotonic of last /triggers fetch, trigger count at that fetch)
_trigger_sync = {}


def sync_triggers(node, health):
    """Drain a node's /triggers into the chop log when it may have changed.

    Fetched when the node's trigger count moves, and otherwise every
    CHOPLOG_SYNC_S -- a chop goes "recording" to "recorded" a post-roll after
    it fires without the count changing, and that transition is most of what
    the log is for.
    """
    name = node["name"]
    count = ((health or {}).get("triggers") or {}).get("count")
    last_at, last_count = _trigger_sync.get(name, (0.0, None))
    now = time.monotonic()
    if count == last_count and (now - last_at) < CHOPLOG_SYNC_S:
        return
    # Recorded before the fetch, and left recorded even if it fails: /triggers
    # returns the node's whole ring rather than a delta, so a failed fetch
    # loses nothing that the next periodic one will not pick up, and a node
    # that is down should not be asked every POLL_SECONDS.
    _trigger_sync[name] = (now, count)
    url = f"http://{node['address']}:{NODE_PORT}/triggers"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:                                     # noqa: BLE001
        return              # unreachable, or a node too old to have /triggers
    choplog_merge(name, body.get("clip_id") or clip_id(name),
                  body.get("triggers"))


def poll_loop():
    while not _stop.is_set():
        for node in NODES:
            if _stop.is_set():
                return
            result = poll_node(node)
            with _status_lock:
                _status[node["name"]] = result
            sync_triggers(node, result.get("health"))
        _stop.wait(POLL_SECONDS)


# ---------------------------------------------------------------------------
# Clips on disk
# ---------------------------------------------------------------------------
_CLIP_RE = re.compile(r"^event_(\d{8})_(\d{6}(?:Z|[+-]\d{4}))_(.+)\.mp4$")


def parse_clip_name(fname):
    """(clip_id, utc_datetime) from a delivered clip name, or (None, None)."""
    m = _CLIP_RE.match(fname)
    if not m:
        return None, None
    date, tod, cid = m.groups()
    try:
        if tod.endswith("Z"):
            when = datetime.strptime(date + tod[:-1], "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc)
        else:
            when = datetime.strptime(date + tod, "%Y%m%d%H%M%S%z")
    except ValueError:
        return cid, None
    return cid, when


def _scan_clips(directory, kept):
    out = []
    try:
        entries = os.scandir(directory)
    except OSError:
        return out
    with entries as it:
        for entry in it:
            if not entry.is_file() or not entry.name.endswith(".mp4"):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            cid, when = parse_clip_name(entry.name)
            out.append({
                "file": entry.name,
                "clip_id": cid,
                "recorded_utc": when.isoformat(timespec="seconds") if when else None,
                "mb": round(st.st_size / (1024 * 1024), 1),
                "received": st.st_mtime,
                # Kept clips are still ordinary clips everywhere else: they
                # play, download and count as "the last chop" exactly as
                # before. The only difference is that the purge cannot see
                # them.
                "kept": kept,
                # Sort key: when the chop HAPPENED, not when the file landed.
                # A node that was offline delivers a backlog in one burst, so
                # every mtime is within the same second and their order is
                # whatever the filesystem hands back -- which made "last chop"
                # able to pick a clip hours older than the newest one.
                # Falls back to mtime for a name that will not parse.
                # An epoch float, not the ISO string, so clips written with a
                # local-time offset still order correctly against UTC ones.
                "_order": when.timestamp() if when else st.st_mtime,
            })
    return out


def list_clips(limit=None, for_clip_id=None):
    """Newest clips first, from the clip directory and the keep directory.

    for_clip_id narrows to one camera -- named so it cannot be confused
    with the clip_id() function it is usually called with.
    """
    out = _scan_clips(INCOMING_DIR, False) + _scan_clips(KEEP_DIR, True)
    if for_clip_id:
        out = [c for c in out if c["clip_id"] == for_clip_id]
    out.sort(key=lambda c: c["_order"], reverse=True)
    return out[:limit] if limit else out


def clips_by_node(clips=None):
    """Newest clip and total count per node, for the wall."""
    summary = {n["name"]: {"count": 0, "last": None} for n in NODES}
    wanted = {clip_id(n["name"]): n["name"] for n in NODES}
    for clip in (list_clips() if clips is None else clips):
        name = wanted.get(clip["clip_id"])
        if name is None:
            continue
        entry = summary[name]
        entry["count"] += 1
        if entry["last"] is None:
            entry["last"] = clip["recorded_utc"] or clip["file"]
    return summary


def newest_clip_for(name, clips=None):
    """The most recent delivered clip for one node, or None."""
    if clips is None:
        clips = list_clips(limit=1, for_clip_id=clip_id(name))
        return clips[0] if clips else None
    want = clip_id(name)
    return next((c for c in clips if c["clip_id"] == want), None)


def pending_state(health, newest):
    """Where a chop that has fired but not arrived currently is.

    The node reports when its trigger last fired and how many clips are queued
    for transcode and for shipping; the aggregator knows what has landed. If
    the trigger is newer than anything delivered, a chop is somewhere in the
    pipeline -- and if it has been there too long, the pipeline is stuck, which
    otherwise nothing in the system notices.
    """
    triggers = (health or {}).get("triggers") or {}
    last_trigger = triggers.get("last_utc")
    if not last_trigger:
        return {"state": "none", "since_s": None}
    try:
        fired = datetime.fromisoformat(last_trigger)
    except ValueError:
        return {"state": "none", "since_s": None}
    if fired.tzinfo is None:
        fired = fired.replace(tzinfo=timezone.utc)

    if newest and newest.get("recorded_utc"):
        try:
            have = datetime.fromisoformat(newest["recorded_utc"])
            # Delivered something at least as new as the last trigger.
            if have >= fired - timedelta(seconds=2):
                return {"state": "none", "since_s": None}
        except ValueError:
            pass

    waited = (datetime.now(timezone.utc) - fired).total_seconds()
    if waited < 0:
        waited = 0.0
    if waited > STUCK_MINUTES * 60:
        state = "delayed"
    else:
        clips = (health or {}).get("clips") or {}
        if clips.get("awaiting_ship"):
            state = "sending"
        else:
            state = "processing"
    return {"state": state, "since_s": round(waited)}


def aggregate_status():
    with _status_lock:
        snap = {k: dict(v) for k, v in _status.items()}
    # One directory scan for the whole snapshot: /status is fetched every
    # POLL_SECONDS by every open wall, and this used to re-scan once per node.
    clips = list_clips()
    by_node = clips_by_node(clips)
    nodes = []
    for node in NODES:
        st = snap[node["name"]]
        health = st.get("health") or {}
        camera = health.get("camera") or {}
        plc = health.get("plc") or {}
        shape = health.get("clip_shape") or {}
        newest = newest_clip_for(node["name"], clips)
        pending = pending_state(health, newest)
        nodes.append({
            "name": node["name"],
            "clip_id": clip_id(node["name"]),
            "address": node["address"],
            "stream": f"http://{node['address']}:{NODE_PORT}/stream",
            "state": st["state"],
            "error": st["error"],
            "camera_state": camera.get("state"),
            "frame_age_s": camera.get("frame_age_s"),
            "plc_state": plc.get("state") if plc.get("enabled") else "disabled",
            "triggers": (health.get("triggers") or {}).get("count"),
            "clips_here": by_node[node["name"]]["count"],
            "last_clip": by_node[node["name"]]["last"],
            # What the "Last chop" button plays and downloads.
            "last_clip_file": newest["file"] if newest else None,
            "last_clip_utc": newest["recorded_utc"] if newest else None,
            "last_clip_kept": bool(newest and newest["kept"]),
            "last_clip_age_s": (
                round(time.time() - newest["received"]) if newest else None),
            # Where the chop sits inside a clip, for the player's marker. None
            # until the node has been reached at least once, in which case the
            # player falls back to the middle of the clip.
            "post_seconds": shape.get("post_seconds"),
            "pre_seconds": shape.get("pre_seconds"),
            "pending": pending,
            "checked": st["checked"],
        })
    healthy = sum(1 for n in nodes if n["state"] == "healthy")
    stuck = [n["name"] for n in nodes if n["pending"]["state"] == "delayed"]
    return {
        "site": SITE or None,
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes_total": len(nodes),
        "nodes_healthy": healthy,
        "nodes_delayed": stuck,
        "retention_days": RETENTION_DAYS,
        "kept_clips": sum(1 for c in clips if c["kept"]),
        "incoming_dir": INCOMING_DIR,
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# The chop log
#
# Nodes remember their own triggers, but only in RAM and only the last few
# hundred; a reboot loses them. The aggregator drains each node's /triggers
# into one file here, so the log survives node restarts, node replacements and
# the clips themselves. That last part is the point: retention deletes footage
# after RETENTION_DAYS, and without this there would afterwards be no record
# that the chop ever happened.
#
# Whether a clip is actually on disk is NOT stored -- it is worked out at read
# time from the directory, so the log cannot go stale against the filesystem.
# ---------------------------------------------------------------------------
_choplog = {}                        # "node|utc" -> record
_choplog_lock = threading.RLock()
_choplog_dirty = False


def _choplog_key(node, utc):
    return f"{node}|{utc}"


def choplog_load():
    """Read the log from disk once at startup. A bad line is skipped, not fatal."""
    loaded, bad = {}, 0
    try:
        with open(CHOPLOG_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(rec, dict) or not rec.get("utc"):
                    bad += 1
                    continue
                loaded[_choplog_key(rec.get("node", ""), rec["utc"])] = rec
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"  ! chop log {CHOPLOG_PATH}: {exc}")
        return
    with _choplog_lock:
        _choplog.clear()
        _choplog.update(loaded)
    if bad:
        print(f"  ! chop log: skipped {bad} unreadable line(s)")


def choplog_flush():
    """Rewrite the log, atomically. Cheap: it is a few hundred KB at most."""
    global _choplog_dirty
    with _choplog_lock:
        if not _choplog_dirty:
            return
        records = sorted(_choplog.values(),
                         key=lambda r: (r.get("utc", ""), r.get("node", "")))
        if len(records) > CHOPLOG_MAX:
            records = records[-CHOPLOG_MAX:]
            _choplog.clear()
            _choplog.update({_choplog_key(r.get("node", ""), r["utc"]): r
                             for r in records})
        body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
        try:
            os.makedirs(os.path.dirname(CHOPLOG_PATH) or ".", exist_ok=True)
            # Same directory, so the replace is atomic: a crash mid-write can
            # never leave a half-written log where the real one was.
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CHOPLOG_PATH) or ".",
                                       prefix=".choplog-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, CHOPLOG_PATH)
            _choplog_dirty = False
        except OSError as exc:
            print(f"  ! chop log could not be written to {CHOPLOG_PATH}: {exc}")


# Fields a node owns. Anything else it sends is ignored rather than stored.
_TRIGGER_FIELDS = ("utc", "source", "clip", "state", "detail")


def choplog_merge(node, name_clip_id, records):
    """Fold one node's /triggers into the log. Returns (added, updated).

    Records are identified by (node, trigger time), so re-reading the same
    node repeatedly is free, and a node that restarts and replays what it
    still remembers cannot create duplicates.
    """
    global _choplog_dirty
    added = updated = 0
    with _choplog_lock:
        for raw in records or []:
            if not isinstance(raw, dict) or not raw.get("utc"):
                continue
            rec = {k: raw.get(k) for k in _TRIGGER_FIELDS}
            rec["node"] = node
            rec["clip_id"] = name_clip_id
            key = _choplog_key(node, rec["utc"])
            have = _choplog.get(key)
            if have is None:
                _choplog[key] = rec
                added += 1
            elif any(have.get(k) != rec.get(k) for k in _TRIGGER_FIELDS):
                # A chop goes "recording" -> "recorded" a post-roll later, and
                # gains its clip name at the same moment.
                have.update(rec)
                updated += 1
        if added or updated:
            _choplog_dirty = True
    if added or updated:
        choplog_flush()
    return added, updated


def clip_outcome(rec, on_disk, now=None):
    """What became of one chop, decided against the clips actually present.

    Kept separate from the stored record so the answer is always current: a
    clip that was on disk yesterday and has since been purged reads as purged
    today, without anything having to rewrite the log.
    """
    state = rec.get("state") or ""
    if state in ("failed", "coalesced"):
        return state
    if on_disk:
        return "kept" if on_disk.get("kept") else "on disk"
    if not rec.get("clip"):
        return "recording" if state == "recording" else "no clip"
    now = now or datetime.now(timezone.utc)
    try:
        fired = datetime.fromisoformat(rec["utc"])
        if fired.tzinfo is None:
            fired = fired.replace(tzinfo=timezone.utc)
        age_s = (now - fired).total_seconds()
    except (ValueError, KeyError):
        return "missing"
    if age_s > RETENTION_DAYS * 86400:
        # Older than retention and gone: deleted on schedule, not lost.
        return "purged"
    if age_s < STUCK_MINUTES * 60:
        return "in transit"
    return "missing"


def choplog_entries(limit=None, node=None):
    """The log, newest first, with each chop's current outcome filled in."""
    on_disk = {c["file"]: c for c in list_clips()}
    now = datetime.now(timezone.utc)
    with _choplog_lock:
        records = [dict(r) for r in _choplog.values()]
    if node:
        records = [r for r in records if r.get("node") == node]
    records.sort(key=lambda r: (r.get("utc", ""), r.get("node", "")), reverse=True)
    if limit:
        records = records[:limit]
    out = []
    for rec in records:
        clip = on_disk.get(rec.get("clip")) if rec.get("clip") else None
        rec["outcome"] = clip_outcome(rec, clip, now)
        rec["playable"] = bool(clip)
        rec["kept"] = bool(clip and clip["kept"])
        rec["mb"] = clip["mb"] if clip else None
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Serving clips
# ---------------------------------------------------------------------------
def clean_clip_name(name):
    """A bare clip filename from a URL, or None.

    The name arrives from a URL, so this is the boundary that stops
    /clip/../../etc/passwd: only a plain filename ending in .mp4 gets past it.
    """
    name = urllib.parse.unquote(name or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if not name.endswith(".mp4"):
        return None
    return name


def safe_clip_path(name):
    """Absolute path of a clip, in the clip directory or the keep directory.

    Both are checked because keeping a clip moves the file: every URL that
    worked before the move has to keep working after it, or the player breaks
    the moment someone presses Keep.
    """
    name = clean_clip_name(name)
    if not name:
        return None
    for directory in (INCOMING_DIR, KEEP_DIR):
        base = os.path.realpath(directory)
        path = os.path.realpath(os.path.join(base, name))
        # dirname check, not startswith: a symlink pointing out of the clip
        # directory resolves elsewhere and is rejected here.
        if os.path.dirname(path) == base and os.path.isfile(path):
            return path
    return None


def set_kept(name, keep):
    """Move a clip into or out of the keep directory.

    Returns (ok, message). An os.replace within one filesystem is atomic, so a
    clip is never briefly in neither place -- which matters because the purge
    may be running at the same time.
    """
    name = clean_clip_name(name)
    if not name:
        return False, "not a clip name"
    src_dir, dst_dir = (INCOMING_DIR, KEEP_DIR) if keep else (KEEP_DIR, INCOMING_DIR)
    src = os.path.join(src_dir, name)
    dst = os.path.join(dst_dir, name)
    if not os.path.isfile(src):
        # Already where it was asked to be: the button is idempotent, because
        # two people at two screens pressing Keep on the same chop is normal.
        if os.path.isfile(dst):
            return True, "already " + ("kept" if keep else "not kept")
        return False, "no such clip"
    try:
        os.makedirs(dst_dir, exist_ok=True)
        os.replace(src, dst)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "kept" if keep else "released"


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header, size):
    """(start, end) inclusive for a single byte range, or None.

    Range support is what makes the player's seek bar work: without it the
    browser re-fetches from byte zero on every seek. Only the single-range form
    browsers actually send is handled.
    """
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    first, last = m.group(1), m.group(2)
    if first == "" and last == "":
        return None
    if first == "":                       # bytes=-500 -> final 500 bytes
        length = int(last)
        if length <= 0:
            return None
        start = max(0, size - length)
        return start, size - 1
    start = int(first)
    if start >= size:
        return None                       # unsatisfiable
    end = int(last) if last else size - 1
    return start, min(end, size - 1)


def slowmo_copy(path, factor, out_path):
    """Write a slow-motion copy by remuxing -- no re-encode.

    -itsscale rescales the input timestamps, so a 120 fps clip is restamped to
    30 fps with every frame intact. It is a container rewrite, not an encode:
    a 30 s clip takes a fraction of a second, which matters because the Pi 5
    has no H.264 encoder either. The result plays at 1/factor speed in any
    player, including someone double-clicking it in Windows.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-itsscale", str(factor), "-i", path,
           "-c", "copy", "-movflags", "+faststart", out_path]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[:200])
    return out_path


# ---------------------------------------------------------------------------
# The wall page
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>chopcam - %(site)s</title>
<!-- The wall, the player and the chop log are one page served at both / and
     /log, so relative URLs need a fixed base to resolve against. -->
<base href="/">
<style>
  :root { color-scheme: dark; }
  html,body { margin:0; height:100%%; background:#0b0d10; color:#e8ecf1;
              font:13px/1.4 system-ui,-apple-system,Segoe UI,sans-serif; }
  body { display:flex; flex-direction:column; }
  header { display:flex; align-items:baseline; gap:14px; padding:8px 14px;
           border-bottom:1px solid #22262c; flex:0 0 auto; }
  header h1 { font-size:14px; margin:0; letter-spacing:.06em;
              text-transform:uppercase; color:#9aa4b2; font-weight:600; }
  #summary { font-variant-numeric:tabular-nums; color:#9aa4b2; }
  #summary b { color:#e8ecf1; }
  /* Columns and rows are set by layout() from the camera count so the tiles
     fill the monitor: 3 cameras go 2-over-1, 5 go 3-over-2, and so on. The
     auto-fit here is only what applies before that runs. Tiles span 2 of a
     doubled column track, which is what lets a short last row sit centred
     instead of hugging the left edge with a hole in the corner. */
  .grid { flex:1 1 auto; min-height:0; display:grid; gap:8px; padding:8px;
          grid-template-columns:repeat(auto-fit,minmax(360px,1fr));
          grid-auto-rows:1fr; }
  .tile { position:relative; background:#000; border:2px solid #2a3038;
          border-radius:6px; overflow:hidden; min-height:0; }
  .tile.healthy    { border-color:#2f6f4f; }
  .tile.degraded   { border-color:#b07d2b; }
  .tile.unreachable{ border-color:#8c3a3a; }
  .tile img { width:100%%; height:100%%; object-fit:contain; display:block; }
  .bar { position:absolute; left:0; right:0; bottom:0; display:flex;
         justify-content:space-between; align-items:center; gap:12px;
         padding:5px 9px; background:#000000cc; font-size:13px; z-index:2; }
  .name { font-weight:700; letter-spacing:.03em; white-space:nowrap; }
  .meta { color:#b6bfca; font-variant-numeric:tabular-nums;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
          flex:1 1 auto; text-align:right; }
  .badge { position:absolute; top:6px; right:6px; padding:3px 9px;
           border-radius:99px; font-size:11px; font-weight:700;
           letter-spacing:.05em; text-transform:uppercase; z-index:2; }
  .healthy .badge    { background:#2f6f4f; }
  .degraded .badge   { background:#c58b2f; color:#1a1205; }
  .unreachable .badge{ background:#a33f3f; }
  /* The last-chop button. Big enough to hit on a touchscreen and readable
     from across the room. */
  .chop { flex:0 0 auto; border:1px solid #3a434e; background:#1b2027;
          color:#e8ecf1; font:600 13px/1 inherit; padding:7px 12px;
          border-radius:5px; cursor:pointer; white-space:nowrap; }
  .chop:hover:not(:disabled) { background:#273040; border-color:#4d5a6b; }
  .chop:disabled { opacity:.45; cursor:default; }
  .chop.processing { border-color:#c58b2f; color:#ffd79a; }
  .chop.delayed    { border-color:#a33f3f; color:#ffb3b3; }
  .down { position:absolute; inset:0; display:flex; flex-direction:column;
          align-items:center; justify-content:center; text-align:center;
          gap:6px; padding:16px 16px 34px; background:#0b0d10ee;
          color:#c9d1da; z-index:1; }
  .down .why { color:#8c9099; font-size:12px; max-width:90%%; }
  .empty { padding:24px; color:#9aa4b2; }
  .empty code { color:#e8ecf1; }

  /* Player: fills the screen over the wall. */
  #player { position:fixed; inset:0; background:#000; z-index:50;
            display:flex; flex-direction:column; }
  #player[hidden] { display:none; }
  #ptop { display:flex; align-items:center; gap:14px; padding:10px 14px;
          background:#0b0d10; border-bottom:1px solid #22262c; flex:0 0 auto; }
  #ptitle { font-weight:700; font-size:15px; letter-spacing:.03em; }
  #pwhen { color:#9aa4b2; font-variant-numeric:tabular-nums; }
  #pspacer { flex:1 1 auto; }
  #player video { flex:1 1 auto; min-height:0; width:100%%;
                  background:#000; object-fit:contain; }
  #pbottom { display:flex; align-items:center; gap:8px; flex-wrap:wrap;
             padding:10px 14px; background:#0b0d10;
             border-top:1px solid #22262c; flex:0 0 auto; }
  .rate { border:1px solid #3a434e; background:#1b2027; color:#e8ecf1;
          font:600 13px/1 inherit; padding:7px 12px; border-radius:5px;
          cursor:pointer; }
  .rate.on { background:#2f6f4f; border-color:#2f6f4f; }
  .dl { border:1px solid #3a434e; background:#1b2027; color:#e8ecf1;
        font:600 13px/1 inherit; padding:7px 12px; border-radius:5px;
        text-decoration:none; }
  .dl.primary { background:#2f4f6f; border-color:#3d6288; }
  #pclose { border:1px solid #3a434e; background:#1b2027; color:#e8ecf1;
            font:700 14px/1 inherit; padding:8px 14px; border-radius:5px;
            cursor:pointer; }
  .hint { color:#7f8894; font-size:12px; }
%(css)s
</style></head>
<body>
  <header>
    <h1>chopcam%(site_label)s</h1>
    <div id="summary">connecting...</div>
    <span id="hspacer"></span>
    <button class="nav" id="logbtn" type="button">Chop log</button>
  </header>
  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" hidden>
    No nodes configured. Set <code>NODES</code> in
    <code>/etc/chopcam-agg.conf</code>, for example
    <code>NODES="uw1=192.168.0.101 uw2=192.168.0.102"</code>, then
    <code>sudo systemctl restart chopcam-wall</code>.
  </div>

  <div id="player" hidden>
    <div id="ptop">
      <span id="ptitle"></span>
      <span id="pwhen"></span>
      <span class="kept" id="pkept" hidden>Kept</span>
      <span id="pspacer"></span>
      <button class="nav" id="plistbtn" type="button">Older clips</button>
      <button id="pclose">Back to live &nbsp;(Esc)</button>
    </div>
    <div id="pmain">
      <div id="pstage">
        <video id="pvideo" playsinline preload="auto"></video>
        <!-- Own timeline, not the browser's: the native scrub bar cannot be
             drawn on, and the one thing worth drawing on it is where the chop
             is. -->
        <div id="ptimeline" title="Click to seek">
          <div id="ptlplayed"></div>
          <div id="ptlchop"></div>
          <div id="ptlhead"></div>
        </div>
        <div id="ptlabels">
          <span id="ptrel"></span>
          <span id="ptabs"></span>
        </div>
      </div>
      <div id="plist" hidden>
        <div class="lhead">Clips from this camera</div>
        <div id="plistrows"></div>
      </div>
    </div>
    <div id="pbottom">
      <button class="rate" id="pplay" style="min-width:5.5em">Pause</button>
      <button class="rate" id="pstepb" title="Back one step (,)">&#8249;</button>
      <button class="rate" id="pstepf" title="Forward one step (.)">&#8250;</button>
      <span class="hint">Speed</span>
      <button class="rate" data-rate="0.1">0.1x</button>
      <button class="rate on" data-rate="0.25">0.25x</button>
      <button class="rate" data-rate="0.5">0.5x</button>
      <button class="rate" data-rate="1">1x</button>
      <button class="rate chopjump" id="pchop" title="Jump to the trigger (c)">
        Jump to chop</button>
      <span id="pspacer2" style="flex:1 1 auto"></span>
      <button class="keep" id="pkeep" type="button">Keep this clip</button>
      <a class="dl primary" id="pdlslow" href="#">Download slow motion</a>
      <a class="dl" id="pdlorig" href="#">Download original speed</a>
    </div>
  </div>
%(html)s
<script>
// Tiles are built once and then only updated. Rebuilding them would tear down
// every MJPEG connection and restart all the streams.
let built = false;
let nodes = {};          // name -> latest status entry
let playing = null;      // node name while the player is open

function ago(sec) {
  if (sec === null || sec === undefined) return '';
  if (sec < 60) return Math.round(sec) + 's';
  if (sec < 3600) return Math.round(sec / 60) + 'm';
  return (sec / 3600).toFixed(1) + 'h';
}

function tile(n) {
  const el = document.createElement('div');
  el.className = 'tile';
  el.id = 'tile-' + n.name;
  el.innerHTML =
    '<img alt="">' +
    '<div class="badge"></div>' +
    '<div class="bar"><span class="name"></span>' +
    '<span class="meta"></span>' +
    '<button class="chop" type="button"></button></div>';
  el.querySelector('.name').textContent = n.clip_id;
  // One button per tile, deliberately. Older clips are reachable from inside
  // the player, where you already are once you have opened one -- a second
  // button here would crowd a bar that has to be readable across a room.
  el.querySelector('.chop').addEventListener('click',
    () => openPlayer(n.name));
  // An overlay holds the streams stopped, and tiles are built on the first
  // status poll -- which for /log lands AFTER the log has opened. Starting
  // the stream here would open an MJPEG connection per camera that nothing is
  // showing, which over the plant switch is exactly what the overlay stops.
  // stopLiveStreams(false) starts it from dataset.src when the overlay closes.
  const img = el.querySelector('img');
  if (overlaysOpen()) img.dataset.src = n.stream;
  else img.setAttribute('src', n.stream);
  return el;
}

function updateChopButton(btn, n) {
  const p = n.pending || {state: 'none'};
  btn.classList.remove('processing', 'delayed');
  if (p.state === 'delayed') {
    btn.classList.add('delayed');
    btn.textContent = 'Chop delayed \u00b7 ' + ago(p.since_s);
    btn.title = 'A chop fired ' + ago(p.since_s) + ' ago and still has not ' +
                'arrived. The pipeline on this node may be stuck.';
  } else if (p.state === 'processing' || p.state === 'sending') {
    btn.classList.add('processing');
    btn.textContent = (p.state === 'sending' ? 'Chop sending' : 'Chop processing')
                      + ' \u00b7 ' + ago(p.since_s);
    btn.title = 'A chop has fired and is still being transcoded on the node.';
  } else if (n.last_clip_file) {
    btn.textContent = 'Last chop \u00b7 ' + ago(n.last_clip_age_s);
    btn.title = 'Play the most recent chop from this camera';
  } else {
    btn.textContent = 'No clips yet';
    btn.title = 'Nothing has been delivered from this camera yet';
  }
  // Still playable while something newer is on the way -- the previous chop
  // is usually what you want to look at anyway.
  btn.disabled = !n.last_clip_file;
}

// Fill the screen rather than leaving a ragged row: square-ish grid, then
// centre whatever is left over on the last row.
//   1 -> 1      2 -> 2x1    3 -> 2 over 1
//   4 -> 2x2    5 -> 3 over 2    6 -> 3x2    7-9 -> 3x3 ...
// Narrow windows fall back to as many columns as actually fit, so the wall
// stays usable on a laptop or phone instead of producing unreadable slivers.
const MIN_TILE_PX = 360;

function layout(n) {
  const grid = document.getElementById('grid');
  if (!n) return;
  const width = grid.clientWidth || window.innerWidth;
  const ideal = Math.ceil(Math.sqrt(n));
  const fits = Math.max(1, Math.floor(width / MIN_TILE_PX));
  const cols = Math.max(1, Math.min(ideal, fits, n));
  const rows = Math.ceil(n / cols);

  grid.style.gridTemplateColumns = 'repeat(' + (cols * 2) + ', 1fr)';
  grid.style.gridTemplateRows = 'repeat(' + rows + ', 1fr)';

  const tiles = grid.querySelectorAll('.tile');
  tiles.forEach(t => {
    t.style.gridColumnStart = 'auto';
    t.style.gridColumnEnd = 'span 2';
  });

  // A last row with fewer tiles than columns starts half a tile in, which the
  // doubled tracks make an exact offset rather than an approximation.
  const leftover = n - (rows - 1) * cols;
  if (leftover > 0 && leftover < cols && tiles[n - leftover]) {
    tiles[n - leftover].style.gridColumnStart = String(cols - leftover + 1);
  }
}

function render(s) {
  const grid = document.getElementById('grid');
  document.getElementById('empty').hidden = s.nodes.length > 0;
  if (!built) {
    s.nodes.forEach(n => grid.appendChild(tile(n)));
    built = true;
    layout(s.nodes.length);
    window.addEventListener('resize', () => layout(s.nodes.length));
  }
  s.nodes.forEach(n => { nodes[n.name] = n; });

  let extra = '';
  if (s.nodes_delayed && s.nodes_delayed.length)
    extra = ' &middot; <b>' + s.nodes_delayed.length + ' delayed</b>';
  if (s.kept_clips)
    extra += ' &middot; <b>' + s.kept_clips + '</b> kept';
  document.getElementById('summary').innerHTML =
    '<b>' + s.nodes_healthy + '</b>/' + s.nodes_total + ' nodes healthy' +
    extra + ' &middot; keeping ' + s.retention_days + 'd' +
    ' &middot; ' + s.utc.replace('T', ' ').replace('+00:00', 'Z');

  s.nodes.forEach(n => {
    const el = document.getElementById('tile-' + n.name);
    if (!el) return;
    el.className = 'tile ' + n.state;
    el.querySelector('.badge').textContent = n.state;
    let meta = [];
    if (n.state === 'unreachable') {
      meta.push(n.address);
    } else {
      if (n.plc_state) meta.push('plc ' + n.plc_state);
      if (n.clips_here) meta.push(n.clips_here + ' clips');
    }
    el.querySelector('.meta').textContent = meta.join('  \u00b7  ');
    updateChopButton(el.querySelector('.chop'), n);

    const img = el.querySelector('img');
    let down = el.querySelector('.down');
    if (n.state === 'unreachable') {
      // Blank the <img> too: a failed load renders a broken-image icon, which
      // looks like a bug in the wall rather than a node being down.
      if (!overlaysOpen()) img.style.visibility = 'hidden';
      if (!down) {
        down = document.createElement('div');
        down.className = 'down';
        down.innerHTML = '<div class="what"></div><div class="why"></div>';
        el.appendChild(down);
      }
      down.querySelector('.what').textContent = 'No response from ' + n.address;
      down.querySelector('.why').textContent = n.error || '';
    } else {
      if (!overlaysOpen()) img.style.visibility = 'visible';
      if (down) down.remove();
    }
  });
}

%(js)s

async function refresh() {
  try {
    const r = await fetch('status', {cache: 'no-store'});
    render(await r.json());
  } catch (e) {
    document.getElementById('summary').textContent =
      'aggregator unreachable \u2014 ' + e;
  }
}
refresh();
setInterval(refresh, %(poll_ms)d);
</script>
</body></html>"""


_CSS = r"""
  /* ---- added features: keep, timeline, clip list, chop log ------------- */
  #hspacer { flex:1 1 auto; }
  .nav { border:1px solid #3a434e; background:#1b2027; color:#e8ecf1;
         font:600 12px/1 inherit; padding:6px 11px; border-radius:5px;
         cursor:pointer; }
  .nav:hover { background:#273040; border-color:#4d5a6b; }
  .nav.on { background:#2f4f6f; border-color:#3d6288; }

  .kept { background:#5b45a0; color:#fff; padding:3px 9px; border-radius:99px;
          font-size:11px; font-weight:700; letter-spacing:.05em;
          text-transform:uppercase; }
  .keep { border:1px solid #3a434e; background:#1b2027; color:#e8ecf1;
          font:600 13px/1 inherit; padding:7px 12px; border-radius:5px;
          cursor:pointer; }
  .keep:hover:not(:disabled) { background:#273040; border-color:#4d5a6b; }
  .keep:disabled { opacity:.4; cursor:default; }
  .keep.on { background:#5b45a0; border-color:#7a5cc0; }

  /* ---- player ---------------------------------------------------------- */
  #pmain { flex:1 1 auto; min-height:0; display:flex; }
  #pstage { flex:1 1 auto; min-width:0; min-height:0;
            display:flex; flex-direction:column; }
  #ptimeline { position:relative; height:26px; margin:6px 14px 0; flex:0 0 auto;
               background:#1b2027; border:1px solid #2a3038; border-radius:4px;
               cursor:pointer; overflow:hidden; }
  #ptlplayed { position:absolute; left:0; top:0; bottom:0; width:0;
               background:#2f4f6f; }
  /* The trigger instant. This mark is the whole reason the player does not
     use the browser's own scrub bar, which cannot be drawn on. */
  #ptlchop { position:absolute; top:0; bottom:0; width:3px; margin-left:-1px;
             background:#ff4d4d; box-shadow:0 0 7px #ff4d4d; z-index:2;
             pointer-events:none; }
  /* The label paints over the marker's own line, so it needs a solid ground
     or the line runs straight through the letters. */
  #ptlchop::after { content:'CHOP'; position:absolute; left:50%;
                    transform:translateX(-50%); top:5px; padding:1px 4px;
                    background:#1a0f0f; border-radius:3px;
                    font:700 10px/1.2 system-ui,sans-serif; letter-spacing:.09em;
                    color:#ff9a9a; }
  #ptlhead { position:absolute; top:0; bottom:0; width:2px; margin-left:-1px;
             background:#e8ecf1; z-index:3; pointer-events:none; }
  #ptlabels { display:flex; justify-content:space-between; gap:12px;
              padding:4px 15px 0; font-size:12px; color:#9aa4b2;
              font-variant-numeric:tabular-nums; flex:0 0 auto; }
  #ptrel { color:#ffb3b3; font-weight:600; }
  .chopjump { border-color:#8c3a3a; color:#ffb3b3; }
  .chopjump:hover { background:#2b1d1d; }

  /* ---- per-camera clip list -------------------------------------------- */
  #plist { flex:0 0 340px; max-width:45vw; overflow-y:auto; background:#0e1116;
           border-left:1px solid #22262c; }
  #plist[hidden] { display:none; }
  .lhead { position:sticky; top:0; z-index:1; background:#0e1116;
           padding:9px 12px; font:600 11px/1.3 inherit; letter-spacing:.08em;
           text-transform:uppercase; color:#9aa4b2;
           border-bottom:1px solid #22262c; }
  .crow { display:flex; align-items:center; gap:9px; padding:9px 12px;
          border-bottom:1px solid #171b21; cursor:pointer; }
  .crow:hover { background:#161c24; }
  .crow.on { background:#1d2836; box-shadow:inset 3px 0 0 #3d6288; }
  .crow .cstar { width:1em; text-align:center; color:#a98cf0; font-size:14px; }
  .crow .cwhen { flex:1 1 auto; min-width:0; }
  .crow .cdate { display:block; font-weight:600; }
  .crow .cage { display:block; color:#8c9099; font-size:11.5px; }
  .crow .csize { color:#8c9099; font-size:11.5px; white-space:nowrap;
                 font-variant-numeric:tabular-nums; }
  .cnote { padding:12px; color:#8c9099; }

  /* ---- chop log -------------------------------------------------------- */
  #logview { position:fixed; inset:0; background:#0b0d10; z-index:60;
             display:flex; flex-direction:column; }
  #logview[hidden] { display:none; }
  #ltop { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
          padding:10px 14px; border-bottom:1px solid #22262c; flex:0 0 auto; }
  #ltop h2 { margin:0; font-size:14px; letter-spacing:.06em;
             text-transform:uppercase; color:#9aa4b2; }
  #lnodes { display:flex; gap:6px; flex-wrap:wrap; }
  #lwrap { flex:1 1 auto; min-height:0; overflow:auto; }
  table.log { width:100%; border-collapse:collapse; font-size:13px; }
  table.log th { position:sticky; top:0; z-index:1; background:#0e1116;
                 text-align:left; padding:8px 12px; white-space:nowrap;
                 font-size:11px; letter-spacing:.07em; text-transform:uppercase;
                 color:#9aa4b2; border-bottom:1px solid #22262c; }
  table.log td { padding:7px 12px; border-bottom:1px solid #161a20; }
  table.log tr:hover td { background:#12161c; }
  td.when { white-space:nowrap; font-variant-numeric:tabular-nums; }
  td.when b { font-weight:600; }
  td.when small { display:block; color:#7f8894; }
  td.acts { white-space:nowrap; text-align:right; }
  .pill { display:inline-block; padding:3px 9px; border-radius:99px;
          font-size:11px; font-weight:700; letter-spacing:.04em;
          text-transform:uppercase; white-space:nowrap; }
  .pill.ondisk    { background:#2f6f4f; }
  .pill.kept      { background:#5b45a0; }
  .pill.transit   { background:#c58b2f; color:#1a1205; }
  .pill.recording { background:#2f4f6f; }
  .pill.purged,
  .pill.coalesced { background:#242a32; color:#9aa4b2; }
  .pill.missing,
  .pill.failed,
  .pill.noclip    { background:#a33f3f; }
  .mini { border:1px solid #3a434e; background:#1b2027; color:#e8ecf1;
          font:600 12px/1 inherit; padding:5px 9px; border-radius:4px;
          cursor:pointer; text-decoration:none; display:inline-block;
          margin-left:5px; }
  .mini:hover:not(:disabled) { background:#273040; }
  .mini:disabled { opacity:.3; cursor:default; }
  .mini.on { background:#5b45a0; border-color:#7a5cc0; }
  #lempty { padding:26px; color:#9aa4b2; max-width:52em; }

  /* A wall monitor has nobody to dismiss a modal dialog, so failures are
     transient text rather than alert(). */
  #toast { position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
           z-index:99; background:#a33f3f; color:#fff; font-weight:600;
           padding:10px 16px; border-radius:6px; max-width:80vw; }
  #toast[hidden] { display:none; }

  @media (max-width:900px) {
    #pmain { flex-direction:column; }
    #plist { flex:1 1 auto; max-width:100%; border-left:none;
             border-top:1px solid #22262c; }
  }
"""


_HTML = r"""
  <div id="logview" hidden>
    <div id="ltop">
      <h2>Chop log</h2>
      <span id="lsummary" class="hint"></span>
      <span id="lnodes"></span>
      <span style="flex:1 1 auto"></span>
      <button class="nav" id="lclose" type="button">Back to live &nbsp;(Esc)</button>
    </div>
    <div id="lwrap">
      <table class="log">
        <thead><tr>
          <th>When</th><th>Camera</th><th>Trigger</th><th>Clip</th>
          <th>Size</th><th></th>
        </tr></thead>
        <tbody id="lrows"></tbody>
      </table>
      <div id="lempty" hidden></div>
    </div>
  </div>
  <div id="toast" hidden></div>
"""


_JS = r"""
// ---- shared --------------------------------------------------------------
function overlaysOpen() {
  return !document.getElementById('player').hidden ||
         !document.getElementById('logview').hidden;
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 5000);
}

// Local time, WITH the date. A timestamp without a date answers half the
// question, and these pages are read by people who were not here yesterday.
function stamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso.replace('T', ' ');
  return d.toLocaleString([], {weekday: 'short', year: 'numeric',
                               month: 'short', day: 'numeric',
                               hour: '2-digit', minute: '2-digit',
                               second: '2-digit', hour12: false});
}

function agoIso(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return ago(Math.max(0, (Date.now() - d.getTime()) / 1000));
}

function stopLiveStreams(stop) {
  // Not just hiding: each <img> holds an open MJPEG connection and keeps
  // decoding. Leaving several running while software-decoding a 120 fps clip
  // is what makes playback stutter on a Pi 5.
  document.querySelectorAll('.tile img').forEach(img => {
    if (stop) {
      if (!img.dataset.src) img.dataset.src = img.getAttribute('src');
      img.removeAttribute('src');
    } else if (img.dataset.src) {
      img.setAttribute('src', img.dataset.src);
      delete img.dataset.src;
    }
  });
}

// ---- player --------------------------------------------------------------
const video = document.getElementById('pvideo');
let rate = 0.25;
// What is loaded in the player right now. post is the recording node's
// POST_SECONDS, which is what places the trigger marker.
let cur = {node: null, file: null, utc: null, kept: false, post: null};
const listCache = {};                  // node name -> clips from /clips

function setRate(r) {
  rate = r;
  video.playbackRate = r;
  document.querySelectorAll('.rate[data-rate]').forEach(b =>
    b.classList.toggle('on', parseFloat(b.dataset.rate) === r));
}
document.querySelectorAll('.rate[data-rate]').forEach(b =>
  b.addEventListener('click', () => setRate(parseFloat(b.dataset.rate))));

// The browser's own controls fade out after a few seconds, and the wall may be
// on a monitor with nothing to move a pointer with. These never hide.
const playBtn = document.getElementById('pplay');
function syncPlayBtn() { playBtn.textContent = video.paused ? 'Play' : 'Pause'; }
playBtn.addEventListener('click', () => {
  video.paused ? video.play().catch(() => {}) : video.pause();
});
video.addEventListener('play', syncPlayBtn);
video.addEventListener('pause', syncPlayBtn);
video.addEventListener('ended', syncPlayBtn);

// playbackRate is reset by a load, so it has to be reapplied once the new
// clip's metadata is in.
video.addEventListener('loadedmetadata', () => { video.playbackRate = rate; });
video.addEventListener('play', () => { video.playbackRate = rate; });

// A nudge small enough to be a frame or two at any rate these cameras run at,
// which is what stepping through the moment of a chop needs.
const STEP_S = 1 / 60;
function step(dir) {
  if (!isFinite(video.duration)) return;
  video.pause();
  video.currentTime = Math.max(0, Math.min(video.duration,
                                           video.currentTime + dir * STEP_S));
  drawTimeline();
}
document.getElementById('pstepb').addEventListener('click', () => step(-1));
document.getElementById('pstepf').addEventListener('click', () => step(1));

function mmss(t) {
  if (!isFinite(t)) return '--';
  const m = Math.floor(t / 60), s = t - m * 60;
  return m + ':' + (s < 10 ? '0' : '') + s.toFixed(1);
}

// Where the trigger instant is, in seconds from the start of the clip.
function chopAt() {
  const d = video.duration;
  if (!isFinite(d) || d <= 0) return null;
  // Measured back from the END, not forward from the start. The post-roll is
  // recorded after the trigger and is always complete; the pre-roll comes out
  // of the ring buffer and can be short if the buffer had not filled, which
  // puts the chop LATER in the file than PRE_SECONDS. Halfway is only right
  // when both are intact, so it is the fallback for a node whose clip shape
  // the aggregator has not read yet.
  if (cur.post > 0 && cur.post < d) return d - cur.post;
  return d / 2;
}

function drawTimeline() {
  const d = video.duration;
  const played = document.getElementById('ptlplayed');
  const head = document.getElementById('ptlhead');
  const mark = document.getElementById('ptlchop');
  const abs = document.getElementById('ptabs');
  const rel = document.getElementById('ptrel');
  if (!isFinite(d) || d <= 0) {
    played.style.width = '0';
    head.style.left = '0';
    mark.hidden = true;
    abs.textContent = rel.textContent = '';
    return;
  }
  const pos = Math.max(0, Math.min(1, video.currentTime / d));
  played.style.width = (pos * 100).toFixed(3) + '%';
  head.style.left = (pos * 100).toFixed(3) + '%';
  const c = chopAt();
  mark.hidden = c === null;
  if (c !== null) mark.style.left = (c / d * 100).toFixed(3) + '%';
  abs.textContent = mmss(video.currentTime) + ' / ' + mmss(d);
  if (c === null) {
    rel.textContent = '';
  } else {
    const off = video.currentTime - c;
    rel.textContent = (off >= 0 ? '+' : '−') +
                      Math.abs(off).toFixed(2) + ' s from chop';
  }
}
['timeupdate', 'loadedmetadata', 'seeked', 'durationchange'].forEach(
  ev => video.addEventListener(ev, drawTimeline));

// timeupdate only fires a few times a second, which leaves the playhead
// visibly behind the picture. Redraw with the display instead, but only while
// the player is actually open.
function tickTimeline() {
  if (document.getElementById('player').hidden) return;
  drawTimeline();
  requestAnimationFrame(tickTimeline);
}

document.getElementById('ptimeline').addEventListener('click', e => {
  const d = video.duration;
  if (!isFinite(d) || d <= 0) return;
  const box = e.currentTarget.getBoundingClientRect();
  video.currentTime = Math.max(
    0, Math.min(d, (e.clientX - box.left) / box.width * d));
  drawTimeline();
});

function jumpToChop() {
  const c = chopAt();
  if (c === null) return;
  // Half a second early, so the chop happens on screen instead of the clip
  // starting mid-event. Two seconds of wall clock at 0.25x.
  video.currentTime = Math.max(0, c - 0.5);
  drawTimeline();
}
document.getElementById('pchop').addEventListener('click', jumpToChop);

// ---- keep ----------------------------------------------------------------
function syncKeep() {
  const btn = document.getElementById('pkeep');
  btn.disabled = !cur.file;
  btn.classList.toggle('on', !!cur.kept);
  btn.textContent = cur.kept ? 'Kept ★  (release)' : 'Keep this clip';
  btn.title = cur.kept
    ? 'This clip is exempt from the purge. Press to let it expire normally.'
    : 'Move this clip where the purge cannot delete it, however old it gets';
  document.getElementById('pkept').hidden = !cur.kept;
}

async function setKept(file, keep) {
  const r = await fetch((keep ? 'keep/' : 'unkeep/') + encodeURIComponent(file),
                        {method: 'POST'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) throw new Error(j.detail || ('HTTP ' + r.status));
}

async function toggleKeep() {
  if (!cur.file) return;
  const btn = document.getElementById('pkeep');
  btn.disabled = true;
  try {
    await setKept(cur.file, !cur.kept);
    cur.kept = !cur.kept;
    syncKeep();
    delete listCache[cur.node];
    loadList(cur.node);
    refresh();
  } catch (e) {
    toast('Could not change this clip: ' + e.message);
    btn.disabled = false;
  }
}
document.getElementById('pkeep').addEventListener('click', toggleKeep);

// ---- per-camera clip list ------------------------------------------------
async function loadList(name) {
  const rows = document.getElementById('plistrows');
  if (!listCache[name]) rows.innerHTML = '<div class="cnote">loading...</div>';
  try {
    const r = await fetch('clips?limit=500&node=' + encodeURIComponent(name),
                          {cache: 'no-store'});
    listCache[name] = (await r.json()).clips || [];
  } catch (e) {
    rows.innerHTML = '<div class="cnote">could not list clips</div>';
    return;
  }
  renderList(name);
}

function renderList(name) {
  const clips = listCache[name];
  if (!clips) return;                  // loadList is still on its way
  // The only way into the list, so it says how much is behind it.
  document.getElementById('plistbtn').textContent =
    clips.length > 1 ? 'Older clips \u00b7 ' + (clips.length - 1) : 'Older clips';
  const rows = document.getElementById('plistrows');
  document.querySelector('#plist .lhead').textContent =
    clips.length + (clips.length === 1 ? ' clip from ' : ' clips from ') +
    ((nodes[name] || {}).clip_id || name);
  rows.innerHTML = '';
  if (!clips.length) {
    rows.innerHTML = '<div class="cnote">nothing delivered yet</div>';
    return;
  }
  clips.forEach(c => {
    const el = document.createElement('div');
    el.className = 'crow' + (c.file === cur.file ? ' on' : '');
    el.innerHTML = '<span class="cstar"></span>' +
                   '<span class="cwhen"><span class="cdate"></span>' +
                   '<span class="cage"></span></span>' +
                   '<span class="csize"></span>';
    el.querySelector('.cstar').textContent = c.kept ? '★' : '';
    el.querySelector('.cdate').textContent = stamp(c.recorded_utc) || c.file;
    el.querySelector('.cage').textContent =
      (c.recorded_utc ? agoIso(c.recorded_utc) + ' ago' : 'time unknown') +
      (c.kept ? '  ·  kept' : '');
    el.querySelector('.csize').textContent = c.mb + ' MB';
    el.title = c.file + (c.recorded_utc ? '\n' + c.recorded_utc : '');
    el.addEventListener('click', () => loadClip(name, c));
    rows.appendChild(el);
  });
}

document.getElementById('plistbtn').addEventListener('click', () => {
  const panel = document.getElementById('plist');
  panel.hidden = !panel.hidden;
  document.getElementById('plistbtn').classList.toggle('on', !panel.hidden);
  if (!panel.hidden && cur.node) loadList(cur.node);
});

// ---- opening and closing -------------------------------------------------
function loadClip(name, c) {
  const n = nodes[name] || {};
  cur = {node: name, file: c.file, utc: c.recorded_utc || null,
         kept: !!c.kept, post: n.post_seconds || null};
  const url = 'clip/' + encodeURIComponent(c.file);
  document.getElementById('ptitle').textContent = n.clip_id || name;
  document.getElementById('pwhen').textContent = stamp(c.recorded_utc) || c.file;
  document.getElementById('pwhen').title = c.file;
  document.getElementById('pdlslow').href = url + '?slowmo=1';
  document.getElementById('pdlorig').href = url + '?download=1';
  syncKeep();
  video.src = url;
  setRate(rate);
  video.play().catch(() => {});   // autoplay may be blocked; controls still work
  syncPlayBtn();
  renderList(name);
}

function openPlayer(name, file) {
  const n = nodes[name];
  if (!n) return;
  // Metadata for the newest clip is already in the status snapshot; for any
  // other file it comes from the row that was clicked.
  const last = !file || file === n.last_clip_file;
  file = file || n.last_clip_file;
  if (!file) return;
  playing = name;
  closeLog();
  stopLiveStreams(true);
  // The list starts closed and is opened by the Older clips button: the point
  // of the player is the clip, and the panel takes a fifth of the screen.
  const panel = document.getElementById('plist');
  panel.hidden = true;
  document.getElementById('plistbtn').classList.remove('on');
  document.getElementById('player').hidden = false;
  if (file)
    loadClip(name, {file: file,
                    recorded_utc: last ? n.last_clip_utc : null,
                    kept: last ? n.last_clip_kept : false});
  loadList(name);
  requestAnimationFrame(tickTimeline);
}

function closePlayer() {
  playing = null;
  cur = {node: null, file: null, utc: null, kept: false, post: null};
  video.pause();
  video.removeAttribute('src');
  video.load();                   // drop the buffered clip from memory
  document.getElementById('player').hidden = true;
  if (!overlaysOpen()) stopLiveStreams(false);
}
document.getElementById('pclose').addEventListener('click', closePlayer);

// ---- chop log ------------------------------------------------------------
let logNode = null;
let logTimer = null;

function logOpen() { return !document.getElementById('logview').hidden; }

function openLog() {
  if (logOpen()) return;
  document.getElementById('logview').hidden = false;
  document.getElementById('logbtn').classList.add('on');
  stopLiveStreams(true);
  loadLog();
  logTimer = setInterval(loadLog, 10000);
}

function closeLog() {
  if (!logOpen()) return;
  clearInterval(logTimer);
  logTimer = null;
  document.getElementById('logview').hidden = true;
  document.getElementById('logbtn').classList.remove('on');
  if (!overlaysOpen()) stopLiveStreams(false);
}
document.getElementById('logbtn').addEventListener('click',
  () => logOpen() ? closeLog() : openLog());
document.getElementById('lclose').addEventListener('click', closeLog);

async function loadLog() {
  try {
    const r = await fetch('choplog?limit=500', {cache: 'no-store'});
    renderLog(await r.json());
  } catch (e) {
    document.getElementById('lsummary').textContent = 'chop log unavailable';
  }
}

// outcome -> [css class, what it means]
const OUTCOMES = {
  'on disk':    ['ondisk',    'Delivered, and on the aggregator now'],
  'kept':       ['kept',      'Kept: the purge will never delete this one'],
  'in transit': ['transit',   'Recorded on the node, still transcoding or on its way here'],
  'recording':  ['recording', 'Fired just now; the post-roll is still being recorded'],
  'purged':     ['purged',    'Deleted on schedule after it passed retention'],
  'coalesced':  ['coalesced', 'Fired while the previous chop was still recording, so it is covered by that clip'],
  'missing':    ['missing',   'Recorded on the node but never arrived here'],
  'failed':     ['failed',    'The node could not record this one'],
  'no clip':    ['noclip',    'Fired, but produced no clip'],
};

function renderLog(s) {
  const entries = s.entries || [];
  const shown = logNode ? entries.filter(e => e.node === logNode) : entries;

  document.getElementById('lsummary').textContent =
    s.total + ' chop' + (s.total === 1 ? '' : 's') + ' recorded  ·  ' +
    'clips deleted after ' + s.retention_days + 'd unless kept';

  // Filter chips are built from the nodes the LOG contains, not from NODES, so
  // a camera that has since been removed from the config is still reachable.
  const names = [];
  entries.forEach(e => {
    if (e.node && names.indexOf(e.node) < 0) names.push(e.node);
  });
  const bar = document.getElementById('lnodes');
  const wanted = names.join('|');
  if (bar.dataset.built !== wanted) {
    bar.dataset.built = wanted;
    bar.innerHTML = '';
    [[null, 'All cameras']].concat(
      names.map(n => [n, (nodes[n] || {}).clip_id || n])
    ).forEach(pair => {
      const b = document.createElement('button');
      b.className = 'nav';
      b.type = 'button';
      b.textContent = pair[1];
      b.addEventListener('click', () => { logNode = pair[0]; loadLog(); });
      bar.appendChild(b);
    });
  }
  Array.from(bar.children).forEach((b, i) =>
    b.classList.toggle('on', (i === 0 ? null : names[i - 1]) === logNode));

  const body = document.getElementById('lrows');
  const empty = document.getElementById('lempty');
  body.innerHTML = '';
  empty.hidden = shown.length > 0;
  empty.textContent = entries.length
    ? 'No chops from this camera.'
    : 'No chops recorded yet. Every trigger any camera reports lands here, ' +
      'whether a clip came of it or not -- and stays after the footage ' +
      'itself has been purged.';

  shown.forEach(e => {
    const tr = document.createElement('tr');
    const known = OUTCOMES[e.outcome] || ['missing', e.outcome];
    tr.innerHTML =
      '<td class="when"><b></b><small></small></td>' +
      '<td class="cam"></td><td class="src"></td>' +
      '<td><span class="pill"></span></td>' +
      '<td class="size"></td><td class="acts"></td>';
    tr.querySelector('.when b').textContent = stamp(e.utc) || e.utc || '';
    tr.querySelector('.when small').textContent =
      (agoIso(e.utc) ? agoIso(e.utc) + ' ago  ·  ' : '') + (e.utc || '');
    tr.querySelector('.cam').textContent = e.clip_id || e.node || '';
    tr.querySelector('.src').textContent = e.source || '';
    tr.querySelector('.src').title = e.detail || '';
    const pill = tr.querySelector('.pill');
    pill.className = 'pill ' + known[0];
    pill.textContent = e.outcome;
    pill.title = known[1];
    tr.querySelector('.size').textContent = e.mb ? e.mb + ' MB' : '';

    const acts = tr.querySelector('.acts');
    if (e.playable && nodes[e.node]) {
      const play = document.createElement('button');
      play.className = 'mini';
      play.type = 'button';
      play.textContent = 'Play';
      play.addEventListener('click', () => openPlayer(e.node, e.clip));
      acts.appendChild(play);
    }
    if (e.playable) {
      const keep = document.createElement('button');
      keep.className = 'mini' + (e.kept ? ' on' : '');
      keep.type = 'button';
      keep.textContent = e.kept ? 'Kept ★' : 'Keep';
      keep.title = e.kept ? 'Release this clip back to normal retention'
                          : 'Exempt this clip from the purge';
      keep.addEventListener('click', async () => {
        keep.disabled = true;
        try {
          await setKept(e.clip, !e.kept);
          delete listCache[e.node];
          loadLog();
        } catch (err) {
          toast('Could not change this clip: ' + err.message);
          keep.disabled = false;
        }
      });
      acts.appendChild(keep);
      const dl = document.createElement('a');
      dl.className = 'mini';
      dl.textContent = 'Download';
      dl.href = 'clip/' + encodeURIComponent(e.clip) + '?slowmo=1';
      dl.title = 'Download this clip in slow motion';
      acts.appendChild(dl);
    }
    body.appendChild(tr);
  });
}

// ---- keys ----------------------------------------------------------------
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (logOpen()) return closeLog();
    if (playing) return closePlayer();
  }
  if (!playing) return;
  if (e.key === ' ') {
    e.preventDefault();
    video.paused ? video.play().catch(() => {}) : video.pause();
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault();
    video.currentTime = Math.max(0, video.currentTime - 1);
  } else if (e.key === 'ArrowRight') {
    e.preventDefault();
    video.currentTime = Math.min(video.duration || 0, video.currentTime + 1);
  } else if (e.key === ',') {
    step(-1);
  } else if (e.key === '.') {
    step(1);
  } else if (e.key === 'c' || e.key === 'C') {
    jumpToChop();
  } else if (e.key === 'k' || e.key === 'K') {
    toggleKeep();
  }
});

// /log is this same page with the log already open, so it can be bookmarked on
// a laptop without the live tiles ever starting.
if (/\/log\/?$/.test(location.pathname) || location.hash === '#log')
  openLog();
"""


def wall_page():
    """The wall, the player and the chop log -- one page, served at / and /log.

    _CSS/_HTML/_JS are substituted rather than written into _PAGE because
    _PAGE is %-formatted: every literal percent inside it has to be doubled,
    and those blocks are full of them. Substituted values are not rescanned,
    so they need no escaping at all.
    """
    return (_PAGE % {
        "site": SITE or "wall",
        "site_label": f" &middot; {SITE}" if SITE else "",
        "poll_ms": POLL_SECONDS * 1000,
        "css": _CSS,
        "html": _HTML,
        "js": _JS,
    }).encode("utf-8")


def _limit_param(params, default=None):
    """?limit=N, clamped. Falls back to CLIP_LIST_LIMIT."""
    raw = (params.get("limit") or [""])[0]
    try:
        return max(1, min(2000, int(raw)))
    except (TypeError, ValueError):
        return default or CLIP_LIST_LIMIT


class WallHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, indent=2).encode("utf-8"),
                   "application/json", status)

    def _serve_clip(self, path, download_name=None):
        """Send a file, honouring a single Range request."""
        size = os.path.getsize(path)
        rng = parse_range(self.headers.get("Range"), size)
        if self.headers.get("Range") and rng is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = rng if rng else (0, size - 1)
        length = end - start + 1

        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{download_name}"')
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass            # viewer closed the player or cancelled a download

    def _serve_slowmo(self, path, name):
        """Remux to slow motion on demand and hand it over as a download."""
        tmpdir = tempfile.mkdtemp(prefix="chopcam-slowmo-")
        stem = name[:-4] if name.endswith(".mp4") else name
        out_name = f"{stem}_slowmo{SLOWMO_FACTOR}x.mp4"
        out = os.path.join(tmpdir, out_name)
        try:
            slowmo_copy(path, SLOWMO_FACTOR, out)
            self._serve_clip(out, download_name=out_name)
        except FileNotFoundError:
            self._json({"error": "ffmpeg is not installed on the aggregator; "
                                 "slow-motion downloads need it"}, 500)
        except Exception as exc:                          # noqa: BLE001
            self._json({"error": f"slow-motion remux failed: {exc}"}, 500)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def do_POST(self):
        """Keep and release. POST, not GET: it moves a file.

        A GET that changes state would also be followed by anything that
        prefetches links, which on a page left open on a wall monitor is not
        theoretical.
        """
        raw_path, _, _query = self.path.partition("?")
        for prefix, keep in (("/keep/", True), ("/unkeep/", False)):
            if not raw_path.startswith(prefix):
                continue
            ok, msg = set_kept(raw_path[len(prefix):], keep)
            self._json({"ok": ok, "kept": keep if ok else None,
                        "detail": msg}, 200 if ok else 404)
            return
        self.send_error(404)

    def do_GET(self):
        raw_path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        if raw_path.startswith("/clip/"):
            name = raw_path[len("/clip/"):]
            path = safe_clip_path(name)
            if not path:
                self.send_error(404)
                return
            if params.get("slowmo"):
                self._serve_slowmo(path, os.path.basename(path))
            elif params.get("download"):
                self._serve_clip(path, download_name=os.path.basename(path))
            else:
                self._serve_clip(path)
            return

        path = raw_path.rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._send(wall_page(), "text/html; charset=utf-8")
        elif path == "/status":
            self._json(aggregate_status())
        elif path == "/log":
            # Same page, with the chop log open: one implementation, and a URL
            # a laptop can bookmark without starting every live tile.
            self._send(wall_page(), "text/html; charset=utf-8")
        elif path == "/clips":
            # ?node= is what the player's clip list uses; without it this is
            # every camera, as before.
            node = (params.get("node") or [""])[0]
            want = clip_id(node) if node else None
            clips = [{k: v for k, v in c.items() if not k.startswith("_")}
                     for c in list_clips(_limit_param(params), for_clip_id=want)]
            self._json({"incoming_dir": INCOMING_DIR, "node": node or None,
                        "clips": clips})
        elif path == "/choplog":
            node = (params.get("node") or [""])[0] or None
            self._json({
                "site": SITE or None,
                "retention_days": RETENTION_DAYS,
                "entries": choplog_entries(_limit_param(params), node),
                "total": len(_choplog),
                "path": CHOPLOG_PATH,
            })
        elif path == "/healthz":
            status = aggregate_status()
            writable = os.path.isdir(INCOMING_DIR) and os.access(INCOMING_DIR, os.W_OK)
            ok = writable and status["nodes_total"] > 0
            self._json({
                "role": "aggregator",
                "site": SITE or None,
                "healthy": ok,
                "incoming_dir": INCOMING_DIR,
                "incoming_writable": writable,
                "nodes_total": status["nodes_total"],
                "nodes_healthy": status["nodes_healthy"],
                "kept_clips": status["kept_clips"],
                "choplog_entries": len(_choplog),
                "utc": status["utc"],
            }, 200 if ok else 503)
        else:
            self.send_error(404)


def validate_config():
    problems = []
    if not NODES:
        problems.append(
            'NODES is empty -- nothing to display. Set it to the capture nodes '
            'at this install, e.g. NODES="uw1=192.168.0.101 uw2=192.168.0.102"')
    if not SITE:
        problems.append(
            "SITE is not set. It must match SITE on this install's capture "
            "nodes, or delivered clips cannot be matched to the tiles showing "
            "them.")
    if not os.path.isdir(INCOMING_DIR):
        problems.append(
            f"INCOMING_DIR {INCOMING_DIR!r} does not exist. It must match "
            "AGG_DIR in each node's config and be writable by the account the "
            "nodes ship as.")
    elif not os.access(INCOMING_DIR, os.W_OK):
        problems.append(f"INCOMING_DIR {INCOMING_DIR!r} is not writable.")
    return problems


def main():
    print(f"chopcam aggregator | site {SITE or '(unset)'} | "
          f"config {C.get('_path')}")
    for node in NODES:
        print(f"  node {node['name']:12} {node['address']}:{NODE_PORT}")
    print(f"  clips  {INCOMING_DIR}")
    print(f"  keep   {KEEP_DIR}")
    for problem in validate_config():
        print(f"  ! {problem}")

    # Best effort: a keep directory that cannot be created only means the Keep
    # button reports an error when pressed, which is not a reason not to start.
    try:
        os.makedirs(KEEP_DIR, exist_ok=True)
    except OSError as exc:
        print(f"  ! cannot create {KEEP_DIR}: {exc}")
    choplog_load()
    print(f"  log    {CHOPLOG_PATH} ({len(_choplog)} chop(s))")

    threading.Thread(target=poll_loop, name="poll", daemon=True).start()
    srv = ThreadingHTTPServer((WALL_HOST, WALL_PORT), WallHandler)
    srv.daemon_threads = True
    print(f"  wall   http://<aggregator>:{WALL_PORT}/")
    print(f"  chops  http://<aggregator>:{WALL_PORT}/log")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()


if __name__ == "__main__":
    if "--check-config" in sys.argv:
        issues = validate_config()
        for item in issues:
            print(f"  * {item}")
        if issues:
            sys.exit(1)
        print(f"{C.get('_path')}: OK")
        print(f"  site  : {SITE}")
        print(f"  nodes : " + ", ".join(f"{n['name']}@{n['address']}"
                                        for n in NODES))
        print(f"  clips : {INCOMING_DIR}")
        print(f"  keep  : {KEEP_DIR}")
        print(f"  log   : {CHOPLOG_PATH} (max {CHOPLOG_MAX})")
        print(f"  wall  : {WALL_HOST}:{WALL_PORT}")
        sys.exit(0)
    main()
