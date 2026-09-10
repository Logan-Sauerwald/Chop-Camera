#!/usr/bin/env python3
"""
wall.py -- chopcam aggregator: live preview wall and node status.

Runs on the aggregator Pi 5, driving the monitor. It does three things:

  * serves a grid of live tiles, one per capture node
  * polls every node's /healthz and shows which are actually recording
  * lists the clips that have landed in INCOMING_DIR

The status polling is done HERE rather than in the browser on purpose. The
nodes' HTTP servers send no CORS headers, so page JavaScript cannot read their
/healthz directly; proxying it server-side and serving the aggregate from our
own origin sidesteps that entirely. MJPEG <img> tags are not CORS-restricted,
so the video tiles do come straight from the nodes -- the aggregator never
touches video, which is what keeps it cheap.

Clips arrive by scp from the nodes (postprocess.sh ships them). This process
does not receive them; it only reports what is on disk.

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
_status = {n["name"]: {"state": "unknown", "reachable": False, "health": None,
                       "error": "", "checked": None}
           for n in NODES}
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


def poll_loop():
    while not _stop.is_set():
        for node in NODES:
            if _stop.is_set():
                return
            result = poll_node(node)
            with _status_lock:
                _status[node["name"]] = result
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


def list_clips(limit=None):
    """Newest clips in INCOMING_DIR, newest first."""
    try:
        entries = os.scandir(INCOMING_DIR)
    except OSError:
        return []
    out = []
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
            })
    out.sort(key=lambda c: c["received"], reverse=True)
    return out[:limit] if limit else out


def clips_by_node():
    """Newest clip and total count per node, for the wall."""
    summary = {n["name"]: {"count": 0, "last": None} for n in NODES}
    wanted = {clip_id(n["name"]): n["name"] for n in NODES}
    for clip in list_clips():
        name = wanted.get(clip["clip_id"])
        if name is None:
            continue
        entry = summary[name]
        entry["count"] += 1
        if entry["last"] is None:
            entry["last"] = clip["recorded_utc"] or clip["file"]
    return summary


def newest_clip_for(name):
    """The most recent delivered clip for one node, or None."""
    want = clip_id(name)
    for clip in list_clips():
        if clip["clip_id"] == want:
            return clip
    return None


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
    by_node = clips_by_node()
    nodes = []
    for node in NODES:
        st = snap[node["name"]]
        health = st.get("health") or {}
        camera = health.get("camera") or {}
        plc = health.get("plc") or {}
        newest = newest_clip_for(node["name"])
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
            "last_clip_age_s": (
                round(time.time() - newest["received"]) if newest else None),
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
        "incoming_dir": INCOMING_DIR,
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# Serving clips
# ---------------------------------------------------------------------------
def safe_clip_path(name):
    """Absolute path of a clip inside INCOMING_DIR, or None.

    The name arrives from a URL, so this is the boundary that stops
    /clip/../../etc/passwd. Only a bare filename ending in .mp4 that actually
    resolves inside INCOMING_DIR is served.
    """
    name = urllib.parse.unquote(name or "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if not name.endswith(".mp4"):
        return None
    base = os.path.realpath(INCOMING_DIR)
    path = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(path) != base or not os.path.isfile(path):
        return None
    return path


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
</style></head>
<body>
  <header>
    <h1>chopcam%(site_label)s</h1>
    <div id="summary">connecting...</div>
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
      <span id="pspacer"></span>
      <button id="pclose">Back to live &nbsp;(Esc)</button>
    </div>
    <video id="pvideo" controls playsinline preload="auto"></video>
    <div id="pbottom">
      <button class="rate" id="pplay" style="min-width:5.5em">Pause</button>
      <span class="hint">Speed</span>
      <button class="rate" data-rate="0.1">0.1x</button>
      <button class="rate on" data-rate="0.25">0.25x</button>
      <button class="rate" data-rate="0.5">0.5x</button>
      <button class="rate" data-rate="1">1x</button>
      <span id="pspacer2" style="flex:1 1 auto"></span>
      <a class="dl primary" id="pdlslow" href="#">Download slow motion</a>
      <a class="dl" id="pdlorig" href="#">Original speed</a>
    </div>
  </div>
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
    '<img alt="" src="' + n.stream + '">' +
    '<div class="badge"></div>' +
    '<div class="bar"><span class="name"></span>' +
    '<span class="meta"></span>' +
    '<button class="chop" type="button"></button></div>';
  el.querySelector('.name').textContent = n.clip_id;
  el.querySelector('.chop').addEventListener('click',
    () => openPlayer(n.name));
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

function render(s) {
  const grid = document.getElementById('grid');
  document.getElementById('empty').hidden = s.nodes.length > 0;
  if (!built) {
    s.nodes.forEach(n => grid.appendChild(tile(n)));
    built = true;
  }
  s.nodes.forEach(n => { nodes[n.name] = n; });

  let extra = '';
  if (s.nodes_delayed && s.nodes_delayed.length)
    extra = ' &middot; <b>' + s.nodes_delayed.length + ' delayed</b>';
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
      if (!playing) img.style.visibility = 'hidden';
      if (!down) {
        down = document.createElement('div');
        down.className = 'down';
        down.innerHTML = '<div class="what"></div><div class="why"></div>';
        el.appendChild(down);
      }
      down.querySelector('.what').textContent = 'No response from ' + n.address;
      down.querySelector('.why').textContent = n.error || '';
    } else {
      if (!playing) img.style.visibility = 'visible';
      if (down) down.remove();
    }
  });
}

// ---- player --------------------------------------------------------------
const video = document.getElementById('pvideo');
let rate = 0.25;

function setRate(r) {
  rate = r;
  video.playbackRate = r;
  document.querySelectorAll('.rate[data-rate]').forEach(b =>
    b.classList.toggle('on', parseFloat(b.dataset.rate) === r));
}
document.querySelectorAll('.rate[data-rate]').forEach(b =>
  b.addEventListener('click', () => setRate(parseFloat(b.dataset.rate))));

// The browser's own controls fade out after a few seconds, and the wall may be
// on a monitor with nothing to move a pointer with. This button never hides.
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

function openPlayer(name) {
  const n = nodes[name];
  if (!n || !n.last_clip_file) return;
  playing = name;
  stopLiveStreams(true);
  const url = 'clip/' + encodeURIComponent(n.last_clip_file);
  document.getElementById('ptitle').textContent = n.clip_id;
  document.getElementById('pwhen').textContent =
    (n.last_clip_utc ? n.last_clip_utc.replace('T', ' ').replace('+00:00', 'Z')
                     : n.last_clip_file) +
    (n.last_clip_age_s !== null ? '  \u00b7  ' + ago(n.last_clip_age_s) + ' ago' : '');
  document.getElementById('pdlslow').href = url + '?slowmo=1';
  document.getElementById('pdlorig').href = url + '?download=1';
  document.getElementById('player').hidden = false;
  video.src = url;
  setRate(rate);
  video.play().catch(() => {});   // autoplay may be blocked; controls still work
  syncPlayBtn();
}

function closePlayer() {
  playing = null;
  video.pause();
  video.removeAttribute('src');
  video.load();                   // drop the buffered clip from memory
  document.getElementById('player').hidden = true;
  stopLiveStreams(false);
}
document.getElementById('pclose').addEventListener('click', closePlayer);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && playing) closePlayer();
  if (e.key === ' ' && playing) {
    e.preventDefault();
    video.paused ? video.play() : video.pause();
  }
});

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


def wall_page():
    return (_PAGE % {
        "site": SITE or "wall",
        "site_label": f" &middot; {SITE}" if SITE else "",
        "poll_ms": POLL_SECONDS * 1000,
    }).encode("utf-8")


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
        elif path == "/clips":
            self._json({"incoming_dir": INCOMING_DIR,
                        "clips": list_clips(CLIP_LIST_LIMIT)})
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
    for problem in validate_config():
        print(f"  ! {problem}")

    threading.Thread(target=poll_loop, name="poll", daemon=True).start()
    srv = ThreadingHTTPServer((WALL_HOST, WALL_PORT), WallHandler)
    srv.daemon_threads = True
    print(f"  wall   http://<aggregator>:{WALL_PORT}/")
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
        print(f"  wall  : {WALL_HOST}:{WALL_PORT}")
        sys.exit(0)
    main()
