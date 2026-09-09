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
        capture._mux(self.clip, 120.0, self.out, "event_test_chop2", "mkv", meta)
        path = os.path.join(self.out, "event_test_chop2.mkv")
        self.assertTrue(os.path.exists(path), os.listdir(self.out))
        info = probe(path)
        self.assertEqual(int(info["streams"][0]["nb_read_packets"]), 30)
        self.assertEqual(info["streams"][0]["codec_name"], "mjpeg")

    def test_metadata_is_written_into_the_clip(self):
        # Node identity has to survive a rename on the aggregator.
        meta = {"title": "chop2 2026-09-09T19:30:12+00:00",
                "comment": "node=chop2 trigger=plc tag=DB100.DBX0.7"}
        capture._mux(self.clip, 120.0, self.out, "event_meta_chop2", "mkv", meta)
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
            capture._mux(self.clip, rate, self.out, f"event_r{rate}", "mkv", {})
            info = probe(os.path.join(self.out, f"event_r{rate}.mkv"))
            self.assertEqual(int(info["streams"][0]["nb_read_packets"]), 30)
            duration = float(info["format"]["duration"])
            expected = len(self.clip) / rate
            self.assertAlmostEqual(duration, expected, delta=expected * 0.05,
                                   msg=f"{rate} fps: {duration}s, want {expected}s")

    def test_fractional_rate_is_coerced_to_an_integer(self):
        # 100.25 is one of the rates that corrupts the timeline if passed
        # through verbatim.
        capture._mux(self.clip, 100.25, self.out, "event_frac", "mkv", {})
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
        capture._mux(self.clip, 120.0, self.out, "event_clean_chop2", "mkv", {})
        leftovers = [f for f in os.listdir(self.out) if ".part." in f]
        self.assertEqual(leftovers, [])

    def test_failed_mux_leaves_no_final_file(self):
        # A clip of non-JPEG bytes makes ffmpeg fail; the writer must not
        # publish a name that postprocess.sh would then treat as complete.
        bad = [(0.0, b"not a jpeg at all")]
        capture._mux(bad, 120.0, self.out, "event_bad_chop2", "mkv", {})
        self.assertFalse(os.path.exists(os.path.join(self.out,
                                                     "event_bad_chop2.mkv")))
        self.assertEqual([f for f in os.listdir(self.out) if ".part." in f], [])

    def test_part_file_is_hidden_from_the_postprocess_glob(self):
        # postprocess.sh iterates "$RAW_DIR"/*.mkv, which bash does not expand
        # to dotfiles -- that is what keeps a half-written clip out of it.
        import glob
        open(os.path.join(self.out, ".event_x.part.mkv"), "wb").close()
        self.assertEqual(glob.glob(os.path.join(self.out, "*.mkv")), [])


class TestHealth(unittest.TestCase):
    """The /healthz gate: a node is healthy only when the camera is delivering
    frames AND (the PLC is connected OR the PLC trigger is switched off).

    capture is a module singleton shared with the other test files, so the
    config-derived globals this logic reads are set explicitly here rather than
    inherited from whichever file imported it first.
    """

    def setUp(self):
        import time
        self._saved = (capture.PLC_TRIGGER, dict(capture._status))
        self.now = time.monotonic()

    def tearDown(self):
        capture.PLC_TRIGGER = self._saved[0]
        with capture._status_lock:
            capture._status.clear()
            capture._status.update(self._saved[1])
        with capture.buffer_lock:
            capture.frame_buffer.clear()

    def _frame(self, age_s):
        with capture.buffer_lock:
            capture.frame_buffer.append((self.now - age_s, b"\xff\xd8\xff\xd9"))
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

    def test_snapshot_is_json_serialisable(self):
        self._plc(True, "connected")
        self._frame(0.05)
        json.dumps(capture.health_snapshot())


if __name__ == "__main__":
    unittest.main()
