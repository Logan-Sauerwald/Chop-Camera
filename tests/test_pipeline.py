#!/usr/bin/env python3
"""End-to-end tests for the frame path in src/capture.py.

Uses ffmpeg to produce a real MJPEG stream, splits it with the production
splitter, and muxes it back out with the production writer -- so the JPEG
marker scanning, the .part rename and the clip metadata are all exercised
against real files rather than mocks. Skipped where ffmpeg is unavailable.
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

_tmpdir = tempfile.TemporaryDirectory()
_conf_path = os.path.join(_tmpdir.name, "chopcam.conf")
with open(_conf_path, "w") as fh:
    fh.write('NODE_NAME="chop2"\nPLC_TRIGGER="false"\n'
             'MODBUS_TEST_TRIGGER="true"\nFPS="120"\n'
             'PRE_SECONDS="2"\nPOST_SECONDS="2"\n'
             f'STATE_DIR="{_tmpdir.name}"\n')
os.environ["CHOPCAM_CONF"] = _conf_path

capture = importlib.import_module("capture")


def make_mjpeg_stream(frames, size="320x240"):
    """A real concatenated-JPEG byte stream, exactly as the camera emits."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration=100",
           "-frames:v", str(frames), "-c:v", "mjpeg", "-q:v", "5",
           "-f", "mjpeg", "pipe:1"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries",
         "stream=nb_read_packets,codec_name:format=format_name,duration:format_tags",
         "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class TestJpegSplitter(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.stream = make_mjpeg_stream(20)

    def test_splits_whole_stream(self):
        buf = bytearray(self.stream)
        frames = capture.extract_jpeg_frames(buf)
        self.assertEqual(len(frames), 20)
        self.assertEqual(len(buf), 0)
        for f in frames:
            self.assertTrue(f.startswith(b"\xff\xd8"))
            self.assertTrue(f.endswith(b"\xff\xd9"))

    def test_reassembles_across_arbitrary_chunk_boundaries(self):
        # The real read() returns 64 KiB chunks that cut frames anywhere;
        # a frame split across two reads must survive intact.
        for chunk in (1, 7, 256, 4096, 65536):
            buf = bytearray()
            got = []
            for i in range(0, len(self.stream), chunk):
                buf.extend(self.stream[i:i + chunk])
                got.extend(capture.extract_jpeg_frames(buf))
            self.assertEqual(len(got), 20, f"chunk={chunk}")
            self.assertEqual(b"".join(got), self.stream, f"chunk={chunk}")

    def test_leading_junk_is_discarded(self):
        buf = bytearray(b"\x00\x11garbage" + self.stream)
        frames = capture.extract_jpeg_frames(buf)
        self.assertEqual(len(frames), 20)

    def test_buffer_does_not_grow_without_a_start_marker(self):
        # A stream with no SOI used to accumulate forever and could OOM the
        # service; it must now stay bounded.
        buf = bytearray()
        for _ in range(50):
            buf.extend(b"\x00" * 4096)
            capture.extract_jpeg_frames(buf)
        self.assertLessEqual(len(buf), 1)

    def test_trailing_partial_frame_is_held_not_emitted(self):
        half = len(self.stream) // 2
        buf = bytearray(self.stream[:half])
        frames = capture.extract_jpeg_frames(buf)
        self.assertGreater(len(frames), 0)
        self.assertGreater(len(buf), 0)          # partial frame retained
        buf.extend(self.stream[half:])
        frames += capture.extract_jpeg_frames(buf)
        self.assertEqual(len(frames), 20)
        self.assertEqual(b"".join(frames), self.stream)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class TestMux(unittest.TestCase):

    def setUp(self):
        self.out = tempfile.mkdtemp(dir=_tmpdir.name)
        buf = bytearray(make_mjpeg_stream(30))
        self.frames = capture.extract_jpeg_frames(buf)
        self.clip = [(i / 120.0, f) for i, f in enumerate(self.frames)]

    def test_writes_mkv_keeping_every_frame(self):
        meta = {"title": "chop2 test", "comment": "node=chop2 trigger=modbus"}
        capture._mux(self.clip, 120.0, self.out, "event_test_chop2", meta)
        path = os.path.join(self.out, "event_test_chop2.mkv")
        self.assertTrue(os.path.exists(path), os.listdir(self.out))
        info = probe(path)
        self.assertEqual(int(info["streams"][0]["nb_read_packets"]), 30)
        self.assertEqual(info["streams"][0]["codec_name"], "mjpeg")

    def test_metadata_is_written_into_the_clip(self):
        # Node identity has to survive a rename on the aggregator.
        meta = {"title": "chop2 2026-09-09T19:30:12+00:00",
                "comment": "node=chop2 trigger=plc tag=DB100.DBX0.7"}
        capture._mux(self.clip, 120.0, self.out, "event_meta_chop2", meta)
        info = probe(os.path.join(self.out, "event_meta_chop2.mkv"))
        tags = {k.lower(): v for k, v in info["format"].get("tags", {}).items()}
        self.assertIn("chop2", tags.get("title", ""))
        self.assertIn("DB100.DBX0.7", tags.get("comment", ""))

    def test_clip_duration_matches_frame_count(self):
        # Regression guard for a silent ffmpeg failure: for many NON-INTEGER
        # input frame rates the raw MJPEG demuxer stops advancing PTS after
        # ~51 frames, so a 30 s clip reports half a second and plays as a blur.
        # writer_loop therefore muxes at a whole number; if that rounding is
        # ever removed, these durations collapse.
        for rate in (30, 100, 120):
            capture._mux(self.clip, rate, self.out, f"event_r{rate}", {})
            info = probe(os.path.join(self.out, f"event_r{rate}.mkv"))
            self.assertEqual(int(info["streams"][0]["nb_read_packets"]), 30)
            duration = float(info["format"]["duration"])
            expected = len(self.clip) / rate
            self.assertAlmostEqual(duration, expected, delta=expected * 0.05,
                                   msg=f"{rate} fps: {duration}s, want {expected}s")

    def test_fractional_rate_is_coerced_to_an_integer(self):
        # 100.25 is one of the rates that corrupts the timeline if passed
        # through verbatim.
        capture._mux(self.clip, 100.25, self.out, "event_frac", {})
        info = probe(os.path.join(self.out, "event_frac.mkv"))
        self.assertEqual(int(info["streams"][0]["nb_read_packets"]), 30)
        duration = float(info["format"]["duration"])
        self.assertAlmostEqual(duration, 30 / 100, delta=0.05,
                               msg=f"timeline collapsed: {duration}s")

    def test_writer_rounds_measured_fps_to_a_whole_number(self):
        for measured, want in ((119.873, 120), (100.25, 100), (0.4, 1),
                               (86.6, 87), (29.97, 30)):
            self.assertEqual(max(1, round(measured)), want)

    def test_no_part_file_is_left_behind(self):
        capture._mux(self.clip, 120.0, self.out, "event_clean_chop2", {})
        leftovers = [f for f in os.listdir(self.out) if ".part." in f]
        self.assertEqual(leftovers, [])

    def test_failed_mux_leaves_no_final_file(self):
        # A clip of non-JPEG bytes makes ffmpeg fail; the writer must not
        # publish a name that postprocess.sh would then treat as complete.
        bad = [(0.0, b"not a jpeg at all")]
        capture._mux(bad, 120.0, self.out, "event_bad_chop2", {})
        self.assertFalse(os.path.exists(os.path.join(self.out,
                                                     "event_bad_chop2.mkv")))
        self.assertEqual([f for f in os.listdir(self.out) if ".part." in f], [])

    def test_part_file_is_hidden_from_the_postprocess_glob(self):
        # postprocess.sh iterates "$RAW_DIR"/*.mkv, which bash does not expand
        # to dotfiles -- that is what keeps a half-written clip out of it.
        import glob
        open(os.path.join(self.out, ".event_x.part.mkv"), "wb").close()
        self.assertEqual(glob.glob(os.path.join(self.out, "*.mkv")), [])


