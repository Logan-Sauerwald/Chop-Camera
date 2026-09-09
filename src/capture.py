#!/usr/bin/env python3
"""
capture.py -- chopcam node capture service.

Continuously buffers MJPEG frames from a USB camera in RAM. On a trigger, saves
a clip spanning PRE_SECONDS before and POST_SECONDS after the trigger instant.
Also serves a live preview over HTTP for aiming and monitoring.

TRIGGERS (either or both, per config)
  PLC     -- polls one boolean on the PLC, firing on a FALSE->TRUE edge. Both
             Allen-Bradley ControlLogix (EtherNet/IP) and Siemens S7
             (ISO-on-TCP) are supported -- see src/plc.py and PLC_TYPE in the
             config. The PLC needs no new programming; we watch an existing
             output.
  Modbus  -- bench trigger you can fire by hand. Keep it on during
             commissioning to separate "PLC problem" from "camera problem".

Clips are written as MJPEG stream-copy (~0.1 s, near-zero CPU) so capture never
stalls. postprocess.sh converts them to H.264 later, at idle priority.

USAGE
  capture.py                      run the service
  capture.py --test-trigger       connect and watch the trigger bit (any PLC)
  capture.py --list-tags 156N0    list tags matching a substring (ControlLogix)
  capture.py --config PATH        use a specific config file
"""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
# Make sibling modules (plc.py) importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONFIG_SEARCH = [
    os.environ.get("CHOPCAM_CONF"),
    "/etc/chopcam.conf",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chopcam.conf"),
]


def load_config(path=None):
    """Parse the shared KEY="value" config that postprocess.sh also sources."""
    candidates = [path] if path else CONFIG_SEARCH
    for cand in candidates:
        if cand and os.path.isfile(cand):
            cfg = {}
            with open(cand) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    cfg[key.strip()] = val.strip().strip('"').strip("'")
            cfg["_path"] = cand
            return cfg
    raise SystemExit(
        "No config found. Copy chopcam.conf.example to /etc/chopcam.conf.\n"
        "Searched: " + ", ".join(c for c in candidates if c)
    )


_cfg_path = None
if "--config" in sys.argv:
    _cfg_path = sys.argv[sys.argv.index("--config") + 1]
C = load_config(_cfg_path)


def _s(key, default=""):
    return C.get(key, default)


