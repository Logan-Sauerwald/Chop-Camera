#!/usr/bin/env python3
"""Unit tests for config parsing and clip naming in src/capture.py.

capture.py loads its config at import time, so the test config is written and
pointed at via CHOPCAM_CONF before the module is imported.
"""

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, SRC)

CONF = """
# chopcam test config
NODE_NAME="chop3"
PLC_TYPE="siemens"
PLC_PATH="192.0.2.10"
TRIGGER_TAG="DB100.DBX0.7"
SIEMENS_RACK="0"
SIEMENS_SLOT="2"
POLL_HZ="60"          # inline comment after a quoted value
FPS=120               # inline comment after a bare value
PRE_SECONDS="15"
POST_SECONDS="15"
export STATE_DIR="/var/lib/chopcam"
LIVE_FEED="true"
MODBUS_TEST_TRIGGER="false"
NOTE="a # b"
EMPTY=""
"""

_tmpdir = tempfile.TemporaryDirectory()
_conf_path = os.path.join(_tmpdir.name, "chopcam.conf")
with open(_conf_path, "w") as fh:
    fh.write(CONF)
os.environ["CHOPCAM_CONF"] = _conf_path

capture = importlib.import_module("capture")


class TestConfigParsing(unittest.TestCase):

    def test_quoted_values(self):
        self.assertEqual(capture.NODE_NAME, "chop3")
        self.assertEqual(capture.TRIGGER_TAG, "DB100.DBX0.7")

    def test_inline_comment_after_quoted_value(self):
        # Before the fix this produced '30"  # inline...' -> int() failed ->
        # POLL_HZ silently fell back to 30 while bash read 60. The two readers
        # of this file disagreed and nothing said so.
        self.assertEqual(capture.POLL_HZ, 60)

    def test_inline_comment_after_bare_value(self):
        self.assertEqual(capture.FPS, 120)

    def test_export_prefix_is_stripped(self):
        self.assertEqual(capture.C["STATE_DIR"], "/var/lib/chopcam")

    def test_hash_inside_quotes_is_kept(self):
        self.assertEqual(capture.C["NOTE"], "a # b")

    def test_empty_value(self):
        self.assertEqual(capture.C["EMPTY"], "")

    def test_booleans(self):
        self.assertTrue(capture.LIVE_FEED)
        self.assertFalse(capture.MODBUS_TEST_TRIGGER)

    def test_parse_value_directly(self):
        cases = {
            '"30"': "30",
            "'30'": "30",
            "30": "30",
            '"30"   # why': "30",
            "30   # why": "30",
            '"a # b"': "a # b",
            '""': "",
            "": "",
            '  "  spaced  "  ': "  spaced  ",
        }
        for raw, want in cases.items():
            self.assertEqual(capture.parse_config_value(raw), want, repr(raw))


class TestClipNaming(unittest.TestCase):

    def test_utc_names_are_sortable_and_marked(self):
        t = datetime(2026, 9, 9, 19, 30, 12, tzinfo=timezone.utc)
        capture.CLIP_TIMESTAMP = "utc"
        self.assertEqual(capture.clip_basename(t),
                         "event_20260909_193012Z_chop3")

    def test_local_names_carry_the_offset(self):
        # Ambiguity in the autumn DST overlap is what the offset prevents.
        t = datetime(2026, 9, 9, 19, 30, 12, tzinfo=timezone.utc)
        capture.CLIP_TIMESTAMP = "local"
        name = capture.clip_basename(t)
        self.assertTrue(name.startswith("event_2026"), name)
        self.assertTrue(name.endswith("_chop3"), name)
        # an offset like +0000 / -0500 must be present
        self.assertRegex(name, r"_\d{8}_\d{6}[+-]\d{4}_chop3$")

    def tearDown(self):
        capture.CLIP_TIMESTAMP = "utc"


