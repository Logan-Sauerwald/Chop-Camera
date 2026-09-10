#!/usr/bin/env python3
"""
chopcam_config.py -- the shared config reader.

chopcam.conf is read twice by design: bash `source` in postprocess.sh and this
parser everywhere Python needs it. If the two ever disagree about a value, half
the system runs on settings the other half does not have, silently. That is why
the parsing lives in one place and has a test comparing it to bash key by key.

Used by the capture node (src/capture.py) and by the aggregator
(aggregator/wall.py), which keeps one format across both roles.
"""

import os

CONFIG_SEARCH = [
    os.environ.get("CHOPCAM_CONF"),
    "/etc/chopcam.conf",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chopcam.conf"),
]


def parse_config_value(raw):
    """Parse one bash-style RHS the way `source` would, for our subset.

    Handles  KEY="30"  KEY='30'  KEY=30  and strips trailing comments:
        POLL_HZ="60"   # faster     ->  60
        POLL_HZ=60     # faster     ->  60
        NOTE="a # b"                ->  a # b
    Without this, an inline comment silently made the value unparseable and the
    setting reverted to its built-in default while bash still read it correctly.
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        return raw[1:] if end < 0 else raw[1:end]
    return raw.split("#", 1)[0].strip()


def load_config(path=None, search=None):
    """Parse the shared KEY="value" config that postprocess.sh also sources."""
    candidates = [path] if path else (search or CONFIG_SEARCH)
    for cand in candidates:
        if cand and os.path.isfile(cand):
            cfg = {}
            with open(cand) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[len("export "):].lstrip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    if not key.isidentifier():
                        continue            # not a plain assignment; skip
                    cfg[key] = parse_config_value(val)
            cfg["_path"] = cand
            return cfg
    raise SystemExit(
        "No config found. Copy the example config into place.\n"
        "Searched: " + ", ".join(c for c in candidates if c)
    )
