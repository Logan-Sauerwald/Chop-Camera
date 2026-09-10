#!/usr/bin/env python3
"""Unit tests for aggregator/wall.py.

Node-list and clip-name parsing are pure logic and are what decide whether a
tile watches the right camera and whether a delivered clip is attributed to
it. No network, no nodes, no clips on disk.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

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
# Derived at import time from whichever config got there first, so they are
# pinned alongside INCOMING_DIR rather than left pointing at another test's
# temporary directory.
wall.KEEP_DIR = os.path.join(_incoming, "keep")
wall.CHOPLOG_PATH = os.path.join(_tmp.name, "choplog.jsonl")
# _status is keyed by NODES as it stood at import, which may be another test
# module's single-node config; rebuild it for the nodes pinned above.
wall._status = wall.blank_status(wall.NODES)


def _clear_clips():
    """Empty the shared clip directory, keep/ included."""
    for name in os.listdir(_incoming):
        path = os.path.join(_incoming, name)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)


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
        _clear_clips()

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

    def test_newest_is_by_recorded_time_not_delivery_time(self):
        # A node that was offline delivers its backlog in one burst: every
        # mtime lands in the same second, so ordering by mtime is arbitrary.
        # "Last chop" must still be the most recently RECORDED clip.
        self._touch("event_20260910_080000Z_line3-uw1.mp4")   # oldest chop
        self._touch("event_20260910_200000Z_line3-uw1.mp4")   # newest chop
        self._touch("event_20260910_120000Z_line3-uw1.mp4")   # middle
        newest = wall.newest_clip_for("uw1")
        self.assertEqual(newest["file"], "event_20260910_200000Z_line3-uw1.mp4")
        self.assertEqual([c["file"] for c in wall.list_clips()][0],
                         "event_20260910_200000Z_line3-uw1.mp4")

    def test_a_delivered_clip_is_not_reported_as_still_processing(self):
        # The bug this guards: with the newest clip picked wrongly, a node
        # whose chop had already arrived showed "Chop processing" forever.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        recent = now - timedelta(minutes=4)
        self._touch(f"event_{recent:%Y%m%d_%H%M%S}Z_line3-uw1.mp4")
        self._touch("event_20260101_010000Z_line3-uw1.mp4")     # much older
        health = {"triggers": {"last_utc":
                               (now - timedelta(minutes=5)).isoformat(timespec="seconds")},
                  "clips": {"awaiting_transcode": 0, "awaiting_ship": 0}}
        state = wall.pending_state(health, wall.newest_clip_for("uw1"))
        self.assertEqual(state["state"], "none", state)

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


class KeepCase(unittest.TestCase):

    def setUp(self):
        _clear_clips()
        os.makedirs(wall.KEEP_DIR, exist_ok=True)

    def _touch(self, name, kept=False, size=1024):
        path = os.path.join(wall.KEEP_DIR if kept else _incoming, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return name

    def _name(self, when, node="uw1"):
        return f"event_{when:%Y%m%d_%H%M%S}Z_line3-{node}.mp4"

    def kept_names(self):
        return sorted(os.listdir(wall.KEEP_DIR))

    def loose_names(self):
        return sorted(n for n in os.listdir(_incoming) if n.endswith(".mp4"))


class TestKeep(KeepCase):
    """Keeping a clip has to leave everything else about it working.

    The file moves, so every path that resolved it before the move -- playing
    it, downloading it, calling it this camera's last chop -- has to resolve it
    after, or the button breaks the thing it was pressed on.
    """

    def test_keep_moves_the_file(self):
        name = self._touch(self._name(datetime.now(timezone.utc)))
        ok, _ = wall.set_kept(name, True)
        self.assertTrue(ok)
        self.assertEqual(self.kept_names(), [name])
        self.assertEqual(self.loose_names(), [])

    def test_release_moves_it_back(self):
        name = self._touch(self._name(datetime.now(timezone.utc)), kept=True)
        ok, _ = wall.set_kept(name, False)
        self.assertTrue(ok)
        self.assertEqual(self.kept_names(), [])
        self.assertEqual(self.loose_names(), [name])

    def test_keeping_twice_is_not_an_error(self):
        # Two people at two screens pressing Keep on the same chop is normal.
        name = self._touch(self._name(datetime.now(timezone.utc)))
        self.assertTrue(wall.set_kept(name, True)[0])
        ok, detail = wall.set_kept(name, True)
        self.assertTrue(ok)
        self.assertIn("already", detail)
        self.assertEqual(self.kept_names(), [name])

    def test_unknown_clip_is_reported_not_invented(self):
        ok, _ = wall.set_kept("event_20260910_193012Z_line3-uw9.mp4", True)
        self.assertFalse(ok)

    def test_traversal_is_refused(self):
        for bad in ("../secret.mp4", "..%2Fsecret.mp4", "/etc/passwd",
                    ".hidden.mp4", "notes.txt", "", None):
            self.assertFalse(wall.set_kept(bad, True)[0], bad)

    def test_a_kept_clip_still_serves(self):
        name = self._touch(self._name(datetime.now(timezone.utc)))
        before = wall.safe_clip_path(name)
        wall.set_kept(name, True)
        after = wall.safe_clip_path(name)
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(before, after)          # it really did move
        self.assertTrue(after.startswith(os.path.realpath(wall.KEEP_DIR)))

    def test_a_kept_clip_is_still_the_last_chop(self):
        now = datetime.now(timezone.utc)
        name = self._touch(self._name(now))
        wall.set_kept(name, True)
        newest = wall.newest_clip_for("uw1")
        self.assertIsNotNone(newest)
        self.assertEqual(newest["file"], name)
        self.assertTrue(newest["kept"])

    def test_keeping_does_not_reorder_the_clip_list(self):
        # The move rewrites mtime; ordering must still come from the name.
        now = datetime.now(timezone.utc)
        old = self._touch(self._name(now - timedelta(hours=2)))
        new = self._touch(self._name(now))
        wall.set_kept(old, True)                    # the OLDER one is kept
        self.assertEqual([c["file"] for c in wall.list_clips()], [new, old])

    def test_kept_and_loose_clips_are_counted_together(self):
        now = datetime.now(timezone.utc)
        self._touch(self._name(now - timedelta(hours=1)))
        self._touch(self._name(now, node="uw2"), kept=True)
        summary = wall.clips_by_node()
        self.assertEqual(summary["uw1"]["count"], 1)
        self.assertEqual(summary["uw2"]["count"], 1)

    def test_clip_path_still_refuses_a_symlink_out_of_the_tree(self):
        outside = os.path.join(_tmp.name, "outside.mp4")
        with open(outside, "wb") as fh:
            fh.write(b"x")
        link = os.path.join(wall.KEEP_DIR, "event_20260910_193012Z_line3-uw1.mp4")
        os.symlink(outside, link)
        self.assertIsNone(wall.safe_clip_path(os.path.basename(link)))


class TestBrowseOlderClips(KeepCase):
    """What the player's clip list is built from."""

    def test_filters_to_one_camera(self):
        now = datetime.now(timezone.utc)
        mine = self._touch(self._name(now, node="uw1"))
        self._touch(self._name(now - timedelta(minutes=1), node="uw2"))
        listed = wall.list_clips(for_clip_id="line3-uw1")
        self.assertEqual([c["file"] for c in listed], [mine])

    def test_every_clip_carries_the_date_it_was_recorded(self):
        # The list has to answer "when", not just "which" -- the date is the
        # half of that the filename alone does not make readable.
        when = datetime(2026, 9, 10, 19, 30, 12, tzinfo=timezone.utc)
        self._touch(self._name(when))
        clip = wall.list_clips()[0]
        self.assertEqual(clip["recorded_utc"], "2026-09-10T19:30:12+00:00")

    def test_newest_first_across_both_directories(self):
        now = datetime.now(timezone.utc)
        names = []
        for i in range(6):
            name = self._name(now - timedelta(minutes=i))
            self._touch(name, kept=(i % 2 == 0))
            names.append(name)
        self.assertEqual([c["file"] for c in wall.list_clips()], names)

    def test_limit_applies_after_ordering(self):
        now = datetime.now(timezone.utc)
        newest = self._touch(self._name(now))
        for i in range(1, 5):
            self._touch(self._name(now - timedelta(minutes=i)))
        listed = wall.list_clips(limit=2)
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0]["file"], newest)