def _i(key, default=0):
    try:
        return int(C.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _b(key, default=False):
    return str(C.get(key, default)).strip().lower() in ("true", "yes", "1", "on")


NODE_NAME = _s("NODE_NAME", "chop1")

PLC_TRIGGER = _b("PLC_TRIGGER", True)
PLC_TYPE    = _s("PLC_TYPE", "controllogix")
PLC_PATH    = _s("PLC_PATH", "10.2.4.1")
TRIGGER_TAG = _s("TRIGGER_TAG")
POLL_HZ     = max(1, _i("POLL_HZ", 30))

MODBUS_TEST_TRIGGER = _b("MODBUS_TEST_TRIGGER", True)
MODBUS_BIND_IP      = _s("MODBUS_BIND_IP", "0.0.0.0")
MODBUS_PORT         = _i("MODBUS_PORT", 5020)
MODBUS_TRIGGER_PDU  = _i("MODBUS_TRIGGER_PDU", 0)

CAMERA_DEVICE = _s("CAMERA_DEVICE", "/dev/video0")
FRAME_WIDTH   = _i("FRAME_WIDTH", 1920)
FRAME_HEIGHT  = _i("FRAME_HEIGHT", 1080)
FPS           = _i("FPS", 120)

LIVE_FEED = _b("LIVE_FEED", True)
LIVE_HOST = _s("LIVE_HOST", "0.0.0.0")
LIVE_PORT = _i("LIVE_PORT", 8080)
LIVE_FPS  = max(1, _i("LIVE_FPS", 15))

PRE_SECONDS  = _i("PRE_SECONDS", 15)
POST_SECONDS = _i("POST_SECONDS", 15)

STATE_DIR  = _s("STATE_DIR", "/var/lib/chopcam")
OUTPUT_DIR = os.path.join(STATE_DIR, "raw")
ENCODE     = _s("CAPTURE_ENCODE", "mjpeg")

# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("chopcam")

# snap7 logs every connect/disconnect at INFO, which would fill the journal
# once the PLC reconnect loop is running. Warnings and errors still come through.
logging.getLogger("snap7").setLevel(logging.WARNING)

# Ring buffer of (monotonic_ts, jpeg_bytes). maxlen self-trims, so RAM is
# bounded. At 120 fps / 30 s this is ~3720 frames, roughly 400 MB.
_BUF_LEN = int(FPS * (PRE_SECONDS + POST_SECONDS)) + FPS
frame_buffer = deque(maxlen=_BUF_LEN)
buffer_lock = threading.Lock()

trigger_q: "Queue[tuple[float, datetime, str]]" = Queue()
_stop = threading.Event()


def fire_trigger(source):
    """Single entry point for every trigger source."""
    trigger_q.put((time.monotonic(), datetime.now(), source))


# ---------------------------------------------------------------------------
# PLC trigger: poll a boolean tag over EtherNet/IP
# ---------------------------------------------------------------------------
def plc_poll_loop():
    """Poll the configured PLC boolean; fire on a FALSE->TRUE (rising) edge.

    PLC-agnostic: plc.make_trigger_source() returns a ControlLogix or Siemens
    driver depending on PLC_TYPE. Opening a connection is expensive (pycomm3
    uploads the controller's tag list), so we open once and read repeatedly,
    reconnecting only on failure.
    """
    from plc import make_trigger_source

    period = 1.0 / POLL_HZ
    while not _stop.is_set():
        source = None
        try:
            source = make_trigger_source(C)
            source.open()
            log.info("PLC connected: %s", source.describe())
            last = False
            while not _stop.is_set():
                t0 = time.monotonic()
                val = bool(source.read_bool())
                if val and not last:
                    log.info("TRIGGER: %s went TRUE", TRIGGER_TAG)
                    fire_trigger("plc")
                last = val
                time.sleep(max(0.0, period - (time.monotonic() - t0)))
        except ValueError as exc:
            # Bad configuration (unknown PLC_TYPE, unparseable address).
            # Retrying won't help, so say so loudly and stop polling.
            log.error("PLC config error: %s -- fix /etc/chopcam.conf "
                      "and restart; PLC trigger disabled", exc)
            return
        except Exception as exc:                      # noqa: BLE001
            log.error("PLC error: %s; retrying in 2s", exc)
            time.sleep(2)
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:                     # noqa: BLE001
                    pass


def test_trigger():
    """Connect once and read the configured boolean. Works for any PLC type."""
    from plc import make_trigger_source
    try:
        source = make_trigger_source(C)
    except ValueError as exc:
        print(f"Config error: {exc}")
        return
    print(f"Target: {source.label}")
    try:
        with source:
            print(f"Connected: {source.describe()}")
            print(f"Current value: {source.read_bool()}")
            print("\nWatching for 10 s -- toggle the bit to confirm "
                  "(Ctrl-C to stop)...")
            last = None
            end = time.monotonic() + 10
            while time.monotonic() < end:
                val = bool(source.read_bool())
                if val != last:
                    print(f"  {datetime.now():%H:%M:%S}  -> {val}")
                    last = val
                time.sleep(1.0 / POLL_HZ)
    except KeyboardInterrupt:
        pass
    except Exception as exc:                          # noqa: BLE001
        print(f"\nCould not read from PLC: {exc}")
        print("If the PLC isn't connected yet, this is expected. If it is:")
        print("  - check `ping " + PLC_PATH.split('/')[0] + "`")
        print("  - ControlLogix: a chassis needs the CPU slot, "
              "e.g. PLC_PATH=\"10.2.4.1/1\"")
        print("  - Siemens: check SIEMENS_RACK/SIEMENS_SLOT (S7-1200/1500 are "
              "usually 0/1, S7-300/400 are 0/2), that PUT/GET communication is")
        print("    permitted on the CPU, and that the DB has 'optimized block "
              "access' turned OFF")


def list_tags(substr=""):
    """List PLC tags. ControlLogix only -- Siemens has no equivalent browse."""
    from plc import make_trigger_source, LogixSource
    try:
        source = make_trigger_source(C)
    except ValueError as exc:
        print(f"Config error: {exc}")
        return
    if not isinstance(source, LogixSource):
        print("--list-tags only works with ControlLogix. Siemens addresses are "
              "absolute (e.g. DB100.DBX0.7) and can't be browsed this way.")
        print("Use --test-trigger to verify the configured address instead.")
        return
    try:
        source.list_tags(substr)
    except Exception as exc:                          # noqa: BLE001
        print(f"Could not reach PLC at {PLC_PATH}: {exc}")
        print("If the PLC isn't connected yet, this is expected.")
        print("If it is: check `ping`, and try a chassis slot path "
              "like \"10.2.4.1/1\" in PLC_PATH.")


# ---------------------------------------------------------------------------
# Modbus bench trigger
# ---------------------------------------------------------------------------
def build_modbus_context():
    from pymodbus.datastore import (ModbusSequentialDataBlock,
                                    ModbusSlaveContext, ModbusServerContext)

    class TriggerCoilBlock(ModbusSequentialDataBlock):
        """Fires on a coil write and acks by resetting the coil."""

        def setValues(self, address, values):
            super().setValues(address, values)
            # The slave context adds 1 to the wire PDU address before calling
            # us, so undo that for logging and matching.
            pdu_start = address - 1
            pdu_end = pdu_start + len(values) - 1
            log.info("coil write: PDU %d..%d = %s", pdu_start, pdu_end, list(values))
            if (pdu_start <= MODBUS_TRIGGER_PDU <= pdu_end
                    and bool(values[MODBUS_TRIGGER_PDU - pdu_start])):
                log.info("TRIGGER: modbus coil PDU %d", MODBUS_TRIGGER_PDU)
                fire_trigger("modbus")
                # ack via the BASE method so we don't recurse
                ModbusSequentialDataBlock.setValues(
                    self, MODBUS_TRIGGER_PDU + 1, [False])

    coils = TriggerCoilBlock.create()
    slave = ModbusSlaveContext(co=coils, di=None, hr=None, ir=None)
    return ModbusServerContext(slaves=slave, single=True)


# ---------------------------------------------------------------------------
# Live preview: MJPEG-over-HTTP, served from frames already in the buffer.
# Never opens the camera a second time, so it cannot cause a
# "Device or resource busy" conflict with the capture thread.
# ---------------------------------------------------------------------------
_LIVE_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>chopcam - live</title>
<style>
  html,body{margin:0;background:#111;height:100%;display:flex;
            align-items:center;justify-content:center;}
  .wrap{position:relative;display:inline-block;line-height:0;}
  img{display:block;max-width:100vw;max-height:100vh;}
  .grid{position:absolute;inset:0;pointer-events:none;}
</style></head>
<body>
  <div class="wrap">
    <img src="/stream" alt="live camera">
    <svg class="grid" viewBox="0 0 100 100" preserveAspectRatio="none">
      <line x1="33.3" y1="0" x2="33.3" y2="100" stroke="#ffffff40" stroke-width=".2"/>
      <line x1="66.6" y1="0" x2="66.6" y2="100" stroke="#ffffff40" stroke-width=".2"/>
      <line x1="0" y1="33.3" x2="100" y2="33.3" stroke="#ffffff40" stroke-width=".2"/>
      <line x1="0" y1="66.6" x2="100" y2="66.6" stroke="#ffffff40" stroke-width=".2"/>
      <line x1="50" y1="45" x2="50" y2="55" stroke="#ff5050e0" stroke-width=".4"/>
      <line x1="45" y1="50" x2="55" y2="50" stroke="#ff5050e0" stroke-width=".4"/>
    </svg>
  </div>
</body></html>"""


class LiveHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):       # silence per-request stderr spam
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_LIVE_PAGE)))
            self.end_headers()
            self.wfile.write(_LIVE_PAGE)
            return

        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            period = 1.0 / LIVE_FPS
            try:
                while not _stop.is_set():
                    t0 = time.monotonic()
                    with buffer_lock:
                        frame = frame_buffer[-1][1] if frame_buffer else None
                    if frame:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(
                            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n")
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                    time.sleep(max(0.0, period - (time.monotonic() - t0)))
            except (BrokenPipeError, ConnectionResetError):
                pass                    # browser tab closed -- fine
            return

        self.send_error(404)


def start_live_feed():
    try:
        srv = ThreadingHTTPServer((LIVE_HOST, LIVE_PORT), LiveHandler)
    except OSError as exc:
        log.error("live feed could not bind :%d (%s)", LIVE_PORT, exc)
        return
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="live", daemon=True).start()
    log.info("live preview on port %d  (http://<pi-ip>:%d/)", LIVE_PORT, LIVE_PORT)


# ---------------------------------------------------------------------------
# Capture thread: ffmpeg -> concatenated JPEG stream -> ring buffer
# ---------------------------------------------------------------------------
_SOI = b"\xff\xd8"      # JPEG start-of-image
_EOI = b"\xff\xd9"      # JPEG end-of-image


def capture_loop():
    """Pull MJPEG frames off the camera and push JPEG bytes into the buffer.

    Frames stay compressed the whole way: ffmpeg copies the camera's native
    MJPEG, we split on JPEG markers, and only the writer ever decodes. Decoding
    here instead would cost ~6 MB per frame and blow out RAM at 120 fps.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", f"{FRAME_WIDTH}x{FRAME_HEIGHT}",
        "-framerate", str(FPS),
        "-i", CAMERA_DEVICE,
        "-c", "copy", "-f", "mjpeg", "pipe:1",
    ]
    while not _stop.is_set():
        log.info("starting camera capture: %s", CAMERA_DEVICE)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            log.error("ffmpeg not found -- sudo apt install ffmpeg")
            return

        buf = bytearray()
        try:
            while not _stop.is_set():
                chunk = proc.stdout.read(65536)
                if not chunk:
                    # A few of these right after boot are normal: USB
                    # enumeration finishes after systemd starts the service.
                    log.warning("camera stream ended; will restart")
                    break
                buf.extend(chunk)
                while True:
                    s = buf.find(_SOI)
                    if s < 0:
                        break
                    e = buf.find(_EOI, s + 2)
                    if e < 0:
                        if s > 0:
                            del buf[:s]     # drop junk before a partial frame
                        break
                    e += 2
                    frame = bytes(buf[s:e])
                    del buf[:e]
                    with buffer_lock:
                        frame_buffer.append((time.monotonic(), frame))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

        if not _stop.is_set():
            time.sleep(2)


