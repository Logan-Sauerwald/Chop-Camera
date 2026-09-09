#!/usr/bin/env python3
"""
plc.py -- PLC trigger sources.

Supports two PLC families behind one interface, so capture.py doesn't care
which is on the other end:

  * Allen-Bradley ControlLogix / CompactLogix  (EtherNet/IP, via pycomm3)
  * Siemens S7-300/400/1200/1500              (ISO-on-TCP, via python-snap7)

Both are polled the same way: open a connection once, read one boolean
repeatedly, fire on a FALSE->TRUE edge.

Add a new PLC family by subclassing TriggerSource and registering it in
make_trigger_source().
"""

import re

# ---------------------------------------------------------------------------
# Siemens address parsing
# ---------------------------------------------------------------------------
# DB100.DBX0.7  -> data block 100, byte 0, bit 7   (DBX/DBB optional: DB100.0.7)
# Q0.7 / A0.7   -> process outputs  (English / German mnemonics)
# I3.2 / E3.2   -> process inputs
# M10.3         -> memory bits (merkers / flags)
_ADDR_DB = re.compile(r"^DB(\d+)\.DB[XB]?(\d+)\.(\d+)$", re.I)
_ADDR_BIT = re.compile(r"^([QAIEM])(\d+)\.(\d+)$", re.I)

_AREA_LETTER = {
    "Q": "PA", "A": "PA",     # outputs
    "I": "PE", "E": "PE",     # inputs
    "M": "MK",                # merkers / flags
}


def parse_siemens_address(addr):
    """Return (area_name, db_number, byte_index, bit_index).

    area_name is the snap7 Area enum member name, resolved lazily so this
    module can be imported (and unit-tested) without snap7 installed.
    """
    a = (addr or "").strip().replace(" ", "")

    m = _ADDR_DB.match(a)
    if m:
        area, db, byte, bit = "DB", int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _ADDR_BIT.match(a)
        if not m:
            raise ValueError(
                f"Cannot parse Siemens address {addr!r}. "
                "Expected DB100.DBX0.7, Q0.7, I0.7 or M10.3"
            )
        area = _AREA_LETTER[m.group(1).upper()]
        db, byte, bit = 0, int(m.group(2)), int(m.group(3))

    # A bit index above 7 is a typo, not a valid address. Catch it here rather
    # than letting it become a confusing read error later.
    if not 0 <= bit <= 7:
        raise ValueError(
            f"Bit index {bit} in {addr!r} is out of range -- bits are 0-7."
        )
    return area, db, byte, bit


# ---------------------------------------------------------------------------
# Common interface
# ---------------------------------------------------------------------------
class TriggerSource:
    """One boolean on a PLC, polled for a rising edge."""

    label = "?"

    def open(self):
        raise NotImplementedError

    def read_bool(self):
        """Return the current value. Raise on any read/connection failure."""
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
        self.path = path
        self.tag = tag
        self.label = f"ControlLogix {path} tag {tag}"
        self._plc = None

    def open(self):
        from pycomm3 import LogixDriver
        self._plc = LogixDriver(self.path)
        self._plc.open()

    def read_bool(self):
        result = self._plc.read(self.tag)
        # A pycomm3 Tag's truthiness reflects .error, not .value -- a valid
        # False read is still truthy, so this only catches genuine errors.
        if not result:
            raise IOError(f"read {self.tag} failed: {result.error}")
        return bool(result.value)

    def close(self):
        if self._plc is not None:
            self._plc.close()
            self._plc = None

    def describe(self):
        info = {}
        try:
            info = self._plc.info or {}
        except Exception:       # noqa: BLE001
            pass
        return f"ControlLogix {self.path} ({info.get('device_type', '?')}) tag {self.tag}"

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

    def __init__(self, ip, rack, slot, address):
        self.ip = ip
        self.rack = int(rack)
        self.slot = int(slot)
        self.address = address
        self._area_name, self._db, self._byte, self._bit = \
            parse_siemens_address(address)
        self.label = f"Siemens {ip} rack {self.rack} slot {self.slot} addr {address}"
        self._client = None
        self._area = None

    def open(self):
        import snap7
        self._area = getattr(snap7.Area, self._area_name)
        self._client = snap7.Client()
        self._client.connect(self.ip, self.rack, self.slot)

    def read_bool(self):
        from snap7.util import get_bool
        # Read the single byte holding our bit, then extract it.
        data = self._client.read_area(self._area, self._db, self._byte, 1)
        return get_bool(data, 0, self._bit)

    def close(self):
        if self._client is not None:
            try:
                self._client.disconnect()
            finally:
                self._client = None

    def describe(self):
        return (f"Siemens S7 {self.ip} (rack {self.rack}, slot {self.slot}) "
                f"{self.address} -> area {self._area_name} "
                f"DB{self._db} byte {self._byte} bit {self._bit}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_LOGIX_ALIASES = {"controllogix", "compactlogix", "logix", "ab",
                  "allen-bradley", "allenbradley", "rockwell", "ethernetip"}
_SIEMENS_ALIASES = {"siemens", "s7", "simatic", "step7", "tia"}


def make_trigger_source(cfg):
    """Build the right TriggerSource from a config dict."""
    plc_type = str(cfg.get("PLC_TYPE", "controllogix")).strip().lower()

    if plc_type in _LOGIX_ALIASES:
        return LogixSource(
            path=cfg.get("PLC_PATH", "10.2.4.1"),
            tag=cfg.get("TRIGGER_TAG", ""),
        )

    if plc_type in _SIEMENS_ALIASES:
        return SiemensSource(
            ip=cfg.get("PLC_PATH", "10.2.4.1"),
            rack=cfg.get("SIEMENS_RACK", 0),
            slot=cfg.get("SIEMENS_SLOT", 1),
            address=cfg.get("TRIGGER_TAG", ""),
        )

    raise ValueError(
        f"Unknown PLC_TYPE {plc_type!r}. "
        f"Use 'controllogix' or 'siemens'."
    )
