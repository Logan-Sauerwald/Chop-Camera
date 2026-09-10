#!/usr/bin/env python3
"""
plc.py -- PLC trigger sources.

Supports several PLC families behind one interface, so capture.py doesn't care
which is on the other end:

  * Allen-Bradley ControlLogix / CompactLogix  (EtherNet/IP, via pycomm3)
  * Siemens S7-300/400/1200/1500              (ISO-on-TCP, via python-snap7)

All are polled the same way: open a connection once, read one boolean
repeatedly, fire on a FALSE->TRUE edge.

ADDING A PLC FAMILY
  1. Subclass TriggerSource and implement open() / read_bool() / close().
  2. Call register_source() with the canonical name, its config aliases, and a
     factory that builds it from the config dict.
  Nothing in capture.py changes -- it only ever sees TriggerSource.

Library versions are pinned by install.sh. Every third-party import is deferred
to open() so this module can be imported (and unit-tested) on a machine with no
PLC libraries installed.
"""

import logging
import re

log = logging.getLogger("chopcam.plc")

# ---------------------------------------------------------------------------
# Siemens address parsing
# ---------------------------------------------------------------------------
# DB100.DBX0.7  -> data block 100, byte 0, bit 7   (DBX/DBB optional: DB100.0.7)
# Q0.7 / A0.7   -> process outputs  (English / German mnemonics)
# I3.2 / E3.2   -> process inputs
# M10.3         -> memory bits (merkers / flags)
# The DBX/DBB token is optional as a whole: DB100.DBX0.7, DB100.DBB0.7
# and DB100.0.7 are the same bit. (The old pattern made only the X/B
# optional, so the documented DB100.0.7 short form never actually parsed.)
_ADDR_DB = re.compile(r"^DB(\d+)\.(?:DB[XB])?(\d+)\.(\d+)$", re.I)
_ADDR_BIT = re.compile(r"^([QAIEM])(\d+)\.(\d+)$", re.I)

_AREA_LETTER = {
    "Q": "PA", "A": "PA",     # outputs
    "I": "PE", "E": "PE",     # inputs
    "M": "MK",                # merkers / flags
}

_ADDR_HELP = ("Expected DB100.DBX0.7 (data block), Q0.7 or A0.7 (output), "
              "I3.2 or E3.2 (input), or M10.3 (merker). Bit index is 0-7.")


def parse_siemens_address(addr):
    """Return (area_name, db_number, byte_index, bit_index).

    area_name is the snap7 Area enum member name, resolved lazily at connect
    time so this function can be used (and tested) without snap7 installed.

    Raises ValueError on anything unparseable -- callers treat that as a
    configuration error, not a transient one.
    """
    a = (addr or "").strip().replace(" ", "")
    if not a:
        raise ValueError(f"Siemens address is empty. {_ADDR_HELP}")

    m = _ADDR_DB.match(a)
    if m:
        area, db, byte, bit = "DB", int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _ADDR_BIT.match(a)
        if not m:
            raise ValueError(f"Cannot parse Siemens address {addr!r}. {_ADDR_HELP}")
        area = _AREA_LETTER[m.group(1).upper()]
        db, byte, bit = 0, int(m.group(2)), int(m.group(3))

    # A bit index above 7 is a typo, not a valid address. Catch it here rather
    # than letting it become a confusing read error later.
    if not 0 <= bit <= 7:
        raise ValueError(
            f"Bit index {bit} in {addr!r} is out of range -- bits are 0-7. "
            "A byte has 8 bits, so DB100.DBX0.8 is really DB100.DBX1.0."
        )
    return area, db, byte, bit


def _resolve_snap7_area(name):
    """Look up a snap7 Area enum member, tolerating library layout changes."""
    import snap7

    enum = getattr(snap7, "Area", None)
    if enum is None:                                  # older/newer layout
        try:
            from snap7.type import Area as enum       # noqa: N813
        except ImportError:
            enum = None
    if enum is None:
        raise ImportError(
            "python-snap7 is installed but exposes no Area enum. chopcam needs "
            "python-snap7 >= 3.1 (pure Python -- no libsnap7 needed). "
            "Fix: pip install 'python-snap7>=3.1,<4'"
        )
    try:
        return getattr(enum, name)
    except AttributeError:                            # pragma: no cover
        raise ValueError(f"snap7 has no memory area {name!r}") from None


def _resolve_snap7_get_bool():
    """Return snap7's get_bool across library layouts."""
    try:
        from snap7.util import get_bool
        return get_bool
    except ImportError:                               # pragma: no cover
        from snap7.util.getters import get_bool
        return get_bool