class TestChopLog(KeepCase):
    """The record of every trigger, including the ones that produced nothing.

    It has to survive a node restart (the node only remembers its last few
    hundred, in RAM), an aggregator restart, and the purge deleting the
    footage -- that last one is the point: the log outlives the clips.
    """

    def setUp(self):
        super().setUp()
        with wall._choplog_lock:
            wall._choplog.clear()
            wall._choplog_dirty = False
        if os.path.exists(wall.CHOPLOG_PATH):
            os.remove(wall.CHOPLOG_PATH)

    def _rec(self, utc, clip=None, state="recorded", source="plc"):
        return {"utc": utc, "clip": clip, "state": state, "source": source,
                "detail": ""}

    def test_merge_adds_and_dedupes(self):
        records = [self._rec("2026-09-10T19:30:12+00:00"),
                   self._rec("2026-09-10T19:35:12+00:00")]
        self.assertEqual(wall.choplog_merge("uw1", "line3-uw1", records), (2, 0))
        # Re-reading the same node repeatedly must be free: /triggers returns
        # the node's whole ring every time, not a delta.
        self.assertEqual(wall.choplog_merge("uw1", "line3-uw1", records), (0, 0))
        self.assertEqual(len(wall._choplog), 2)

    def test_two_nodes_can_chop_in_the_same_second(self):
        one = self._rec("2026-09-10T19:30:12+00:00")
        wall.choplog_merge("uw1", "line3-uw1", [one])
        wall.choplog_merge("uw2", "line3-uw2", [dict(one)])
        self.assertEqual(len(wall._choplog), 2)

    def test_a_chop_gaining_its_clip_updates_in_place(self):
        utc = "2026-09-10T19:30:12+00:00"
        wall.choplog_merge("uw1", "line3-uw1",
                           [self._rec(utc, state="recording")])
        added, updated = wall.choplog_merge(
            "uw1", "line3-uw1",
            [self._rec(utc, clip="event_20260910_193012Z_line3-uw1.mp4")])
        self.assertEqual((added, updated), (0, 1))
        self.assertEqual(len(wall._choplog), 1)
        entry = wall.choplog_entries()[0]
        self.assertEqual(entry["state"], "recorded")

    def test_records_without_a_time_are_ignored(self):
        wall.choplog_merge("uw1", "line3-uw1",
                           [{"source": "plc"}, "nonsense", None])
        self.assertEqual(len(wall._choplog), 0)

    def test_survives_a_restart(self):
        wall.choplog_merge("uw1", "line3-uw1",
                           [self._rec("2026-09-10T19:30:12+00:00")])
        with wall._choplog_lock:
            wall._choplog.clear()
        wall.choplog_load()
        self.assertEqual(len(wall._choplog), 1)

    def test_a_corrupt_line_does_not_lose_the_rest(self):
        with open(wall.CHOPLOG_PATH, "w") as fh:
            fh.write(json.dumps({"utc": "2026-09-10T19:30:12+00:00",
                                 "node": "uw1"}) + "\n")
            fh.write("{ this is not json\n")
            fh.write("\n")
            fh.write(json.dumps({"utc": "2026-09-10T19:31:12+00:00",
                                 "node": "uw1"}) + "\n")
        wall.choplog_load()
        self.assertEqual(len(wall._choplog), 2)

    def test_oldest_entries_are_dropped_past_the_cap(self):
        saved = wall.CHOPLOG_MAX
        wall.CHOPLOG_MAX = 5
        try:
            base = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
            wall.choplog_merge("uw1", "line3-uw1", [
                self._rec((base + timedelta(minutes=i)).isoformat())
                for i in range(20)])
        finally:
            wall.CHOPLOG_MAX = saved
        kept = wall.choplog_entries()
        self.assertEqual(len(kept), 5)
        # The five NEWEST, not whichever five came out of the dict first.
        self.assertEqual(kept[0]["utc"], (base + timedelta(minutes=19)).isoformat())

    def test_newest_first_and_filterable_by_camera(self):
        base = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
        wall.choplog_merge("uw1", "line3-uw1",
                           [self._rec(base.isoformat())])
        wall.choplog_merge("uw2", "line3-uw2",
                           [self._rec((base + timedelta(minutes=5)).isoformat())])
        self.assertEqual([e["node"] for e in wall.choplog_entries()],
                         ["uw2", "uw1"])
        self.assertEqual([e["node"] for e in wall.choplog_entries(node="uw1")],
                         ["uw1"])

    def test_outcome_tracks_what_is_actually_on_disk(self):
        now = datetime.now(timezone.utc)
        name = self._name(now)
        wall.choplog_merge("uw1", "line3-uw1", [self._rec(now.isoformat(), name)])

        # Recorded, nothing delivered yet.
        self.assertEqual(wall.choplog_entries()[0]["outcome"], "in transit")

        self._touch(name)
        entry = wall.choplog_entries()[0]
        self.assertEqual(entry["outcome"], "on disk")
        self.assertTrue(entry["playable"])

        wall.set_kept(name, True)
        entry = wall.choplog_entries()[0]
        self.assertEqual(entry["outcome"], "kept")
        self.assertTrue(entry["kept"])

    def test_a_purged_clip_reads_as_purged_not_lost(self):
        # The log outlives the footage, so "gone" has to distinguish deleted on
        # schedule from never arrived -- otherwise every entry older than the
        # retention window eventually looks like a fault.
        old = datetime.now(timezone.utc) - timedelta(days=wall.RETENTION_DAYS + 3)
        wall.choplog_merge("uw1", "line3-uw1",
                           [self._rec(old.isoformat(), self._name(old))])
        self.assertEqual(wall.choplog_entries()[0]["outcome"], "purged")

    def test_a_clip_that_never_arrived_reads_as_missing(self):
        stale = datetime.now(timezone.utc) - timedelta(
            minutes=wall.STUCK_MINUTES + 5)
        wall.choplog_merge("uw1", "line3-uw1",
                           [self._rec(stale.isoformat(), self._name(stale))])
        self.assertEqual(wall.choplog_entries()[0]["outcome"], "missing")

    def test_node_side_states_are_carried_through(self):
        now = datetime.now(timezone.utc)
        for state, outcome in (("failed", "failed"), ("coalesced", "coalesced")):
            with self.subTest(state=state):
                with wall._choplog_lock:
                    wall._choplog.clear()
                wall.choplog_merge("uw1", "line3-uw1",
                                   [self._rec(now.isoformat(), state=state)])
                self.assertEqual(wall.choplog_entries()[0]["outcome"], outcome)

    def test_a_trigger_that_produced_no_clip_is_still_logged(self):
        # The whole reason the log is fed from the nodes rather than from the
        # directory: a chop with no file leaves no trace on disk at all.
        now = datetime.now(timezone.utc)
        wall.choplog_merge("uw1", "line3-uw1",
                           [self._rec(now.isoformat(), clip=None, state="failed")])
        entry = wall.choplog_entries()[0]
        self.assertEqual(entry["outcome"], "failed")
        self.assertFalse(entry["playable"])