# ---------------------------------------------------------------------------
# Writer thread: assemble pre+post window, mux to disk
# ---------------------------------------------------------------------------
def writer_loop():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    recording_until_mono = 0.0

    while not _stop.is_set():
        try:
            t_mono, t_wall, source = trigger_q.get(timeout=0.5)
        except Empty:
            continue

        if t_mono < recording_until_mono:
            log.info("trigger ignored (inside active recording window)")
            continue
        recording_until_mono = t_mono + POST_SECONDS

        log.info("recording [%s]: waiting %ds for post-roll...", source, POST_SECONDS)
        end = t_mono + POST_SECONDS
        while time.monotonic() < end and not _stop.is_set():
            time.sleep(0.1)

        # Cheap: list() copies references to JPEG bytes already in RAM.
        with buffer_lock:
            snap = list(frame_buffer)

        lo, hi = t_mono - PRE_SECONDS, t_mono + POST_SECONDS
        clip = [(ts, jpg) for ts, jpg in snap if lo <= ts <= hi]
        if not clip:
            log.warning("no frames captured for this trigger; skipping")
            continue

        # Real fps from timestamps, so playback speed is right even if the
        # camera delivered slightly fewer frames than requested.
        span = clip[-1][0] - clip[0][0]
        fps = (len(clip) - 1) / span if span > 0 else float(FPS)

        # MJPEG goes in Matroska: MJPEG-in-MP4 is poorly supported by players,
        # and postprocess.sh looks for .mkv. H.264 stays .mp4.
        ext = "mkv" if ENCODE == "mjpeg" else "mp4"
        fname = f"event_{t_wall.strftime('%Y%m%d_%H%M%S')}_{NODE_NAME}.{ext}"
        _mux(clip, fps, os.path.join(OUTPUT_DIR, fname))


