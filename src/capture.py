#!/usr/bin/env python3
"""
capture.py -- chopcam node capture service.

Continuously buffers MJPEG frames from a USB camera in RAM. On a trigger, saves
a clip spanning PRE_SECONDS before and POST_SECONDS after the trigger instant.
Also serves a live preview and a health endpoint over HTTP.

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
  capture.py                        run the service
  capture.py --test-trigger [SECS]  connect and characterise the trigger bit
                                    (any PLC family; 0 = until Ctrl-C)
  capture.py --list-tags [SUBSTR]   list tags matching a substring (ControlLogix)
  capture.py --check-config         validate the config and exit
  capture.py --config PATH          use a specific config file

HTTP (port LIVE_PORT)
  /            live preview page with framing grid
  /stream      MJPEG stream
  /healthz     JSON node status; 200 healthy, 503 degraded
"""

import asyncio
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue, Empty

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
# Make sibling modules (plc.py) importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Parsing lives in chopcam_config so the aggregator reads the same format the
# same way; see the note there about bash and Python having to agree.
from chopcam_config import (                            # noqa: E402
    CONFIG_SEARCH, load_config, parse_config_value,
)

__all__ = ["CONFIG_SEARCH", "load_config", "parse_config_value"]


def _argv_value(flag, default=None):
    """Value following `flag` on the command line, or `default`."""
    if flag not in sys.argv:
        return default
    idx = sys.argv.index(flag) + 1
    if idx >= len(sys.argv) or sys.argv[idx].startswith("--"):
        return default
    return sys.argv[idx]


C = load_config(_argv_value("--config"))

# Keys whose value came from a built-in default rather than the config file.
# Logged at startup so a typo'd or missing key is visible during commissioning.
_defaulted = []


def _s(key, default=""):
    if key not in C:
        _defaulted.append(key)
        return default
    return C[key]