# ---------------------------------------------------------------------------
# Common interface
# ---------------------------------------------------------------------------
class TriggerSource:
    """One boolean on a PLC, polled for a rising edge."""

    label = "?"

    def open(self):
        raise NotImplementedError

    def read_bool(self):
        """Return the current value as a bool. Raise on any read failure."""
        raise NotImplementedError

    def close(self):
        pass

    def describe(self):
        """Short line logged once per successful connect."""
        return self.label

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        try:
            self.close()
        except Exception:       # noqa: BLE001 -- closing must never mask errors
            pass
        return False


# ---------------------------------------------------------------------------
# Allen-Bradley ControlLogix / CompactLogix
# ---------------------------------------------------------------------------
class LogixSource(TriggerSource):
    """EtherNet/IP via pycomm3.

    `path` is the controller address. If the Ethernet port is on the CPU itself
    a plain IP works; with a separate 1756-EN2T module append the CPU slot,
    e.g. "10.2.4.1/1".
    """

    def __init__(self, path, tag):
        if not str(path or "").strip():
            raise ValueError("PLC_PATH is empty -- set the controller IP, "
                             "e.g. \"10.2.4.1\" (add \"/1\" for the CPU slot "
                             "in a chassis).")
        if not str(tag or "").strip():
            raise ValueError(
                "TRIGGER_TAG is empty -- set the ControlLogix tag to watch. "
                "Find the exact name with:  capture.py --list-tags"
            )
        self.path = str(path).strip()
        self.tag = str(tag).strip()
        self.label = f"ControlLogix {self.path} tag {self.tag}"
        self._plc = None
        self._warned_type = False

    def open(self):
        try:
            from pycomm3 import LogixDriver
        except ImportError as exc:
            raise ImportError(
                "pycomm3 is not installed (needed for PLC_TYPE=\"controllogix\"). "
                "Fix: pip install 'pycomm3>=1.2.14,<2'"
            ) from exc
        self._plc = LogixDriver(self.path)
        self._plc.open()

    def read_bool(self):
        result = self._plc.read(self.tag)
        # A pycomm3 Tag's truthiness reflects .error, not .value -- a valid
        # False read is still truthy, so this only catches genuine errors.
        if not result:
            raise IOError(f"read {self.tag} failed: {result.error}")
        if result.value is None:
            raise IOError(f"read {self.tag} returned no value "
                          f"(type={result.type!r}) -- check the tag name")

        # Reading a whole DINT/INT instead of one bit silently turns the
        # trigger into "any nonzero value", which fires on unrelated data.
        # Say so once rather than letting it look like a PLC fault.
        if not self._warned_type and not isinstance(result.value, bool):
            self._warned_type = True
            log.warning(
                "tag %s reads as %s (value %r), not BOOL -- the trigger is "
                "testing 'nonzero', not one bit. If you meant a single bit, "
                "append the bit index, e.g. %s.7",
                self.tag, result.type, result.value, self.tag,
            )
        return bool(result.value)

    def connected(self):
        return self._plc is not None and bool(getattr(self._plc, "connected", True))

    def close(self):
        if self._plc is not None:
            try:
                self._plc.close()
            finally:
                self._plc = None

    def describe(self):
        info = {}
        try:
            info = self._plc.info or {}
        except Exception:       # noqa: BLE001 -- cosmetic only
            pass
        return (f"ControlLogix {self.path} "
                f"({info.get('product_name') or info.get('device_type', '?')}) "
                f"tag {self.tag}")

    def list_tags(self, substr=""):
        from pycomm3 import LogixDriver
        with LogixDriver(self.path) as plc:
            info = plc.info or {}
            print(f"Connected: {info.get('device_type', '?')}  "
                  f"name={info.get('name', '?')}")
            print(f"Tags matching {substr!r}:" if substr else "All tags:")
            hits = 0
            for tag in sorted(plc.tags):
                if substr.lower() in tag.lower():
                    print("  ", tag)
                    hits += 1
            print(f"({hits} match{'es' if hits != 1 else ''})")
            if not hits and substr:
                print("Nothing matched. Re-run with no filter to list every tag, "
                      "or try a shorter substring.")


