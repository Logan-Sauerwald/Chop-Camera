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
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
            "checked": st["checked"],
        })
    healthy = sum(1 for n in nodes if n["state"] == "healthy")
    return {
        "site": SITE or None,
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nodes_total": len(nodes),
        "nodes_healthy": healthy,
        "incoming_dir": INCOMING_DIR,
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# The wall page
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>chopcam - %(site)s</title>
<style>
  :root { color-scheme: dark; }
  html,body { margin:0; height:100%%; background:#0b0d10; color:#e8ecf1;
              font:13px/1.4 system-ui,-apple-system,Segoe UI,sans-serif; }
  /* The wall owns a whole monitor, so the grid fills whatever is left under
     the header rather than leaving dead space below the tiles. */
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
  /* Solid strip, not a gradient: this is read at a glance from across a
     room, over video that can be any colour. */
  .bar { position:absolute; left:0; right:0; bottom:0; display:flex;
         justify-content:space-between; align-items:baseline; gap:12px;
         padding:5px 9px; background:#000000cc; font-size:13px; z-index:2; }
  .name { font-weight:700; letter-spacing:.03em; white-space:nowrap; }
  .meta { color:#b6bfca; font-variant-numeric:tabular-nums;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .badge { position:absolute; top:6px; right:6px; padding:3px 9px;
           border-radius:99px; font-size:11px; font-weight:700;
           letter-spacing:.05em; text-transform:uppercase; z-index:2; }
  .healthy .badge    { background:#2f6f4f; }
  .degraded .badge   { background:#c58b2f; color:#1a1205; }
  .unreachable .badge{ background:#a33f3f; }
  /* An unreachable node keeps showing its last frame, which reads as
     working. Cover it and say what is wrong. */
  .down { position:absolute; inset:0; display:flex; flex-direction:column;
          align-items:center; justify-content:center; text-align:center;
          gap:6px; padding:16px 16px 34px; background:#0b0d10ee;
          color:#c9d1da; z-index:1; }
  .down .why { color:#8c9099; font-size:12px; max-width:90%%; }
  .empty { padding:24px; color:#9aa4b2; }
  .empty code { color:#e8ecf1; }
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
<script>
// Tiles are built once and then only their status is updated. Rebuilding them
// would tear down each MJPEG connection and restart every stream.
let built = false;

function tile(n) {
  const el = document.createElement('div');
  el.className = 'tile';
  el.id = 'tile-' + n.name;
  el.innerHTML =
    '<img alt="' + n.name + '" src="' + n.stream + '">' +
    '<div class="badge"></div>' +
    '<div class="bar"><span class="name"></span><span class="meta"></span></div>';
  el.querySelector('.name').textContent = n.clip_id;
  return el;
}

function render(s) {
  const grid = document.getElementById('grid');
  document.getElementById('empty').hidden = s.nodes.length > 0;
  if (!built) {
    s.nodes.forEach(n => grid.appendChild(tile(n)));
    built = true;
  }
  document.getElementById('summary').innerHTML =
    '<b>' + s.nodes_healthy + '</b>/' + s.nodes_total + ' nodes healthy' +
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
      if (n.triggers !== null && n.triggers !== undefined)
        meta.push(n.triggers + ' trig');
      if (n.clips_here) meta.push(n.clips_here + ' clips');
    }
    el.querySelector('.meta').textContent = meta.join('  \\u00b7  ');

    // A node that is down still shows its last frame frozen, which reads as
    // working. Say so over the top of it.
    let down = el.querySelector('.down');
    const img = el.querySelector('img');
    if (n.state === 'unreachable') {
      // Also blank the <img>: a failed load renders a broken-image icon,
      // which looks like a bug in the wall rather than a node being down.
      img.style.visibility = 'hidden';
      if (!down) {
        down = document.createElement('div');
        down.className = 'down';
        down.innerHTML = '<div class="what"></div><div class="why"></div>';
        el.appendChild(down);
      }
      down.querySelector('.what').textContent = 'No response from ' + n.address;
      down.querySelector('.why').textContent = n.error || '';
    } else {
      img.style.visibility = 'visible';
      if (down) down.remove();
    }
  });
}

async function refresh() {
  try {
    const r = await fetch('status', {cache: 'no-store'});
    render(await r.json());
  } catch (e) {
    document.getElementById('summary').textContent =
      'aggregator unreachable \\u2014 ' + e;
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

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
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