class TestValidation(unittest.TestCase):
    """validate_config() is run as a subprocess so module-level config
    constants can be varied without re-importing capture in-process."""

    def _run(self, conf_body):
        path = os.path.join(_tmpdir.name, "case.conf")
        with open(path, "w") as fh:
            fh.write(conf_body)
        env = dict(os.environ, CHOPCAM_CONF=path)
        return subprocess.run(
            [sys.executable, os.path.join(SRC, "capture.py"), "--check-config"],
            env=env, capture_output=True, text=True)

    def test_good_config_passes(self):
        res = self._run(CONF)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_missing_node_name_is_fatal(self):
        res = self._run(CONF.replace('NODE_NAME="chop3"', 'NODE_NAME=""'))
        self.assertEqual(res.returncode, 1)
        self.assertIn("NODE_NAME", res.stdout)

    def test_node_name_with_slash_is_fatal(self):
        res = self._run(CONF.replace('NODE_NAME="chop3"', 'NODE_NAME="chop 1/a"'))
        self.assertEqual(res.returncode, 1)
        self.assertIn("NODE_NAME", res.stdout)

    def test_bad_siemens_address_is_fatal(self):
        res = self._run(CONF.replace('TRIGGER_TAG="DB100.DBX0.7"',
                                     'TRIGGER_TAG="DB100.DBX0.9"'))
        self.assertEqual(res.returncode, 1)
        self.assertIn("0-7", res.stdout)

    def test_controllogix_tag_with_siemens_type_is_fatal(self):
        # The likeliest node-to-node copy mistake: right tag, wrong PLC family.
        res = self._run(CONF.replace('TRIGGER_TAG="DB100.DBX0.7"',
                                     'TRIGGER_TAG="_R1_156N0:33:O.7"'))
        self.assertEqual(res.returncode, 1)

    def test_unknown_plc_type_is_fatal(self):
        res = self._run(CONF.replace('PLC_TYPE="siemens"', 'PLC_TYPE="modicon"'))
        self.assertEqual(res.returncode, 1)
        self.assertIn("modicon", res.stdout)

    def test_no_trigger_source_at_all_is_fatal(self):
        body = CONF.replace('MODBUS_TEST_TRIGGER="false"',
                            'MODBUS_TEST_TRIGGER="false"\nPLC_TRIGGER="false"')
        res = self._run(body)
        self.assertEqual(res.returncode, 1)
        self.assertIn("nothing can ever start a recording", res.stdout)

    def test_missing_config_file_is_reported(self):
        # Only meaningful where no fallback exists; on a real node
        # /etc/chopcam.conf is found by the search path and this is moot.
        fallbacks = ["/etc/chopcam.conf",
                     os.path.join(SRC, "..", "chopcam.conf")]
        if any(os.path.isfile(f) for f in fallbacks):
            self.skipTest("a fallback config exists on this machine")
        env = dict(os.environ, CHOPCAM_CONF="/nonexistent/chopcam.conf")
        res = subprocess.run(
            [sys.executable, os.path.join(SRC, "capture.py"), "--check-config"],
            env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("No config found", res.stdout + res.stderr)


class TestMemoryBudget(unittest.TestCase):
    """The ring buffer's byte ceiling has to fit inside systemd's MemoryMax,
    or the service is OOM-killed on the first busy scene instead of saying so.
    """

    def setUp(self):
        self._mb = capture.BUFFER_MAX_MB
        self._limit = capture._cgroup_memory_limit_bytes

    def tearDown(self):
        capture.BUFFER_MAX_MB = self._mb
        capture._cgroup_memory_limit_bytes = self._limit

    def _warn(self, mb, limit_mb):
        """Only the memory-budget warnings -- config_warnings() also reports
        unrelated things like a placeholder PLC address."""
        capture.BUFFER_MAX_MB = mb
        capture._cgroup_memory_limit_bytes = (
            lambda: None if limit_mb is None else limit_mb * 1024 * 1024)
        return [w for w in capture.config_warnings() if "BUFFER_MAX_MB" in w]

    def test_default_budget_fits_the_shipped_memorymax(self):
        # chopcam.service sets MemoryMax=1500M; the 31 s default must fit.
        self.assertEqual(self._warn(620, 1500), [])

    def test_budget_too_close_to_the_limit_warns(self):
        warnings = self._warn(1400, 1500)
        self.assertTrue(warnings)
        self.assertIn("MemoryMax", warnings[0])

    def test_budget_below_the_measured_peak_warns(self):
        # 118 Mbps over a 31 s window is ~457 MB.
        warnings = self._warn(100, 1500)
        self.assertTrue(warnings)
        self.assertIn("below", warnings[0])

    def test_no_cgroup_limit_means_no_memorymax_warning(self):
        # Running outside systemd (a bench run) must not invent a warning.
        self.assertEqual(self._warn(9000, None), [])

    def test_cgroup_reader_tolerates_a_missing_or_unlimited_cgroup(self):
        # Must return None rather than raising, whatever the machine looks
        # like -- it runs on every startup.
        val = capture._cgroup_memory_limit_bytes()
        self.assertTrue(val is None or (isinstance(val, int) and val > 0))


class TestBothReadersAgree(unittest.TestCase):
    """chopcam.conf is read twice: bash `source` in postprocess.sh and the
    parser in capture.py. If they ever disagree about a value, one half of the
    system runs on settings the other half doesn't have -- silently. This
    compares them key by key over the shipped example.
    """

    @classmethod
    def setUpClass(cls):
        cls.example = os.path.join(SRC, "..", "chopcam.conf.example")
        if not shutil.which("bash"):
            raise unittest.SkipTest("bash not available")

    def test_example_config_is_valid_bash(self):
        res = subprocess.run(["bash", "-n", self.example],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_bash_and_python_read_identical_values(self):
        # Ask bash for the value of every key the Python parser found.
        py = capture.load_config(self.example)
        keys = [k for k in py if not k.startswith("_")]
        script = "set -u\nsource %s\n" % self.example
        for k in keys:
            script += 'printf "%%s\\n" "${%s-<<UNSET>>}"\n' % k
        res = subprocess.run(["bash", "-c", script],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        sh_values = res.stdout.split("\n")[:len(keys)]

        self.assertGreater(len(keys), 20, "example config looks truncated")
        mismatches = []
        for key, sh_val in zip(keys, sh_values):
            if py[key] != sh_val:
                mismatches.append(f"{key}: python={py[key]!r} bash={sh_val!r}")
        self.assertEqual(mismatches, [], "readers disagree:\n  " +
                         "\n  ".join(mismatches))

    def test_example_config_needs_editing_before_it_will_run(self):
        # NODE_NAME is deliberately blank in the example: a node started from
        # an unedited copy would write clips nobody can attribute.
        py = capture.load_config(self.example)
        self.assertEqual(py["NODE_NAME"], "")


if __name__ == "__main__":
    unittest.main()