class TestStatusForThePlayer(KeepCase):
    """What the page needs from /status to draw the trigger marker."""

    def _publish(self, health):
        with wall._status_lock:
            wall._status["uw1"] = {"state": "healthy", "reachable": True,
                                   "health": health, "error": "",
                                   "checked": time.time()}

    def test_clip_shape_reaches_the_page(self):
        self._publish({"healthy": True, "clip_shape": {"pre_seconds": 15,
                                                       "post_seconds": 15}})
        node = next(n for n in wall.aggregate_status()["nodes"]
                    if n["name"] == "uw1")
        self.assertEqual(node["post_seconds"], 15)
        self.assertEqual(node["pre_seconds"], 15)

    def test_an_unreached_node_reports_no_shape(self):
        # The player falls back to the middle of the clip; it must be able to
        # tell "unknown" from a real number.
        self._publish(None)
        node = next(n for n in wall.aggregate_status()["nodes"]
                    if n["name"] == "uw1")
        self.assertIsNone(node["post_seconds"])

    def test_kept_count_is_published(self):
        name = self._touch(self._name(datetime.now(timezone.utc)))
        wall.set_kept(name, True)
        self.assertEqual(wall.aggregate_status()["kept_clips"], 1)


if __name__ == "__main__":
    unittest.main()