def _mux(clip, fps, out_path):
    """Stream frames into ffmpeg's stdin.

    Deliberately NOT b"".join(...): at 120 fps a 30 s clip is ~400 MB, and
    building that as one bytes object doubles peak memory on top of the ring
    buffer. Writing frame by frame keeps the extra allocation near zero.
    """
    if ENCODE == "mjpeg":
        video = ["-c:v", "copy", "-f", "matroska"]
    else:
        video = ["-c:v", "libx264", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4"]

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "mjpeg", "-framerate", f"{fps:.3f}", "-i", "pipe:0",
           *video, out_path]

    # stderr to a temp file, not a pipe: nothing reads a pipe while we're busy
    # writing stdin, so a chatty ffmpeg could otherwise deadlock the writer.
    err = tempfile.TemporaryFile()
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        try:
            for _, jpg in clip:
                proc.stdin.write(jpg)
            proc.stdin.close()
        except BrokenPipeError:
            pass                        # ffmpeg died; rc below reports it
        rc = proc.wait(timeout=300)

        if rc == 0:
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            log.info("saved %s (%d frames, %.1f fps, %.0f MB, %.1fs)",
                     out_path, len(clip), fps, size_mb, time.monotonic() - t0)
        else:
            err.seek(0)
            log.error("ffmpeg mux failed (rc=%d): %s", rc,
                      err.read().decode(errors="replace")[:300])
    except Exception as exc:            # noqa: BLE001
        log.error("mux error: %s", exc)
    finally:
        err.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    log.info("chopcam node %s | config %s | clips -> %s",
             NODE_NAME, C.get("_path"), OUTPUT_DIR)

    threading.Thread(target=capture_loop, name="capture", daemon=True).start()
    threading.Thread(target=writer_loop, name="writer", daemon=True).start()
    if LIVE_FEED:
        start_live_feed()
    if PLC_TRIGGER:
        threading.Thread(target=plc_poll_loop, name="plc", daemon=True).start()

    if MODBUS_TEST_TRIGGER:
        from pymodbus.server import StartAsyncTcpServer
        context = build_modbus_context()
        log.info("modbus test trigger on %s:%d (coil PDU %d)",
                 MODBUS_BIND_IP, MODBUS_PORT, MODBUS_TRIGGER_PDU)
        await StartAsyncTcpServer(context, address=(MODBUS_BIND_IP, MODBUS_PORT))
    else:
        while not _stop.is_set():
            await asyncio.sleep(1)


if __name__ == "__main__":
    if "--test-trigger" in sys.argv:
        test_trigger()
        sys.exit(0)
    if "--list-tags" in sys.argv:
        idx = sys.argv.index("--list-tags")
        list_tags(sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "")
        sys.exit(0)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _stop.set()
        log.info("shutting down")
