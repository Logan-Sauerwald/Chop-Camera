#!/usr/bin/env python3
"""Unit tests for src/plc.py.

Everything here runs without pycomm3, python-snap7 or a PLC: address parsing
and driver construction are pure logic, and they are the only parts of the
trigger path that can be checked before you are standing at the panel.

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from plc import (                                    # noqa: E402
    LogixSource, SiemensSource, known_types, make_trigger_source,
    parse_siemens_address,
)


class TestSiemensAddress(unittest.TestCase):

    def test_data_block_forms(self):
        # DBX / DBB / bare byte index are all accepted spellings of the same bit
        for addr in ("DB100.DBX0.7", "DB100.DBB0.7", "DB100.0.7"):
            self.assertEqual(parse_siemens_address(addr), ("DB", 100, 0, 7), addr)

    def test_data_block_larger_indices(self):
        self.assertEqual(parse_siemens_address("DB7.DBX12.3"), ("DB", 7, 12, 3))

    def test_outputs_english_and_german(self):
        self.assertEqual(parse_siemens_address("Q0.7"), ("PA", 0, 0, 7))
        self.assertEqual(parse_siemens_address("A0.7"), ("PA", 0, 0, 7))

    def test_inputs_english_and_german(self):
        self.assertEqual(parse_siemens_address("I3.2"), ("PE", 0, 3, 2))
        self.assertEqual(parse_siemens_address("E3.2"), ("PE", 0, 3, 2))

    def test_merkers(self):
        self.assertEqual(parse_siemens_address("M10.3"), ("MK", 0, 10, 3))

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(parse_siemens_address("  db100.dbx0.7 "), ("DB", 100, 0, 7))
        self.assertEqual(parse_siemens_address("q0.7"), ("PA", 0, 0, 7))

    def test_bit_index_above_seven_rejected(self):
        # The classic typo: a byte has 8 bits, so .8 means the next byte.
        with self.assertRaises(ValueError) as ctx:
            parse_siemens_address("DB100.DBX0.8")
        self.assertIn("0-7", str(ctx.exception))

    def test_empty_rejected(self):
        for bad in ("", None, "   "):
            with self.assertRaises(ValueError):
                parse_siemens_address(bad)

    def test_garbage_rejected(self):
        for bad in ("DB100", "Q0", "X0.7", "DB100.DBX0", "0.7",
                    "_R1_156N0:33:O.7", "DB100.DBW0.7"):
            with self.assertRaises(ValueError, msg=bad):
                parse_siemens_address(bad)

    def test_controllogix_tag_is_not_a_siemens_address(self):
        # Guards the most likely commissioning mistake: right tag, wrong
        # PLC_TYPE. This must fail loudly rather than parse into nonsense.
        with self.assertRaises(ValueError):
            parse_siemens_address("_R1_156N0:33:O.7")


class TestFactory(unittest.TestCase):

    def _cfg(self, **kw):
        base = {"PLC_TYPE": "controllogix", "PLC_PATH": "10.2.4.1",
                "TRIGGER_TAG": "_R1_156N0:33:O.7"}
        base.update(kw)
        return base

    def test_logix_aliases_all_resolve(self):
        for alias in ("controllogix", "CompactLogix", "logix", "AB",
                      "allen-bradley", "rockwell", "ethernetip", "enip"):
            src = make_trigger_source(self._cfg(PLC_TYPE=alias))
            self.assertIsInstance(src, LogixSource, alias)

    def test_siemens_aliases_all_resolve(self):
        for alias in ("siemens", "S7", "simatic", "step7", "TIA", "snap7"):
            src = make_trigger_source(self._cfg(PLC_TYPE=alias,
                                                TRIGGER_TAG="DB100.DBX0.7"))
            self.assertIsInstance(src, SiemensSource, alias)

    def test_unknown_type_lists_the_known_ones(self):
        with self.assertRaises(ValueError) as ctx:
            make_trigger_source(self._cfg(PLC_TYPE="modicon"))
        msg = str(ctx.exception)
        for name in known_types():
            self.assertIn(name, msg)

    def test_empty_type_rejected(self):
        with self.assertRaises(ValueError):
            make_trigger_source(self._cfg(PLC_TYPE=""))

    def test_missing_tag_rejected_for_both_families(self):
        with self.assertRaises(ValueError):
            make_trigger_source(self._cfg(TRIGGER_TAG=""))
        with self.assertRaises(ValueError):
            make_trigger_source(self._cfg(PLC_TYPE="siemens", TRIGGER_TAG=""))

    def test_missing_path_rejected(self):
        with self.assertRaises(ValueError):
            make_trigger_source(self._cfg(PLC_PATH=""))

    def test_siemens_bad_address_fails_at_construction(self):
        # Must surface before the poll loop starts, so the service refuses to
        # boot instead of logging an error every two seconds.
        with self.assertRaises(ValueError):
            make_trigger_source(self._cfg(PLC_TYPE="siemens",
                                          TRIGGER_TAG="DB100.DBX0.9"))

    def test_siemens_non_numeric_rack_slot_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            make_trigger_source(self._cfg(PLC_TYPE="siemens",
                                          TRIGGER_TAG="DB100.DBX0.7",
                                          SIEMENS_SLOT="one"))
        self.assertIn("SIEMENS_RACK", str(ctx.exception))

    def test_siemens_defaults_to_rack0_slot1(self):
        src = make_trigger_source(self._cfg(PLC_TYPE="siemens",
                                            TRIGGER_TAG="DB100.DBX0.7"))
        self.assertEqual((src.rack, src.slot, src.port), (0, 1, 102))

    def test_string_rack_slot_from_config_file(self):
        # Values arrive from the config file as strings; they must coerce.
        src = make_trigger_source(self._cfg(PLC_TYPE="siemens",
                                            TRIGGER_TAG="DB100.DBX0.7",
                                            SIEMENS_RACK="0", SIEMENS_SLOT="2"))
        self.assertEqual((src.rack, src.slot), (0, 2))

    def test_labels_are_populated(self):
        logix = make_trigger_source(self._cfg())
        self.assertIn("_R1_156N0:33:O.7", logix.label)
        siemens = make_trigger_source(self._cfg(PLC_TYPE="siemens",
                                                TRIGGER_TAG="DB100.DBX0.7"))
        self.assertIn("DB100.DBX0.7", siemens.label)


if __name__ == "__main__":
    unittest.main()