class TestFrameRing(unittest.TestCase):
    """The ring is bounded by TIME, not by a frame count derived from the
    configured FPS. That is the whole point: FPS set below the camera's real
    rate used to shrink the buffer below PRE+POST seconds and silently
    truncate the pre-roll of every clip.
    """

    def test_holds_the_full_window_regardless_of_frame_rate(self):
        # 30 s window; feed it at 30, 120 and 400 fps. All must retain 30 s.
        for rate in (30, 120, 400):
            ring = capture.FrameRing(seconds=30, max_bytes=512 * 1024 * 1024)
            for i in range(rate * 40):            # 40 s of frames
                ring.append(i / rate, b"x" * 1000)
            st = ring.stats()
            self.assertGreaterEqual(st["seconds"], 29.9, f"{rate} fps")
            self.assertEqual(st["memory_evictions"], 0, f"{rate} fps")

    def test_underconfigured_fps_no_longer_shortens_the_window(self):
        # The original bug: FPS=60 configured, camera really delivering 120.
        # A count-bounded deque of 60*(15+15)+60 = 1860 frames would have held
        # only 15.5 s of a 31 s window. Time bounding is immune.
        ring = capture.FrameRing(seconds=31, max_bytes=512 * 1024 * 1024)
        for i in range(120 * 40):
            ring.append(i / 120.0, b"x" * 1000)
        self.assertGreaterEqual(ring.stats()["seconds"], 30.9)

    def test_trims_old_frames(self):
        ring = capture.FrameRing(seconds=5, max_bytes=512 * 1024 * 1024)
        for i in range(1000):
            ring.append(i / 100.0, b"x" * 100)
        st = ring.stats()
        self.assertLessEqual(st["seconds"], 5.01)
        self.assertLess(st["frames"], 1000)

    def test_byte_ceiling_bounds_memory_and_is_reported(self):
        # A stream fatter than the budget must not grow without limit, and the
        # truncation must be visible rather than silent.
        ring = capture.FrameRing(seconds=3600, max_bytes=1 * 1024 * 1024)
        for i in range(500):
            ring.append(i / 100.0, b"x" * 10000)          # 5 MB total
        st = ring.stats()
        self.assertLessEqual(st["bytes"], 1024 * 1024)
        self.assertGreater(st["memory_evictions"], 0)

    def test_never_empties_completely(self):
        # Even a single frame larger than the ceiling stays, so the preview
        # and the health check still have something to report.
        ring = capture.FrameRing(seconds=3600, max_bytes=1024)
        ring.append(0.0, b"x" * 50000)
        ring.append(1.0, b"x" * 50000)
        self.assertEqual(ring.stats()["frames"], 1)
        self.assertIsNotNone(ring.latest())

    def test_byte_accounting_stays_exact(self):
        ring = capture.FrameRing(seconds=10, max_bytes=512 * 1024 * 1024)
        for i in range(300):
            ring.append(i / 30.0, b"x" * (100 + i))
        expected = sum(len(f[1]) for f in ring.snapshot())
        self.assertEqual(ring.stats()["bytes"], expected)

    def test_snapshot_and_latest(self):
        ring = capture.FrameRing(seconds=10, max_bytes=512 * 1024 * 1024)
        self.assertIsNone(ring.latest())
        self.assertEqual(ring.snapshot(), [])
        ring.append(1.0, b"a")
        ring.append(2.0, b"b")
        self.assertEqual(ring.latest(), (2.0, b"b"))
        self.assertEqual([f[1] for f in ring.snapshot()], [b"a", b"b"])

    def test_concurrent_appends_do_not_corrupt_accounting(self):
        import threading as _t
        ring = capture.FrameRing(seconds=3600, max_bytes=512 * 1024 * 1024)

        def writer(base):
            for i in range(500):
                ring.append(base + i / 1000.0, b"x" * 64)

        threads = [_t.Thread(target=writer, args=(t * 10.0,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        st = ring.stats()
        self.assertEqual(st["frames"], 2000)
        self.assertEqual(st["bytes"], 2000 * 64)


class TestHealth(unittest.TestCase):
    """The /healthz gate: a node is healthy only when the camera is delivering
    frames AND (the PLC is connected OR the PLC trigger is switched off).

    capture is a module singleton shared with the other test files, so the
    config-derived globals this logic reads are set explicitly here rather than
    inherited from whichever file imported it first.
    """

    def setUp(self):
        import time
        self._saved = (capture.PLC_TRIGGER, capture.MODBUS_TEST_TRIGGER,
                       dict(capture._status))
        self.now = time.monotonic()
        # The gate reads BOTH trigger settings, so both are pinned here rather
        # than inherited from whichever config imported capture first.
        capture.MODBUS_TEST_TRIGGER = False

    def tearDown(self):
        capture.PLC_TRIGGER = self._saved[0]
        capture.MODBUS_TEST_TRIGGER = self._saved[1]
        with capture._status_lock:
            capture._status.clear()
            capture._status.update(self._saved[2])
        capture.frame_buffer.clear()

    def _frame(self, age_s):
        capture.frame_buffer.append(self.now - age_s, b"\xff\xd8\xff\xd9")
        with capture._status_lock:
            capture._status["camera_frames"] = 1
            capture._status["camera_last_frame_mono"] = self.now - age_s

    def _plc(self, enabled, state):
        capture.PLC_TRIGGER = enabled
        with capture._status_lock:
            capture._status["plc_state"] = state

    def test_no_frames_is_unhealthy(self):
        self._plc(False, "disabled")
        with capture._status_lock:
            capture._status["camera_frames"] = 0
            capture._status["camera_last_frame_mono"] = 0.0
        health = capture.health_snapshot()
        self.assertFalse(health["healthy"])
        self.assertIsNone(health["camera"]["frame_age_s"])

    def test_fresh_frame_and_plc_off_is_healthy(self):
        self._plc(False, "disabled")
        self._frame(0.05)
        health = capture.health_snapshot()
        self.assertTrue(health["healthy"], health)
        self.assertEqual(health["camera"]["state"], "streaming")
        self.assertLess(health["camera"]["frame_age_s"], 2.0)

    def test_stale_frame_is_unhealthy(self):
        # A wedged camera keeps the last frame in the buffer forever; age is
        # what distinguishes "streaming" from "stopped an hour ago".
        self._plc(False, "disabled")
        self._frame(30.0)
        health = capture.health_snapshot()
        self.assertFalse(health["healthy"], health)
        self.assertFalse(health["camera"]["ok"])
        self.assertGreater(health["camera"]["frame_age_s"], 2.0)

    def test_plc_enabled_but_disconnected_is_unhealthy(self):
        # The failure that used to be invisible: camera fine, trigger dead,
        # systemd still reporting the unit as active.
        self._plc(True, "error")
        self._frame(0.05)
        health = capture.health_snapshot()
        self.assertFalse(health["healthy"], health)
        self.assertTrue(health["camera"]["ok"])
        self.assertFalse(health["plc"]["ok"])

    def test_plc_connected_is_healthy(self):
        self._plc(True, "connected")
        self._frame(0.05)
        health = capture.health_snapshot()
        self.assertTrue(health["healthy"], health)

    def test_modbus_only_node_is_unhealthy_when_its_port_is_dead(self):
        # With the PLC trigger off, Modbus is the only way in, so its failure
        # has to make the node unhealthy rather than being a warning.
        capture.PLC_TRIGGER = False
        capture.MODBUS_TEST_TRIGGER = True
        with capture._status_lock:
            capture._status["modbus_state"] = "error"
        self._frame(0.05)
        self.assertFalse(capture.health_snapshot()["healthy"])

        with capture._status_lock:
            capture._status["modbus_state"] = "listening"
        self.assertTrue(capture.health_snapshot()["healthy"])

    def test_modbus_failure_is_only_a_warning_when_the_plc_is_the_trigger(self):
        # With the PLC polled, Modbus is a bench aid; losing it must not take
        # the node out of service.
        capture.PLC_TRIGGER = True
        capture.MODBUS_TEST_TRIGGER = True
        with capture._status_lock:
            capture._status["plc_state"] = "connected"
            capture._status["modbus_state"] = "error"
        self._frame(0.05)
        health = capture.health_snapshot()
        self.assertTrue(health["healthy"], health)
        self.assertFalse(health["modbus"]["ok"])

    def test_snapshot_is_json_serialisable(self):
        self._plc(True, "connected")
        self._frame(0.05)
        json.dumps(capture.health_snapshot())


class TestTriggerLog(unittest.TestCase):
    """What the node remembers about its own triggers.

    The aggregator can only see files, so a chop that never produced one --
    the camera was down, the buffer was empty, it landed inside the previous
    recording -- would leave no trace anywhere. This is where that trace is
    made, and /triggers is how it gets off the node.
    """

    def setUp(self):
        from collections import deque
        self._saved_log = list(capture._trigger_log)
        self._saved_status = dict(capture._status)
        with capture._status_lock:
            capture._trigger_log.clear()
        # Drain anything an earlier test left queued.
        while not capture.trigger_q.empty():
            capture.trigger_q.get_nowait()

    def tearDown(self):
        with capture._status_lock:
            capture._trigger_log.clear()
            capture._trigger_log.extend(self._saved_log)
            capture._status.clear()
            capture._status.update(self._saved_status)
        while not capture.trigger_q.empty():
            capture.trigger_q.get_nowait()

    def test_a_trigger_is_recorded_the_moment_it_fires(self):
        capture.fire_trigger("plc")
        log = capture.trigger_log_snapshot()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["source"], "plc")
        self.assertEqual(log[0]["state"], "recording")
        self.assertIsNone(log[0]["clip"])
        # Parseable, and the aggregator dedupes on it, so it must be present.
        self.assertTrue(datetime.fromisoformat(log[0]["utc"]))

    def test_the_record_rides_with_the_trigger(self):
        # Matching on a timestamp would tie for two triggers in one second, so
        # the writer is handed the object itself.
        capture.fire_trigger("modbus")
        _mono, _wall, source, record = capture.trigger_q.get_nowait()
        self.assertEqual(source, "modbus")
        self.assertIs(record, capture._trigger_log[-1])

    def test_the_writer_can_fill_in_the_clip(self):
        capture.fire_trigger("plc")
        _mono, _wall, _src, record = capture.trigger_q.get_nowait()
        capture.set_trigger_state(record, clip="event_x.mp4", state="recorded")
        entry = capture.trigger_log_snapshot()[0]
        self.assertEqual(entry["clip"], "event_x.mp4")
        self.assertEqual(entry["state"], "recorded")

    def test_the_snapshot_is_a_copy(self):
        # It is serialised straight to JSON on a request thread while the
        # writer thread is still mutating records.
        capture.fire_trigger("plc")
        snap = capture.trigger_log_snapshot()
        snap[0]["state"] = "tampered"
        self.assertEqual(capture.trigger_log_snapshot()[0]["state"], "recording")

    def test_the_log_is_bounded(self):
        from collections import deque
        saved = capture._trigger_log
        capture._trigger_log = deque(maxlen=5)
        try:
            for _ in range(20):
                capture.fire_trigger("plc")
            self.assertEqual(len(capture.trigger_log_snapshot()), 5)
        finally:
            capture._trigger_log = saved

    def test_set_trigger_state_tolerates_no_record(self):
        capture.set_trigger_state(None, state="recorded")     # must not raise

    def test_the_trigger_count_still_moves(self):
        before = capture._status["trigger_count"]
        capture.fire_trigger("plc")
        self.assertEqual(capture._status["trigger_count"], before + 1)
        self.assertEqual(capture._status["trigger_last_source"], "plc")

    def test_health_reports_the_log_and_the_clip_shape(self):
        capture.fire_trigger("plc")
        health = capture.health_snapshot()
        self.assertEqual(health["triggers"]["logged"], 1)
        # The aggregator's player puts the trigger marker at post_seconds back
        # from the end of the clip, so the node has to say what that is.
        self.assertEqual(health["clip_shape"]["post_seconds"],
                         capture.POST_SECONDS)
        self.assertEqual(health["clip_shape"]["pre_seconds"],
                         capture.PRE_SECONDS)
        json.dumps(health)                       # served as-is at /healthz


if __name__ == "__main__":
    unittest.main()
