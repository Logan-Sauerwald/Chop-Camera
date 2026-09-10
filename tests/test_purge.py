#!/usr/bin/env python3
"""Unit tests for aggregator/purge.py.

This is the only code in the system that deletes footage, so the guards matter
as much as the feature: age must come from the clip's own name, symlinks must
never be followed, and a mistyped INCOMING_DIR must stop the run.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "aggregator"))
sys.path.insert(0, os.path.join(ROOT, "src"))

_boot = tempfile.TemporaryDirectory()
_bootdir = os.path.join(_boot.name, "incoming")
os.makedirs(_bootdir)
_bootconf = os.path.join(_boot.name, "agg.conf")
with open(_bootconf, "w") as fh:
    fh.write(f'SITE="line3"\nNODES="uw1=1.2.3.4"\nINCOMING_DIR="{_bootdir}"\n')
os.environ.setdefault("CHOPCAM_AGG_CONF", _bootconf)

purge = importlib.import_module("purge")


class PurgeCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.incoming = os.path.join(self.dir, "incoming")
        os.makedirs(self.incoming)
        self.conf = os.path.join(self.dir, "agg.conf")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_conf(self, incoming=None, days=7, limit=85):
        with open(self.conf, "w") as fh:
            fh.write(f'SITE="line3"\nNODES="uw1=1.2.3.4"\n'
                     f'INCOMING_DIR="{incoming or self.incoming}"\n'
                     f'RETENTION_DAYS="{days}"\nDISK_PCT_LIMIT="{limit}"\n')
        return self.conf

    def clip(self, days_old, node="uw1", size=4096):
        when = datetime.now(timezone.utc) - timedelta(days=days_old)
        name = f"event_{when:%Y%m%d_%H%M%S}Z_line3-{node}.mp4"
        path = os.path.join(self.incoming, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return name

    def run_purge(self, *extra):
        argv = sys.argv
        sys.argv = ["purge.py", "--config", self.conf, *extra]
        try:
            return purge.main()
        finally:
            sys.argv = argv

    def names(self):
        return sorted(os.listdir(self.incoming))


class TestRetention(PurgeCase):

    def test_deletes_only_clips_past_retention(self):
        keep_new, keep_edge = self.clip(0), self.clip(6.9)
        old_a, old_b = self.clip(8), self.clip(60)
        self.write_conf(days=7)
        self.run_purge()
        self.assertIn(keep_new, self.names())
        self.assertIn(keep_edge, self.names())
        self.assertNotIn(old_a, self.names())
        self.assertNotIn(old_b, self.names())

    def test_age_comes_from_the_filename_not_mtime(self):
        # A node that was offline delivers a backlog all at once, so every
        # file's mtime is "now" while the chops themselves are weeks old.
        old = self.clip(30)
        os.utime(os.path.join(self.incoming, old), None)
        self.write_conf(days=7)
        self.run_purge()
        self.assertNotIn(old, self.names())

    def test_unparseable_name_falls_back_to_mtime(self):
        stray = os.path.join(self.incoming, "event_unparseable.mp4")
        with open(stray, "wb") as fh:
            fh.write(b"x")
        self.write_conf(days=7)
        self.run_purge()
        self.assertIn("event_unparseable.mp4", self.names())   # mtime is new

        old = time_ago = (datetime.now() - timedelta(days=30)).timestamp()
        os.utime(stray, (old, time_ago))
        self.run_purge()
        self.assertNotIn("event_unparseable.mp4", self.names())

    def test_non_clip_files_are_never_touched(self):
        with open(os.path.join(self.incoming, "notes.txt"), "wb") as fh:
            fh.write(b"keep")
        self.clip(60)
        self.write_conf(days=7)
        self.run_purge()
        self.assertIn("notes.txt", self.names())

    def test_dry_run_deletes_nothing(self):
        self.clip(60), self.clip(90)
        self.write_conf(days=7)
        before = self.names()
        self.run_purge("--dry-run")
        self.assertEqual(self.names(), before)

    def test_changing_retention_takes_effect_without_a_restart(self):
        # The config is re-read on every run: editing the file is the whole
        # interface for changing how long footage is kept.
        c = self.clip(10)
        self.write_conf(days=30)
        self.run_purge()
        self.assertIn(c, self.names())
        self.write_conf(days=7)
        self.run_purge()
        self.assertNotIn(c, self.names())


class TestGuards(PurgeCase):

    def test_refuses_dangerous_incoming_dir(self):
        for bad in ("/", "/home", "/etc", "/var", "/usr"):
            self.write_conf(incoming=bad, days=1)
            self.assertEqual(self.run_purge(), 2, bad)

    def test_never_follows_a_symlink_out_of_the_directory(self):
        outside = os.path.join(self.dir, "precious")
        os.makedirs(outside)
        target = os.path.join(outside, "keepme.mp4")
        with open(target, "wb") as fh:
            fh.write(b"IMPORTANT")
        os.symlink(target,
                   os.path.join(self.incoming,
                                "event_20200101_000000Z_line3-uw1.mp4"))
        self.write_conf(days=1)
        self.run_purge()
        self.assertTrue(os.path.exists(target), "purge followed a symlink")
        with open(target) as fh:
            self.assertEqual(fh.read(), "IMPORTANT")

    def test_missing_directory_is_not_an_error(self):
        self.write_conf(incoming=os.path.join(self.dir, "nope"))
        self.assertEqual(self.run_purge(), 0)


class TestDiskPressure(PurgeCase):
    """The guard that stops a wrong retention number from filling the disk."""

    def setUp(self):
        super().setUp()
        self._real = purge.disk_pct

    def tearDown(self):
        purge.disk_pct = self._real
        super().tearDown()

    def test_purges_oldest_first_when_disk_is_full(self):
        # Every clip is inside retention, so only disk pressure can remove any.
        newest = self.clip(1)
        middle = self.clip(2)
        oldest = self.clip(3)
        self.write_conf(days=30, limit=85)

        # Report full until two clips are gone, then report healthy.
        state = {"n": 0}

        def fake_pct(_path):
            state["n"] = len(os.listdir(self.incoming))
            return 90 if state["n"] > 1 else 10

        purge.disk_pct = fake_pct
        self.run_purge()
        remaining = self.names()
        self.assertEqual(len(remaining), 1)
        self.assertIn(newest, remaining)
        self.assertNotIn(oldest, remaining)
        self.assertNotIn(middle, remaining)

    def test_stops_when_nothing_is_left_rather_than_looping(self):
        self.clip(1)
        self.write_conf(days=30, limit=85)
        purge.disk_pct = lambda _p: 99          # never satisfied
        self.assertEqual(self.run_purge(), 0)   # must terminate
        self.assertEqual(self.names(), [])

    def test_limit_is_clamped_so_it_cannot_delete_everything(self):
        # DISK_PCT_LIMIT below 50 is clamped up: a typo there would otherwise
        # purge the archive on a healthy disk.
        self.clip(1)
        self.write_conf(days=30, limit=1)
        purge.disk_pct = lambda _p: 10          # 10% -- healthy
        self.run_purge()
        self.assertEqual(len(self.names()), 1)


if __name__ == "__main__":
    unittest.main()