def _i(key, default=0):
    raw = _s(key, default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        log_early(f"{key}={raw!r} is not a whole number; using {default}")
        return int(default)


def _b(key, default=False):
    return str(_s(key, default)).strip().lower() in ("true", "yes", "1", "on")


_early_warnings = []


def log_early(msg):
    _early_warnings.append(msg)


# Install identity. Every Pi at one aggregator shares SITE; only NODE_NAME
# differs. Keeping them separate is what makes a per-install conf template
# possible -- copy it to each Pi and change NODE_NAME, PLC_PATH, TRIGGER_TAG
# and NODE_IP, nothing else.
SITE      = _s("SITE", "")
NODE_NAME = _s("NODE_NAME", "")
# What actually labels a clip. Unique plant-wide once SITE is set.
CLIP_ID   = f"{SITE}-{NODE_NAME}" if SITE else NODE_NAME

PLC_TRIGGER = _b("PLC_TRIGGER", True)
PLC_TYPE    = _s("PLC_TYPE", "controllogix")
PLC_PATH    = _s("PLC_PATH", "")
TRIGGER_TAG = _s("TRIGGER_TAG", "")
POLL_HZ     = max(1, _i("POLL_HZ", 30))

MODBUS_TEST_TRIGGER = _b("MODBUS_TEST_TRIGGER", True)
MODBUS_BIND_IP      = _s("MODBUS_BIND_IP", "127.0.0.1")
MODBUS_PORT         = _i("MODBUS_PORT", 5020)
MODBUS_TRIGGER_PDU  = _i("MODBUS_TRIGGER_PDU", 0)

CAMERA_DEVICE   = _s("CAMERA_DEVICE", "/dev/video0")
CAMERA_CONTROLS = _s("CAMERA_CONTROLS", "")
FRAME_WIDTH   = _i("FRAME_WIDTH", 1920)
FRAME_HEIGHT  = _i("FRAME_HEIGHT", 1080)
FPS           = max(1, _i("FPS", 120))

LIVE_FEED = _b("LIVE_FEED", True)
LIVE_HOST = _s("LIVE_HOST", "0.0.0.0")
LIVE_PORT = _i("LIVE_PORT", 8080)
LIVE_FPS  = max(1, _i("LIVE_FPS", 15))

PRE_SECONDS  = max(1, _i("PRE_SECONDS", 15))
POST_SECONDS = max(1, _i("POST_SECONDS", 15))

SHIP_ENABLED = _b("SHIP_ENABLED", False)
AGG_USER     = _s("AGG_USER", "")
AGG_IP       = _s("AGG_IP", "")
AGG_DIR      = _s("AGG_DIR", "")
AGG_PORT     = _i("AGG_PORT", 22)

STATE_DIR  = _s("STATE_DIR", "/var/lib/chopcam")
OUTPUT_DIR = os.path.join(STATE_DIR, "raw")
# postprocess.sh owns these; capture.py only counts what is in them, which is
# enough to say where a chop is in the pipeline without any plumbing between
# the two processes.
ENCODED_DIR = os.path.join(STATE_DIR, "encoded")
CLIP_TIMESTAMP = _s("CLIP_TIMESTAMP", "utc").strip().lower()

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

# One second of slack past PRE+POST: the writer snapshots the buffer
# POST_SECONDS after the trigger, so the oldest frame it still needs is
# PRE+POST seconds old by then.
BUFFER_SECONDS = PRE_SECONDS + POST_SECONDS + 1

# Hard memory ceiling for the ring. Defaults to the window's worth of frames at
# 20 MB/s -- comfortably above the 118 Mbps (14.75 MB/s) peak measured on this
# camera -- so in normal running the TIME bound is what trims the buffer and
# this never binds. It exists to keep a fatter-than-expected stream from
# pushing the service into systemd's MemoryMax.
BUFFER_MAX_MB = _i("BUFFER_MAX_MB", max(64, int(BUFFER_SECONDS * 20)))


class FrameRing:
    """Ring buffer of (monotonic_ts, jpeg_bytes), bounded by TIME and BYTES.

    Bounding by time is the point. The buffer used to be a deque with
    maxlen=FPS*(PRE+POST), which assumed the configured FPS matched what the
    camera actually delivered. Set FPS below the real rate and the deque held
    fewer seconds than PRE+POST, so the pre-roll was silently truncated -- the
    clip looked fine and simply did not reach back far enough to show what led
    into the chop. Trimming on the timestamps we already store removes the
    assumption: the window is PRE+POST+1 seconds whatever rate the camera runs
    at, and FPS is only a request to the camera.

    The byte ceiling is the backstop that maxlen used to provide. When it is
    what evicts a frame -- i.e. the stream is fatter than the budget allows --
    the pre-roll really is short, so that is reported rather than hidden.
    """

    def __init__(self, seconds, max_bytes):
        self.seconds = seconds
        self.max_bytes = max_bytes
        self._frames = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self.memory_evictions = 0        # frames dropped while still in-window
        self._last_warned = 0.0

    def _evict(self):
        self._bytes -= len(self._frames.popleft()[1])

    def append(self, ts, jpg):
        with self._lock:
            self._frames.append((ts, jpg))
            self._bytes += len(jpg)

            cutoff = ts - self.seconds
            while self._frames and self._frames[0][0] < cutoff:
                self._evict()

            # Anything evicted below here was still inside the time window.
            over = 0
            while self._bytes > self.max_bytes and len(self._frames) > 1:
                self._evict()
                over += 1
            if over:
                self.memory_evictions += over
                if ts - self._last_warned > 300:
                    self._last_warned = ts
                    log.warning(
                        "ring buffer hit its %d MB ceiling and is dropping "
                        "frames that are still inside the %.0fs window -- the "
                        "pre-roll will be short. The stream is fatter than "
                        "budgeted (raise BUFFER_MAX_MB, and MemoryMax= in "
                        "chopcam.service to match), or PRE_SECONDS/"
                        "POST_SECONDS are too long for this Pi.",
                        self.max_bytes // (1024 * 1024), self.seconds)

    def snapshot(self):
        """Copy of the current contents. Copies references, not JPEG bytes."""
        with self._lock:
            return list(self._frames)

    def latest(self):
        with self._lock:
            return self._frames[-1] if self._frames else None

    def stats(self):
        with self._lock:
            n = len(self._frames)
            oldest = self._frames[0][0] if n else 0.0
            newest = self._frames[-1][0] if n else 0.0
            return {"frames": n, "bytes": self._bytes,
                    "seconds": (newest - oldest) if n > 1 else 0.0,
                    "memory_evictions": self.memory_evictions}

    def clear(self):
        with self._lock:
            self._frames.clear()
            self._bytes = 0

    def __len__(self):
        with self._lock:
            return len(self._frames)


frame_buffer = FrameRing(BUFFER_SECONDS, BUFFER_MAX_MB * 1024 * 1024)

trigger_q: "Queue[tuple[float, datetime, str]]" = Queue()
_stop = threading.Event()

# ---------------------------------------------------------------------------
# Node status, published at /healthz so the aggregator can tell a live node
# from a wedged one. Every field is cheap to update from the hot paths.
# ---------------------------------------------------------------------------
_status = {
    "camera_state": "starting",
    "camera_last_frame_mono": 0.0,
    "camera_frames": 0,
    "camera_restarts": 0,
    "plc_state": "disabled",
    "plc_detail": "",
    "plc_reads": 0,
    "plc_errors": 0,
    "plc_poll_hz": 0.0,
    "modbus_state": "disabled",
    "modbus_detail": "",
    "trigger_count": 0,
    "trigger_last_utc": None,
    "trigger_last_source": None,
    "clips_written": 0,
    "clips_failed": 0,
    "clip_last": None,
}
_status_lock = threading.Lock()
_started_mono = time.monotonic()


def set_status(**kw):
    with _status_lock:
        _status.update(kw)


def bump_status(key, by=1):
    with _status_lock:
        _status[key] = _status.get(key, 0) + by


def fire_trigger(source):
    """Single entry point for every trigger source."""
    t_wall = datetime.now(timezone.utc)
    trigger_q.put((time.monotonic(), t_wall, source))
    with _status_lock:
        _status["trigger_count"] += 1
        _status["trigger_last_utc"] = t_wall.isoformat(timespec="seconds")
        _status["trigger_last_source"] = source


# ---------------------------------------------------------------------------
# Config validation -- fail fast and loudly, before the camera is even opened.
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Addresses that appear in the shipped documentation. Finding one in a live
# config usually means it was copied and not edited.
_PLACEHOLDER_HOSTS = {"10.2.4.1", "192.168.0.1", "127.0.0.1"}


def validate_config():
    """Return a list of fatal configuration problems (empty == good)."""
    problems = []

    if not NODE_NAME:
        problems.append(
            "NODE_NAME is not set. Every clip filename carries it, so two nodes "
            "sharing a name (or an unset one) produce footage you cannot trace "
            "back to a chop point. Set it to this node's name, e.g. \"chop1\"."
        )
    elif not _NAME_RE.match(NODE_NAME):
        problems.append(
            f"NODE_NAME={NODE_NAME!r} contains characters that are not safe in a "
            "filename. Use letters, digits, dot, dash or underscore only."
        )

    if SITE and not _NAME_RE.match(SITE):
        problems.append(
            f"SITE={SITE!r} contains characters that are not safe in a "
            "filename. Use letters, digits, dot, dash or underscore only."
        )

    if CLIP_TIMESTAMP not in ("utc", "local"):
        problems.append(
            f"CLIP_TIMESTAMP={CLIP_TIMESTAMP!r} must be \"utc\" or \"local\"."
        )

    if PLC_TRIGGER:
        # Builds the driver object (which parses the address) without
        # connecting, so a bad PLC_TYPE or Siemens address is caught here
        # rather than by the poll thread three seconds later.
        try:
            from plc import make_trigger_source
            make_trigger_source(C)
        except ValueError as exc:
            problems.append(f"PLC config: {exc}")
        except ImportError as exc:                    # pragma: no cover
            problems.append(f"PLC driver import failed: {exc}")

    if SHIP_ENABLED:
        # Shipping to the wrong host is worse than not shipping: the node
        # deletes its local copy once delivery verifies, so a half-configured
        # target is a way to lose footage.
        for key, val in (("AGG_USER", AGG_USER), ("AGG_IP", AGG_IP),
                         ("AGG_DIR", AGG_DIR)):
            if not val:
                problems.append(
                    f"SHIP_ENABLED is true but {key} is not set. Set the "
                    f"aggregator account, address and destination folder for "
                    f"THIS machine's setup -- they differ between installs."
                )

    if not PLC_TRIGGER and not MODBUS_TEST_TRIGGER:
        problems.append(
            "Both PLC_TRIGGER and MODBUS_TEST_TRIGGER are false -- nothing can "
            "ever start a recording. Enable at least one."
        )

    return problems


def _cgroup_memory_limit_bytes():
    """The cgroup memory ceiling this process runs under, or None.

    Under systemd this is MemoryMax= from chopcam.service. Reading it lets the
    service say up front that its ring buffer budget does not fit, instead of
    being OOM-killed at the first busy scene.
    """
    for path in ("/sys/fs/cgroup/memory.max",                     # cgroup v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):  # cgroup v1
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw in ("max", ""):
            return None
        try:
            val = int(raw)
        except ValueError:
            continue
        if 0 < val < (1 << 62):     # v1 writes a huge sentinel for "no limit"
            return val
    return None


def _clock_is_synchronised():
    """True if NTP says the clock is good, or if we cannot tell."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True          # not systemd, or no timedatectl -- do not nag
    if out.returncode != 0:
        return True
    return out.stdout.strip().lower() != "no"


def config_warnings():
    """Non-fatal problems worth saying out loud at startup."""
    warnings = []
    budget = BUFFER_MAX_MB * 1024 * 1024
    limit = _cgroup_memory_limit_bytes()
    if limit and budget > limit * 0.6:
        warnings.append(
            f"BUFFER_MAX_MB={BUFFER_MAX_MB} allows a ring buffer of "
            f"{budget / 1048576:.0f} MB, but this service is capped at "
            f"{limit / 1048576:.0f} MB (MemoryMax= in chopcam.service). Leave "
            f"room for the interpreter and ffmpeg: raise MemoryMax to at least "
            f"{int(budget / 1048576 / 0.6)}M, or shorten "
            f"PRE_SECONDS/POST_SECONDS."
        )
    # Every install is on a different machine with its own subnet, so an
    # address carried over from another node's config is the likeliest way to
    # end up pointing at a PLC that exists but is not this chop point's.
    if PLC_TRIGGER and PLC_PATH:
        host = str(PLC_PATH).split("/")[0]
        if host in _PLACEHOLDER_HOSTS:
            warnings.append(
                f"PLC_PATH={PLC_PATH!r} is the address from the shipped "
                f"example, not necessarily this machine's PLC. Confirm it "
                f"with:  capture.py --test-trigger"
            )
    if MODBUS_TEST_TRIGGER and MODBUS_PORT < 1024:
        warnings.append(
            f"MODBUS_PORT={MODBUS_PORT} is privileged (below 1024). The "
            "service runs as a normal user, so binding needs "
            "'AmbientCapabilities=CAP_NET_BIND_SERVICE' in chopcam.service "
            "(there is a commented line for it). Use 502 only if the PLC "
            "pushes the trigger to this Pi."
        )

    # Clip names and metadata are UTC, which only lines cameras up if every
    # node agrees on the time. One unsynchronised Pi puts its clips in the
    # wrong place on the review timeline and nothing about the file says so.
    if not _clock_is_synchronised():
        warnings.append(
            "the system clock is not NTP-synchronised. Clip timestamps are "
            "UTC and are what line several cameras up on the same chop, so "
            "an unsynchronised node produces footage that cannot be "
            "correlated. Check with: timedatectl"
        )

    expected = BUFFER_SECONDS * 14.75      # measured 118 Mbps peak
    if BUFFER_MAX_MB < expected:
        warnings.append(
            f"BUFFER_MAX_MB={BUFFER_MAX_MB} is below the ~{expected:.0f} MB a "
            f"{BUFFER_SECONDS}s window needs at the measured 118 Mbps peak. "
            "The pre-roll will be short on detailed scenes."
        )
    return warnings


def log_effective_config():
    """One block in the journal saying what the service actually believes.

    Commissioning five nodes from copies of one config file makes 'which value
    did this node really load' the most common question; this answers it
    without anyone reading the file over SSH.
    """
    log.info("chopcam node %s | config %s | clips -> %s",
             CLIP_ID, C.get("_path"), OUTPUT_DIR)
    log.info("  camera : %s %dx%d @ %d fps requested",
             CAMERA_DEVICE, FRAME_WIDTH, FRAME_HEIGHT, FPS)
    log.info("  buffer : %ds window, %d MB ceiling  (~%.0f MB expected at "
             "120 fps / 118 Mbps)",
             BUFFER_SECONDS, BUFFER_MAX_MB, BUFFER_SECONDS * 14.75)
    log.info("  clip   : -%ds / +%ds, timestamps=%s",
             PRE_SECONDS, POST_SECONDS, CLIP_TIMESTAMP)
    if PLC_TRIGGER:
        log.info("  plc    : %s %s tag %s @ %d Hz",
                 PLC_TYPE, PLC_PATH, TRIGGER_TAG, POLL_HZ)
    else:
        log.info("  plc    : disabled")
    log.info("  modbus : %s", f"{MODBUS_BIND_IP}:{MODBUS_PORT} "
             f"coil PDU {MODBUS_TRIGGER_PDU}" if MODBUS_TEST_TRIGGER else "disabled")
    for msg in _early_warnings + config_warnings():
        log.warning("config: %s", msg)
    # Keys silently taking a built-in default are usually typos in the file.
    interesting = [k for k in _defaulted
                   if k in ("NODE_NAME", "PLC_TYPE", "PLC_PATH", "TRIGGER_TAG",
                            "POLL_HZ", "FPS", "PRE_SECONDS", "POST_SECONDS",
                            "STATE_DIR")]
    if interesting:
        log.warning("config: not set in %s, using defaults: %s",
                    C.get("_path"), ", ".join(sorted(set(interesting))))


# ---------------------------------------------------------------------------
# PLC trigger: poll a boolean tag
# ---------------------------------------------------------------------------
def plc_poll_loop():
    """Poll the configured PLC boolean; fire on a FALSE->TRUE (rising) edge.

    PLC-agnostic: plc.make_trigger_source() returns whichever driver PLC_TYPE
    selects. Opening a connection is expensive (pycomm3 uploads the
    controller's tag list), so we open once and read repeatedly, reconnecting
    with backoff only on failure.
    """
    from plc import make_trigger_source

    period = 1.0 / POLL_HZ
    backoff = 2.0
    while not _stop.is_set():
        source = None
        try:
            set_status(plc_state="connecting", plc_detail=f"{PLC_TYPE} {PLC_PATH}")
            source = make_trigger_source(C)
            source.open()
            log.info("PLC connected: %s", source.describe())
            set_status(plc_state="connected", plc_detail=source.describe())
            backoff = 2.0

            # Seed the edge detector from the CURRENT value instead of assuming
            # FALSE. A latched bit that is already TRUE when we (re)connect is
            # not a new chop -- treating it as one produced a phantom clip after
            # every network blip.
            last = bool(source.read_bool())
            bump_status("plc_reads")
            if last:
                log.info("%s is already TRUE at connect; waiting for the next "
                         "rising edge", TRIGGER_TAG)

            polls = 0
            slow_polls = 0
            window_start = time.monotonic()
            last_rate_warning = 0.0

            while not _stop.is_set():
                t0 = time.monotonic()
                val = bool(source.read_bool())
                elapsed = time.monotonic() - t0
                polls += 1
                bump_status("plc_reads")

                if val and not last:
                    log.info("TRIGGER: %s went TRUE", TRIGGER_TAG)
                    fire_trigger("plc")
                last = val

                # A read that outlasts the poll period means the achieved rate
                # is set by the PLC, not by POLL_HZ -- and a chop pulse shorter
                # than the real interval gets missed silently.
                if elapsed > period:
                    slow_polls += 1
                time.sleep(max(0.0, period - elapsed))

                now = time.monotonic()
                if now - window_start >= 30.0:
                    achieved = polls / (now - window_start)
                    set_status(plc_poll_hz=round(achieved, 1))
                    # Once per connection, then hourly. A PLC that is
                    # permanently slower than POLL_HZ is a standing condition,
                    # not news every 30 seconds -- and the journal is where
                    # real faults have to stay visible.
                    if achieved < POLL_HZ * 0.8 and (
                            last_rate_warning == 0.0
                            or now - last_rate_warning > 3600):
                        last_rate_warning = now
                        log.warning(
                            "PLC polling at %.1f Hz, configured %d Hz "
                            "(%d/%d reads took longer than the %.0f ms poll "
                            "period). Pulses shorter than %.0f ms may be missed "
                            "-- reduce POLL_HZ to something honest, or ask "
                            "controls to latch the trigger bit. "
                            "/healthz reports the live rate.",
                            achieved, POLL_HZ, slow_polls, polls, period * 1000,
                            1000.0 / max(achieved, 0.001))
                    polls = 0
                    slow_polls = 0
                    window_start = now

        except ValueError as exc:
            # Bad configuration (unknown PLC_TYPE, unparseable address).
            # Retrying won't help, so say so loudly and stop polling.
            log.error("PLC config error: %s -- fix %s and restart; "
                      "PLC trigger disabled", exc, C.get("_path"))
            set_status(plc_state="config-error", plc_detail=str(exc))
            return
        except ImportError as exc:
            log.error("PLC driver unavailable: %s -- PLC trigger disabled", exc)
            set_status(plc_state="driver-missing", plc_detail=str(exc))
            return
        except Exception as exc:                      # noqa: BLE001
            bump_status("plc_errors")
            set_status(plc_state="error", plc_detail=str(exc)[:200])
            log.error("PLC error: %s; retrying in %.0fs", exc, backoff)
            _stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)          # stop hammering a dead PLC
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:                     # noqa: BLE001
                    pass


def test_trigger(duration=30.0):
    """Connect and characterise the trigger bit. Works for any PLC family.

    Beyond "can I read it", this answers the two questions that decide whether
    the system will actually catch a chop:
      * is the output pulse longer than our poll interval, and
      * can we really poll at POLL_HZ against this PLC.
    """
    from plc import make_trigger_source
    try:
        source = make_trigger_source(C)
    except ValueError as exc:
        print(f"Config error: {exc}")
        return 2

    period = 1.0 / POLL_HZ
    print(f"Target : {source.label}")
    print(f"Polling: {POLL_HZ} Hz ({period * 1000:.0f} ms interval)")

    latencies = []
    rising = []          # monotonic time of each FALSE->TRUE
    pulses = []          # seconds each TRUE lasted
    t_rise = None
    # Bound before the try: Ctrl-C during connect must not become a NameError.
    start = time.monotonic()

    try:
        with source:
            print(f"Connected: {source.describe()}")
            val = bool(source.read_bool())
            print(f"Current value: {val}")
            if duration <= 0:
                print("\nWatching until Ctrl-C -- toggle the bit / run a chop...\n")
            else:
                print(f"\nWatching for {duration:.0f} s -- toggle the bit / run "
                      f"a chop (Ctrl-C to stop early)...\n")

            last = val
            if last:
                t_rise = time.monotonic()
            start = time.monotonic()
            while duration <= 0 or (time.monotonic() - start) < duration:
                t0 = time.monotonic()
                val = bool(source.read_bool())
                latencies.append(time.monotonic() - t0)
                now = time.monotonic()
                if val != last:
                    if val:
                        t_rise = now
                        rising.append(now)
                        print(f"  {datetime.now():%H:%M:%S.%f}"[:-3] + "   -> TRUE")
                    else:
                        width = (now - t_rise) if t_rise else 0.0
                        if t_rise:
                            pulses.append(width)
                        print(f"  {datetime.now():%H:%M:%S.%f}"[:-3]
                              + f"   -> FALSE   (TRUE for {width * 1000:.0f} ms)")
                    last = val
                time.sleep(max(0.0, period - (time.monotonic() - t0)))
            span = time.monotonic() - start
    except KeyboardInterrupt:
        span = max(time.monotonic() - start, 1e-6)
        print("\n(stopped)")
    except Exception as exc:                          # noqa: BLE001
        print(f"\nCould not read from PLC: {exc}")
        print("If the PLC isn't connected yet, this is expected. If it is:")
        print("  - check `ping " + str(PLC_PATH).split('/')[0] + "`")
        print("  - ControlLogix: a chassis needs the CPU slot, "
              "e.g. PLC_PATH=\"10.2.4.1/1\"")
        print("  - ControlLogix: confirm the tag name with --list-tags")
        print("  - Siemens: check SIEMENS_RACK/SIEMENS_SLOT (S7-1200/1500 are "
              "usually 0/1, S7-300/400 are 0/2), that PUT/GET communication is")
        print("    permitted on the CPU, and that the DB has 'optimized block "
              "access' turned OFF")
        return 1

    # ---- summary -----------------------------------------------------------
    print("\n--- summary " + "-" * 52)
    if latencies:
        achieved = len(latencies) / span
        print(f"polls        : {len(latencies)} in {span:.1f} s "
              f"-> {achieved:.1f} Hz achieved (POLL_HZ={POLL_HZ})")
        print(f"read latency : min {min(latencies) * 1000:.1f} ms  "
              f"median {statistics.median(latencies) * 1000:.1f} ms  "
              f"max {max(latencies) * 1000:.1f} ms")
    print(f"rising edges : {len(rising)}")
    if len(rising) > 1:
        gaps = [b - a for a, b in zip(rising, rising[1:])]
        print(f"edge spacing : min {min(gaps):.1f} s  max {max(gaps):.1f} s")
    if pulses:
        print(f"pulse width  : min {min(pulses) * 1000:.0f} ms  "
              f"max {max(pulses) * 1000:.0f} ms")

    # Margin has to be judged against the interval we ACHIEVED, not the one we
    # asked for. A PLC that answers in 50 ms makes POLL_HZ=30 a 20 Hz poll, and
    # quoting the configured 33 ms would overstate the headroom by 1.5x.
    effective = period
    if latencies:
        effective = max(period, statistics.median(latencies))

    print()
    ok = True
    if not rising:
        ok = False
        print("VERDICT: no rising edge seen.")
        print("  The connection works, but the bit never went FALSE->TRUE.")
        print("  Either nothing chopped during the window, or this is the wrong")
        print("  address. ControlLogix: try --list-tags to confirm the exact")
        print("  name (output tags are often ...:O.Data.7, not ...:O.7).")
    elif pulses:
        shortest = min(pulses)
        margin = shortest / effective
        actual = " actual" if effective > period * 1.1 else ""
        if margin < 2.0:
            ok = False
            print(f"VERDICT: MARGINAL -- shortest pulse {shortest * 1000:.0f} ms "
                  f"is only {margin:.1f}x the {effective * 1000:.0f} ms{actual} "
                  f"poll interval.")
            print("  Chops will be missed intermittently. Raise POLL_HZ to at "
                  f"least {int(5 / shortest) + 1} (if the PLC can answer that "
                  "fast), or ask controls to latch the bit for >250 ms.")
        else:
            print(f"VERDICT: OK -- shortest pulse {shortest * 1000:.0f} ms is "
                  f"{margin:.0f}x the {effective * 1000:.0f} ms{actual} poll "
                  f"interval.")
    else:
        print("VERDICT: saw a rising edge but no falling edge -- the bit is "
              "still TRUE.")
        print("  If it is latched and never clears, every poll after the first "
              "stays TRUE and only the first chop triggers.")

    if latencies and max(latencies) > period:
        print(f"\nNOTE: the slowest read took {max(latencies) * 1000:.0f} ms, "
              f"longer than the {period * 1000:.0f} ms poll interval. POLL_HZ "
              "is optimistic for this PLC -- the real ceiling is about "
              f"{1.0 / statistics.median(latencies):.0f} Hz.")
    return 0 if ok else 1


def list_tags(substr=""):
    """List PLC tags. ControlLogix only -- Siemens has no equivalent browse."""
    from plc import make_trigger_source, LogixSource
    try:
        source = make_trigger_source(C)
    except ValueError as exc:
        print(f"Config error: {exc}")
        return 2
    if not isinstance(source, LogixSource):
        print("--list-tags only works with ControlLogix. Siemens addresses are "
              "absolute (e.g. DB100.DBX0.7) and can't be browsed this way.")
        print("Use --test-trigger to verify the configured address instead.")
        return 2
    try:
        source.list_tags(substr)
        return 0
    except Exception as exc:                          # noqa: BLE001
        print(f"Could not reach PLC at {PLC_PATH}: {exc}")
        print("If the PLC isn't connected yet, this is expected.")
        print("If it is: check `ping`, and try a chassis slot path "
              "like \"10.2.4.1/1\" in PLC_PATH.")
        return 1


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


def modbus_preflight():
    """Return None if the Modbus address is bindable, else why it is not.

    pymodbus does NOT fail loudly here: on a bind error it logs one warning and
    then prints "Server listening.", so a dead trigger looks like a live one.
    Binding it ourselves first turns that into an answer we can report.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Deliberately no SO_REUSEADDR: we want a genuine conflict to show up.
        sock.bind((MODBUS_BIND_IP, MODBUS_PORT))
        return None
    except PermissionError:
        return (f"port {MODBUS_PORT} is privileged (below 1024) and this "
                f"service does not run as root. Either add "
                f"'AmbientCapabilities=CAP_NET_BIND_SERVICE' to "
                f"chopcam.service (see the commented line there), or use a "
                f"port above 1024.")
    except OSError as exc:
        return f"cannot bind {MODBUS_BIND_IP}:{MODBUS_PORT} -- {exc}"
    finally:
        sock.close()


async def _idle():
    while not _stop.is_set():
        await asyncio.sleep(1)


async def serve_modbus():
    """Run the Modbus server, but never let its failure stop capture.

    Capture is the part that must not fail. A busy port, a missing pymodbus or
    a server that dies later are all reasons to lose the Modbus trigger, and
    none of them is a reason to lose the recording.
    """
    def fail(detail, hint=""):
        log.error("modbus trigger DISABLED: %s", detail)
        if hint:
            log.error("  %s", hint)
        if not PLC_TRIGGER:
            log.error("  This is the ONLY trigger configured -- nothing can "
                      "start a recording until it is fixed.")
        set_status(modbus_state="error", modbus_detail=str(detail)[:200])

    problem = modbus_preflight()
    if problem:
        fail(problem)
        await _idle()
        return

    try:
        from pymodbus.server import StartAsyncTcpServer
    except ImportError as exc:
        fail(f"pymodbus is not installed ({exc})",
             "Fix: pip install 'pymodbus>=3.8,<3.9'  "
             "(3.9+ rewrote the datastore and the callback-datablock pattern "
             "this trigger uses no longer works there)")
        await _idle()
        return

    try:
        context = build_modbus_context()
        log.info("modbus trigger listening on %s:%d "
                 "(coil PDU %d -- a PLC writing \"coil 00001\" means PDU 0)",
                 MODBUS_BIND_IP, MODBUS_PORT, MODBUS_TRIGGER_PDU)
        set_status(modbus_state="listening",
                   modbus_detail=f"{MODBUS_BIND_IP}:{MODBUS_PORT} "
                                 f"PDU {MODBUS_TRIGGER_PDU}")
        await StartAsyncTcpServer(context, address=(MODBUS_BIND_IP, MODBUS_PORT))
        fail("modbus server exited")
    except Exception as exc:                          # noqa: BLE001
        fail(f"modbus server error: {exc}")
    await _idle()


# ---------------------------------------------------------------------------
# Live preview: MJPEG-over-HTTP, served from frames already in the buffer.
# Never opens the camera a second time, so it cannot cause a
# "Device or resource busy" conflict with the capture thread.
# ---------------------------------------------------------------------------
def _live_page():
    """Preview page. Carries the node name so a five-tile wall is readable."""
    return ("""<!doctype html>
<html><head><meta charset="utf-8"><title>chopcam - %(node)s</title>
<style>
  html,body{margin:0;background:#111;height:100%%;display:flex;
            align-items:center;justify-content:center;}
  .wrap{position:relative;display:inline-block;line-height:0;}
  img{display:block;max-width:100vw;max-height:100vh;}
  .grid{position:absolute;inset:0;pointer-events:none;}
  .tag{position:absolute;top:8px;left:10px;font:600 14px system-ui,sans-serif;
       color:#fff;text-shadow:0 1px 3px #000;letter-spacing:.04em;}
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
    <div class="tag">%(node)s</div>
  </div>
</body></html>""" % {"node": CLIP_ID}).encode("utf-8")


def _pending_counts():
    """Clips waiting to be transcoded and waiting to be shipped.

    A directory count on each poll, which is cheap, and it is all the
    aggregator needs to say whether a chop it knows fired is still being
    transcoded, is waiting to be sent, or has gone missing entirely. Dotfiles
    are skipped: those are the in-progress .part writes.
    """
    def count(path):
        try:
            return sum(1 for n in os.listdir(path) if not n.startswith("."))
        except OSError:
            return 0
    return count(OUTPUT_DIR), count(ENCODED_DIR)


def health_snapshot():
    """Node status for /healthz. Cheap enough to serve on every request."""
    now = time.monotonic()
    ring = frame_buffer.stats()
    awaiting_transcode, awaiting_ship = _pending_counts()
    with _status_lock:
        st = dict(_status)

    frame_age = (now - st["camera_last_frame_mono"]) if st["camera_frames"] else None
    # Two seconds without a frame at 120 fps means the camera is gone, not slow.
    camera_ok = frame_age is not None and frame_age < 2.0
    plc_ok = (not PLC_TRIGGER) or st["plc_state"] == "connected"
    modbus_ok = (not MODBUS_TEST_TRIGGER) or st["modbus_state"] == "listening"
    # Whichever trigger this node actually relies on has to be working. With
    # the PLC polled, Modbus is a bench aid and its failure is only a warning;
    # with PLC_TRIGGER off, Modbus is the only way in and its failure is fatal.
    trigger_ok = plc_ok if PLC_TRIGGER else modbus_ok
    # Once running, the ring should span its whole window. Falling short means
    # the byte ceiling is binding and clips will have a truncated pre-roll.
    buffer_ok = (ring["memory_evictions"] == 0
                 or ring["seconds"] >= PRE_SECONDS + POST_SECONDS)

    return {
        "node": NODE_NAME,
        "site": SITE or None,
        "clip_id": CLIP_ID,
        "healthy": bool(camera_ok and trigger_ok and buffer_ok),
        "uptime_s": round(now - _started_mono, 1),
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "camera": {
            "device": CAMERA_DEVICE,
            "state": "streaming" if camera_ok else st["camera_state"],
            "ok": camera_ok,
            "frame_age_s": round(frame_age, 2) if frame_age is not None else None,
            "frames": st["camera_frames"],
            "restarts": st["camera_restarts"],
            "configured_fps": FPS,
        },
        "buffer": {
            "frames": ring["frames"],
            "mb": round(ring["bytes"] / (1024 * 1024), 1),
            "max_mb": BUFFER_MAX_MB,
            "seconds": round(ring["seconds"], 2),
            "window_seconds": BUFFER_SECONDS,
            # Non-zero means the byte ceiling, not the time window, is what
            # trimmed the buffer -- the pre-roll is genuinely short.
            "memory_evictions": ring["memory_evictions"],
        },
        "plc": {
            "enabled": PLC_TRIGGER,
            "type": PLC_TYPE if PLC_TRIGGER else None,
            "tag": TRIGGER_TAG if PLC_TRIGGER else None,
            "state": st["plc_state"],
            "ok": plc_ok,
            "detail": st["plc_detail"],
            "reads": st["plc_reads"],
            "errors": st["plc_errors"],
            "poll_hz": st["plc_poll_hz"],
            "configured_poll_hz": POLL_HZ,
        },
        "modbus": {
            "enabled": MODBUS_TEST_TRIGGER,
            "state": st["modbus_state"],
            "ok": modbus_ok,
            "detail": st["modbus_detail"],
            "bind": f"{MODBUS_BIND_IP}:{MODBUS_PORT}" if MODBUS_TEST_TRIGGER else None,
            "coil_pdu": MODBUS_TRIGGER_PDU if MODBUS_TEST_TRIGGER else None,
        },
        "triggers": {
            "count": st["trigger_count"],
            "last_utc": st["trigger_last_utc"],
            "last_source": st["trigger_last_source"],
        },
        "clips": {
            "written": st["clips_written"],
            "failed": st["clips_failed"],
            "last": st["clip_last"],
            # Where chops are in the pipeline. The aggregator turns these into
            # "processing" / "sending" on the tile, and a chop that is in
            # neither queue yet still hasn't arrived is a stuck pipeline.
            "awaiting_transcode": awaiting_transcode,
            "awaiting_ship": awaiting_ship,
        },
    }


class LiveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):       # silence per-request stderr spam
        pass

    def _send_bytes(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_bytes(_live_page(), "text/html; charset=utf-8")
            return

        if self.path in ("/healthz", "/health", "/healthz.json"):
            health = health_snapshot()
            body = json.dumps(health, indent=2).encode("utf-8")
            # 503 when degraded so the aggregator (or any uptime check) can
            # tell a wedged node from a working one without parsing JSON.
            self._send_bytes(body, "application/json",
                             200 if health["healthy"] else 503)
            return

        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            period = 1.0 / LIVE_FPS
            last_sent = None
            try:
                while not _stop.is_set():
                    t0 = time.monotonic()
                    entry = frame_buffer.latest()
                    # Don't resend the same frame: on a stalled camera this
                    # would spin at LIVE_FPS pushing identical bytes.
                    if entry is not None and entry[0] != last_sent:
                        last_sent = entry[0]
                        frame = entry[1]
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
    log.info("live preview on port %d  (http://<pi-ip>:%d/, health at /healthz)",
             LIVE_PORT, LIVE_PORT)


# ---------------------------------------------------------------------------
# Capture thread: ffmpeg -> concatenated JPEG stream -> ring buffer
# ---------------------------------------------------------------------------
_SOI = b"\xff\xd8"      # JPEG start-of-image
_EOI = b"\xff\xd9"      # JPEG end-of-image

_F_SETPIPE_SZ = 1031    # fcntl.h; not exposed as a constant by the fcntl module
_PIPE_BYTES = 1 << 20   # 1 MiB -- the default unprivileged ceiling on Linux
_READ_CHUNK = 65536


def _widen_pipe(fd):
    """Enlarge the ffmpeg->python pipe.

    Linux pipes default to 64 KiB. At the measured 118 Mbps that is only ~4 ms
    of slack: if this thread is descheduled for longer than that, ffmpeg blocks
    on write, v4l2 backs up, and frames are dropped. 1 MiB buys ~70 ms instead,
    which covers a GC pause or a busy moment on the preview server.
    """
    try:
        import fcntl
        fcntl.fcntl(fd, _F_SETPIPE_SZ, _PIPE_BYTES)
        return True
    except (ImportError, OSError, ValueError):
        return False    # not Linux, or /proc/sys/fs/pipe-max-size is lower


def extract_jpeg_frames(buf):
    """Consume complete JPEGs from `buf` (a bytearray), returning them in order.

    Any trailing partial frame is left in `buf` for the next chunk. Splitting on
    markers is safe for entropy-coded data -- a literal 0xFF there is always
    stuffed with 0x00 -- but NOT for a JPEG carrying an EXIF thumbnail, whose
    own EOI would cut the frame short. This camera emits none; re-check if the
    module is ever swapped.
    """
    frames = []
    while True:
        s = buf.find(_SOI)
        if s < 0:
            # No start marker anywhere: drop everything but a trailing 0xFF,
            # which may be the first half of an SOI split across chunks.
            # Without this a garbage stream grows the buffer without limit.
            del buf[:-1]
            break
        e = buf.find(_EOI, s + 2)
        if e < 0:
            if s > 0:
                del buf[:s]     # drop junk before a partial frame
            break
        e += 2
        frames.append(bytes(buf[s:e]))
        del buf[:e]
    return frames


def apply_camera_controls():
    """Pin v4l2 controls before ffmpeg opens the camera.

    This matters more than it looks. The module ships with auto-exposure on
    (Aperture Priority) and a default exposure of 15.6 ms, while 120 fps allows
    only 8.33 ms per frame -- exposure cannot exceed the frame period, so the
    camera silently drops to ~64 fps in anything less than bright light, and
    what frames it does deliver carry 15 ms of motion blur across a moving
    blade. Autofocus can also hunt mid-event on a fixed mount.

    Every install has different lighting, so this is a per-install setting.
    Applied one control at a time and in order, because exposure_time_absolute
    is inactive until auto_exposure has been set to manual.

    Failures are logged, never fatal: a camera that will not take a control
    still records.
    """
    if not CAMERA_CONTROLS.strip():
        return
    pairs = [p.strip() for p in CAMERA_CONTROLS.split(",") if p.strip()]
    applied, failed = [], []
    for pair in pairs:
        if "=" not in pair:
            log.warning("camera control %r is not name=value; skipping", pair)
            failed.append(pair)
            continue
        try:
            res = subprocess.run(
                ["v4l2-ctl", "-d", CAMERA_DEVICE, f"--set-ctrl={pair}"],
                capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            log.warning("v4l2-ctl not found -- camera controls not applied "
                        "(sudo apt install v4l-utils)")
            return
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("camera control %s failed: %s", pair, exc)
            failed.append(pair)
            continue
        if res.returncode == 0:
            applied.append(pair)
        else:
            failed.append(pair)
            log.warning("camera control %s rejected: %s", pair,
                        (res.stderr or res.stdout).strip()[:120])
    if applied:
        log.info("camera controls applied: %s", ", ".join(applied))
    if failed:
        log.warning("camera controls NOT applied: %s -- check the names with "
                    "v4l2-ctl -d %s --list-ctrls",
                    ", ".join(failed), CAMERA_DEVICE)


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
    first = True
    while not _stop.is_set():
        log.info("starting camera capture: %s", CAMERA_DEVICE)
        set_status(camera_state="opening")
        # Re-applied on every restart: UVC controls do not reliably survive
        # the device being closed and reopened.
        apply_camera_controls()
        if not first:
            bump_status("camera_restarts")
        first = False
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            log.error("ffmpeg not found -- sudo apt install ffmpeg")
            set_status(camera_state="no-ffmpeg")
            return

        if not _widen_pipe(proc.stdout.fileno()):
            log.warning("could not enlarge the capture pipe past the 64 KiB "
                        "default; frame drops are more likely under load")

        buf = bytearray()
        try:
            while not _stop.is_set():
                chunk = proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    # A few of these right after boot are normal: USB
                    # enumeration finishes after systemd starts the service.
                    log.warning("camera stream ended; will restart")
                    set_status(camera_state="stream-ended")
                    break
                buf.extend(chunk)
                for frame in extract_jpeg_frames(buf):
                    now = time.monotonic()
                    frame_buffer.append(now, frame)
                    with _status_lock:
                        _status["camera_last_frame_mono"] = now
                        _status["camera_frames"] += 1
                        _status["camera_state"] = "streaming"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

        if not _stop.is_set():
            _stop.wait(2)


# ---------------------------------------------------------------------------
# Writer thread: assemble pre+post window, mux to disk
# ---------------------------------------------------------------------------
def clip_basename(t_wall_utc):
    """Clip filename stem.

    UTC by default and marked with a trailing Z. Five nodes recording the same
    chop have to be lined up on the aggregator, and local time is both
    ambiguous for one hour every autumn and wrong to compare across nodes whose
    zones or DST state differ. Set CLIP_TIMESTAMP="local" if reviewers would
    rather read wall-clock names than correlate cameras.
    """
    if CLIP_TIMESTAMP == "local":
        stamp = t_wall_utc.astimezone().strftime("%Y%m%d_%H%M%S%z")
    else:
        stamp = t_wall_utc.strftime("%Y%m%d_%H%M%S") + "Z"
    return f"event_{stamp}_{CLIP_ID}"


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

        # Cheap: copies references to JPEG bytes already in RAM.
        snap = frame_buffer.snapshot()

        lo, hi = t_mono - PRE_SECONDS, t_mono + POST_SECONDS
        clip = [(ts, jpg) for ts, jpg in snap if lo <= ts <= hi]
        if not clip:
            log.warning("no frames captured for this trigger; skipping "
                        "(is the camera streaming? check /healthz)")
            bump_status("clips_failed")
            continue

        # If the buffer had already discarded part of the pre-roll we would
        # silently ship a short clip, so say it out loud.
        if clip[0][0] > lo + 0.5:
            log.warning("pre-roll short by %.1fs -- buffer did not reach back "
                        "%ds before the trigger", clip[0][0] - lo, PRE_SECONDS)

        # Real fps from timestamps, so playback speed is right even if the
        # camera delivered slightly fewer frames than requested.
        span = clip[-1][0] - clip[0][0]
        fps = (len(clip) - 1) / span if span > 0 else float(FPS)

        # ...but MUX at a whole number. ffmpeg's raw MJPEG demuxer silently
        # corrupts the timeline for many non-integer input rates: after ~51
        # frames it stops advancing PTS by a frame duration and increments by
        # one tick, so a 30 s clip claims to be half a second long and plays as
        # a blur. Measured on ffmpeg 6.1.1: 100.25, 90.5, 30.5 and 125.75 all
        # break; 120, 119.873, 60 and 29.97 do not. There is no warning, and
        # neither an exact rational (-framerate 401/4) nor an output -r avoids
        # it. Integer rates are always correct, and in the cases that did work
        # ffmpeg had quantised to a whole number anyway -- so the fraction was
        # never buying anything. The measured rate is kept in the metadata and
        # the log for anyone who needs it.
        mux_fps = max(1, round(fps))

        # MJPEG in Matroska: MJPEG-in-MP4 is poorly supported by players, and
        # postprocess.sh looks for .mkv. Encoding here is never an option --
        # libx264 runs ~6 fps on this material, so a 30 s clip would peg the
        # CPU for ten minutes while the capture thread is feeding a 120 fps
        # buffer. postprocess.sh does it later at idle priority.
        base = clip_basename(t_wall)
        meta = {
            "title": f"{CLIP_ID} {t_wall.isoformat(timespec='seconds')}",
            "comment": (f"node={NODE_NAME} site={SITE or '-'} trigger={source} "
                        f"tag={TRIGGER_TAG or '-'} plc={PLC_TYPE} "
                        f"frames={len(clip)} measured_fps={fps:.3f} "
                        f"mux_fps={mux_fps} "
                        f"pre={PRE_SECONDS}s post={POST_SECONDS}s"),
        }
        _mux(clip, mux_fps, OUTPUT_DIR, base, meta, measured_fps=fps)


def _mux(clip, fps, out_dir, base, meta, measured_fps=None):
    """Stream frames into ffmpeg's stdin.

    Deliberately NOT b"".join(...): at 120 fps a 30 s clip is ~400 MB, and
    building that as one bytes object doubles peak memory on top of the ring
    buffer. Writing frame by frame keeps the extra allocation near zero.

    Written to a dot-prefixed .part name and renamed on success, so
    postprocess.sh can never pick up a half-written clip (its glob skips
    dotfiles) and a crash mid-write leaves nothing that looks complete.
    """
    out_path = os.path.join(out_dir, f"{base}.mkv")
    tmp_path = os.path.join(out_dir, f".{base}.part.mkv")
    video = ["-c:v", "copy", "-f", "matroska"]

    meta_args = []
    for key, val in meta.items():
        meta_args += ["-metadata", f"{key}={val}"]

    # `fps` must be a whole number here -- see the note in writer_loop.
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "mjpeg", "-framerate", str(int(fps)), "-i", "pipe:0",
           *video, *meta_args, tmp_path]

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
            os.replace(tmp_path, out_path)
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            log.info("saved %s (%d frames, %d fps%s, %.0f MB, %.1fs)",
                     out_path, len(clip), int(fps),
                     f", measured {measured_fps:.2f}" if measured_fps else "",
                     size_mb, time.monotonic() - t0)
            set_status(clip_last=os.path.basename(out_path))
            bump_status("clips_written")
        else:
            err.seek(0)
            log.error("ffmpeg mux failed (rc=%d): %s", rc,
                      err.read().decode(errors="replace")[:300])
            bump_status("clips_failed")
    except Exception as exc:            # noqa: BLE001
        log.error("mux error: %s", exc)
        bump_status("clips_failed")
    finally:
        err.close()
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    log_effective_config()

    threading.Thread(target=capture_loop, name="capture", daemon=True).start()
    threading.Thread(target=writer_loop, name="writer", daemon=True).start()
    if LIVE_FEED:
        start_live_feed()
    if PLC_TRIGGER:
        threading.Thread(target=plc_poll_loop, name="plc", daemon=True).start()
    else:
        set_status(plc_state="disabled")

    if MODBUS_TEST_TRIGGER:
        await serve_modbus()
    else:
        set_status(modbus_state="disabled")
        await _idle()


def _run_service():
    problems = validate_config()
    if problems:
        log.error("Refusing to start -- %d configuration problem(s) in %s:",
                  len(problems), C.get("_path"))
        for p in problems:
            log.error("  * %s", p)
        return 78                        # EX_CONFIG
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _stop.set()
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    if "--check-config" in sys.argv:
        issues = validate_config()
        if issues:
            print(f"{len(issues)} problem(s) in {C.get('_path')}:")
            for item in issues:
                print(f"  * {item}")
            sys.exit(1)
        for warn in config_warnings():
            print(f"  ! {warn}")
        print(f"{C.get('_path')}: OK")
        print(f"  node   : {CLIP_ID}"
              + (f"   (site={SITE} node={NODE_NAME})" if SITE else ""))
        print(f"  plc    : {PLC_TYPE} {PLC_PATH} tag {TRIGGER_TAG} @ {POLL_HZ} Hz"
              if PLC_TRIGGER else "  plc    : disabled")
        print(f"  camera : {CAMERA_DEVICE} {FRAME_WIDTH}x{FRAME_HEIGHT} @ {FPS}")
        print(f"  clip   : -{PRE_SECONDS}s/+{POST_SECONDS}s -> {OUTPUT_DIR}")
        print(f"  buffer : {BUFFER_SECONDS}s window, {BUFFER_MAX_MB} MB ceiling")
        sys.exit(0)
    if "--test-trigger" in sys.argv:
        raw = _argv_value("--test-trigger", "30")
        try:
            secs = float(raw)
        except (TypeError, ValueError):
            print(f"--test-trigger takes a number of seconds (0 = until "
                  f"Ctrl-C), got {raw!r}")
            sys.exit(2)
        sys.exit(test_trigger(secs))
    if "--list-tags" in sys.argv:
        sys.exit(list_tags(_argv_value("--list-tags", "")))
    sys.exit(_run_service())