# ---------------------------------------------------------------------------
# Siemens S7
# ---------------------------------------------------------------------------
class SiemensSource(TriggerSource):
    """ISO-on-TCP (port 102) via python-snap7.

    Rack/slot depend on the CPU family:
        S7-300 / S7-400     rack 0, slot 2
        S7-1200 / S7-1500   rack 0, slot 1

    Two settings must be right on S7-1200/1500 or reads fail even though the
    connection succeeds:
      * "Permit access with PUT/GET communication from remote partner"
        must be ENABLED (CPU properties -> Protection & Security).
      * Any DB you read must have "Optimized block access" DISABLED, otherwise
        it has no absolute byte addresses to read.
    """

    def __init__(self, ip, rack, slot, address, port=102):
        if not str(ip or "").strip():
            raise ValueError("PLC_PATH is empty -- set the CPU IP, e.g. \"10.2.4.1\".")
        try:
            self.rack = int(rack)
            self.slot = int(slot)
        except (TypeError, ValueError):
            raise ValueError(
                f"SIEMENS_RACK/SIEMENS_SLOT must be whole numbers, got "
                f"rack={rack!r} slot={slot!r}. S7-1200/1500 are usually 0/1, "
                f"S7-300/400 are 0/2."
            ) from None
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            raise ValueError(f"SIEMENS_PORT must be a number, got {port!r}") from None

        self.ip = str(ip).strip()
        self.address = str(address or "").strip()
        # Parsed here so a bad address is a startup error, not a runtime one.
        self._area_name, self._db, self._byte, self._bit = \
            parse_siemens_address(self.address)
        self.label = (f"Siemens {self.ip} rack {self.rack} slot {self.slot} "
                      f"addr {self.address}")
        self._client = None
        self._area = None
        self._get_bool = None

    def open(self):
        try:
            import snap7
        except ImportError as exc:
            raise ImportError(
                "python-snap7 is not installed (needed for PLC_TYPE=\"siemens\"). "
                "Fix: pip install 'python-snap7>=3.1,<4'  "
                "(3.x is pure Python -- no libsnap7 native library needed, "
                "despite what most guides online say.)"
            ) from exc
        # Resolve everything up front so a library-layout problem surfaces at
        # connect time rather than in the middle of the poll loop.
        self._area = _resolve_snap7_area(self._area_name)
        self._get_bool = _resolve_snap7_get_bool()
        self._client = snap7.Client()
        self._client.connect(self.ip, self.rack, self.slot, self.port)

    def read_bool(self):
        # Read the single byte holding our bit, then extract it.
        data = self._client.read_area(self._area, self._db, self._byte, 1)
        return bool(self._get_bool(data, 0, self._bit))

    def connected(self):
        if self._client is None:
            return False
        try:
            return bool(self._client.get_connected())
        except Exception:       # noqa: BLE001
            return False

    def close(self):
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:   # noqa: BLE001
                pass
            finally:
                self._client = None

    def describe(self):
        cpu = ""
        try:
            info = self._client.get_cpu_info()
            name = getattr(info, "ModuleTypeName", b"") or b""
            if isinstance(name, bytes):
                name = name.decode("ascii", "replace")
            cpu = f" ({str(name).strip()})" if str(name).strip() else ""
        except Exception:       # noqa: BLE001 -- cosmetic; some CPUs refuse SZL
            pass
        return (f"Siemens S7 {self.ip}{cpu} (rack {self.rack}, slot {self.slot}) "
                f"{self.address} -> area {self._area_name} "
                f"DB{self._db} byte {self._byte} bit {self._bit}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# canonical name -> (aliases, factory). Adding a family is one register_source()
# call; capture.py needs no changes.
_REGISTRY = {}
_ALIASES = {}


def register_source(canonical, aliases, factory):
    """Register a PLC family under a canonical name plus config aliases."""
    _REGISTRY[canonical] = factory
    for alias in {canonical, *aliases}:
        _ALIASES[alias.strip().lower()] = canonical


def known_types():
    """Canonical PLC_TYPE values, for error messages and docs."""
    return sorted(_REGISTRY)


def _make_logix(cfg):
    return LogixSource(
        path=cfg.get("PLC_PATH", ""),
        tag=cfg.get("TRIGGER_TAG", ""),
    )


def _make_siemens(cfg):
    return SiemensSource(
        ip=cfg.get("PLC_PATH", ""),
        rack=cfg.get("SIEMENS_RACK", 0),
        slot=cfg.get("SIEMENS_SLOT", 1),
        address=cfg.get("TRIGGER_TAG", ""),
        port=cfg.get("SIEMENS_PORT", 102),
    )


register_source(
    "controllogix",
    {"compactlogix", "logix", "ab", "allen-bradley", "allenbradley",
     "rockwell", "ethernetip", "enip"},
    _make_logix,
)
register_source(
    "siemens",
    {"s7", "simatic", "step7", "tia", "snap7"},
    _make_siemens,
)


def make_trigger_source(cfg):
    """Build the right TriggerSource from a config dict.

    Raises ValueError for any configuration problem (unknown family, missing
    or unparseable address). Callers treat that as fatal rather than retrying.
    """
    raw = str(cfg.get("PLC_TYPE", "") or "").strip().lower()
    if not raw:
        raise ValueError(
            f"PLC_TYPE is empty. Set it to one of: {', '.join(known_types())}"
        )
    canonical = _ALIASES.get(raw)
    if canonical is None:
        raise ValueError(
            f"Unknown PLC_TYPE {raw!r}. Known types: {', '.join(known_types())}"
        )
    return _REGISTRY[canonical](cfg)
