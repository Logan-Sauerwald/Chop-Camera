#!/usr/bin/env python3
"""Unit tests for aggregator/wall.py.

Node-list and clip-name parsing are pure logic and are what decide whether a
tile watches the right camera and whether a delivered clip is attributed to
it. No network, no nodes, no clips on disk.
"""

import importlib
import os
import sys
import tempfile
import unittest
from datetime import timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "aggregator"))
sys.path.insert(0, os.path.join(ROOT, "src"))

_tmp = tempfile.TemporaryDirectory()
_incoming = os.path.join(_tmp.name, "incoming")
os.makedirs(_incoming)
_conf = os.path.join(_tmp.name, "agg.conf")
with open(_conf, "w") as fh:
    fh.write('SITE="line3"\n'
             'NODES="uw1=192.168.0.101 uw2=192.168.0.102"\n'
             'NODE_PORT="8080"\n'
             f'INCOMING_DIR="{_incoming}"\n')
os.environ["CHOPCAM_AGG_CONF"] = _conf

wall = importlib.import_module("wall")

# wall reads its config at import time, and another test module may have
# imported it first (purge.py imports it too), in which case the cached module
# carries that module's settings. Force the ones these tests rely on.
wall.SITE = "line3"
wall.NODES = wall.parse_nodes("uw1=192.168.0.101 uw2=192.168.0.102")
wall.INCOMING_DIR = _incoming


class TestParseNodes(unittest.TestCase):

    def test_parses_pairs_in_order(self):
        nodes = wall.parse_nodes("uw1=192.168.0.4 uw2=192.168.0.8 uw3=192.168.0.13")
        self.assertEqual([n["name"] for n in nodes], ["uw1", "uw2", "uw3"])
        self.assertEqual(nodes[2]["address"], "192.168.0.13")

    def test_variable_node_count(self):
        # The number of Pis differs per install; nothing else should change.
        for count in (1, 4, 12):
            raw = " ".join(f"n{i}=10.0.0.{i}" for i in range(count))
            self.assertEqual(len(wall.parse_nodes(raw)), count)

    def test_empty_means_no_nodes(self):
        self.assertEqual(wall.parse_nodes(""), [])
        self.assertEqual(wall.parse_nodes(None), [])

    def test_extra_whitespace_tolerated(self):
        self.assertEqual(len(wall.parse_nodes("  uw1=10.0.0.1   uw2=10.0.0.2  ")), 2)

    def test_hostnames_allowed(self):
        nodes = wall.parse_nodes("uw1=chop-uw1.plant.local")
        self.assertEqual(nodes[0]["address"], "chop-uw1.plant.local")

    def test_missing_equals_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            wall.parse_nodes("uw1 192.168.0.4")
        self.assertIn("name=address", str(ctx.exception))

    def test_empty_half_rejected(self):
        for bad in ("uw1=", "=192.168.0.4"):
            with self.assertRaises(ValueError, msg=bad):
                wall.parse_nodes(bad)

    def test_duplicate_name_rejected(self):
        # Two tiles with one name would silently watch one camera.
        with self.assertRaises(ValueError) as ctx:
            wall.parse_nodes("uw1=10.0.0.1 uw1=10.0.0.2")
        self.assertIn("twice", str(ctx.exception))

    def test_unsafe_name_rejected(self):
        with self.assertRaises(ValueError):
            wall.parse_nodes("uw 1=10.0.0.1")


class TestClipNames(unittest.TestCase):
    """Clip names are the only link between a delivered file and the tile that
    recorded it, so this is what makes per-node clip counts correct."""

    def test_utc_name(self):
        cid, when = wall.parse_clip_name("event_20260910_193012Z_line3-uw1.mp4")
        self.assertEqual(cid, "line3-uw1")
        self.assertEqual(when.tzinfo, timezone.utc)
        self.assertEqual(when.isoformat(), "2026-09-10T19:30:12+00:00")

    def test_local_name_with_offset(self):
        cid, when = wall.parse_clip_name("event_20260910_143012-0500_line3-uw1.mp4")
        self.assertEqual(cid, "line3-uw1")
        self.assertEqual(when.utcoffset().total_seconds(), -5 * 3600)

    def test_node_without_site(self):
        cid, _ = wall.parse_clip_name("event_20260910_193012Z_chop1.mp4")
        self.assertEqual(cid, "chop1")

    def test_underscores_in_node_name(self):
        cid, _ = wall.parse_clip_name("event_20260910_193012Z_line_3-uw_1.mp4")
        self.assertEqual(cid, "line_3-uw_1")

    def test_non_clip_files_ignored(self):
        for bad in ("notes.txt", "event_bad.mp4", "event_20260910_193012Z.mp4",
                    "clip.mp4"):
            self.assertEqual(wall.parse_clip_name(bad), (None, None), bad)

    def test_clip_id_matches_the_capture_side(self):
        self.assertEqual(wall.clip_id("uw1"), "line3-uw1")


class TestClipAttribution(unittest.TestCase):

    def setUp(self):
        for f in os.listdir(_incoming):
            os.remove(os.path.join(_incoming, f))

    def _touch(self, name):
        with open(os.path.join(_incoming, name), "wb") as fh:
            fh.write(b"x" * 1024)

    def test_counts_land_on_the_right_node(self):
        self._touch("event_20260910_193012Z_line3-uw1.mp4")
        self._touch("event_20260910_194012Z_line3-uw1.mp4")
        self._touch("event_20260910_195012Z_line3-uw2.mp4")
        summary = wall.clips_by_node()
        self.assertEqual(summary["uw1"]["count"], 2)
        self.assertEqual(summary["uw2"]["count"], 1)

    def test_clips_from_another_site_are_not_counted(self):
        # Several installs may share a filesystem one day; a clip from another
        # site must not inflate this wall's counts.
        self._touch("event_20260910_193012Z_line9-uw1.mp4")
        self.assertEqual(wall.clips_by_node()["uw1"]["count"], 0)

    def test_listing_is_newest_first(self):
        import time
        self._touch("event_20260910_193012Z_line3-uw1.mp4")
        time.sleep(0.01)
        self._touch("event_20260910_194012Z_line3-uw2.mp4")
        clips = wall.list_clips()
        self.assertEqual(clips[0]["clip_id"], "line3-uw2")

    def test_missing_incoming_dir_is_not_fatal(self):
        saved = wall.INCOMING_DIR
        wall.INCOMING_DIR = "/nonexistent/chopcam"
        try:
            self.assertEqual(wall.list_clips(), [])
        finally:
            wall.INCOMING_DIR = saved


class TestValidation(unittest.TestCase):

    def test_good_config_passes(self):
        self.assertEqual(wall.validate_config(), [])

    def test_missing_incoming_dir_reported(self):
        saved = wall.INCOMING_DIR
        wall.INCOMING_DIR = "/nonexistent/chopcam"
        try:
            problems = wall.validate_config()
            self.assertTrue(any("INCOMING_DIR" in p for p in problems))
        finally:
            wall.INCOMING_DIR = saved

    def test_no_nodes_reported(self):
        saved = wall.NODES
        wall.NODES = []
        try:
            problems = wall.validate_config()
            self.assertTrue(any("NODES" in p for p in problems))
        finally:
            wall.NODES = saved


if __name__ == "__main__":
    unittest.main()
