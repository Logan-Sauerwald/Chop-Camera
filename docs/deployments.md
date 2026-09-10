# Deployments

One row per capture node. Addresses, tags and even PLC families differ between
machines, so **nothing here is a default** — `chopcam.conf.example` ships those
fields blank on purpose, and the service refuses to start until they are set.

Record every node here as it is commissioned. When a node misbehaves a year
from now, this table is what says which PLC it was ever supposed to be talking
to.

## Why per-node, not per-site

Three things vary independently:

- **Subnet.** Each machine's controls network is its own. A `10.2.4.x` address
  copied onto a `192.168.0.x` machine gives you a node that starts cleanly and
  never triggers.
- **PLC family.** Some lines are Allen-Bradley, some Siemens. `PLC_TYPE` picks
  the driver per node.
- **Whether the PLC is shared.** On the Allen-Bradley line below, one PLC
  serves every chop point and only `TRIGGER_TAG` changes. On the Siemens
  unwinder line, **each station has its own PLC**, so `PLC_PATH` changes too.

That second case is the one that catches people: there is no shared address to
inherit, so both values must be set per node.

## Template

| Node (`NODE_NAME`) | Machine / position | `PLC_TYPE` | `PLC_PATH` | `TRIGGER_TAG` | Node IP | Aggregator | Commissioned |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

`NODE_NAME` must be unique across everything sharing an aggregator. If footage
from several machines might ever be pooled, prefix the machine
(`sl3-uw1`, not `uw1`).

---

## Machine: chop line (Allen-Bradley ControlLogix)

One ControlLogix serving five chop points; only the tag changes per node.

| Node | Position | `PLC_TYPE` | `PLC_PATH` | `TRIGGER_TAG` | Node IP |
|---|---|---|---|---|---|
| chop1 | chop point 1 | `controllogix` | `10.2.4.1` | `_R1_156N0:33:O.7` | 10.2.4.100 |
| chop2 | chop point 2 | `controllogix` | `10.2.4.1` | *(confirm)* | 10.2.4.101 |
| chop3 | chop point 3 | `controllogix` | `10.2.4.1` | *(confirm)* | 10.2.4.102 |
| chop4 | chop point 4 | `controllogix` | `10.2.4.1` | *(confirm)* | 10.2.4.103 |
| chop5 | chop point 5 | `controllogix` | `10.2.4.1` | *(confirm)* | 10.2.4.104 |

Aggregator: `10.2.4.200` (planned).

Rockwell output tags are frequently `...:O.Data.7` rather than `...:O.7`, or an
alias — do not guess, list them:

```bash
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --list-tags 156N0
```

If connect times out but ping works, the CPU is in a chassis and `PLC_PATH`
needs the slot: `"10.2.4.1/1"`.

---

## Machine: unwinder splice knives (Siemens S7)

**A separate PLC per station.** Both `PLC_PATH` and `TRIGGER_TAG` change per
node. Trigger bits are merkers (`M`), not outputs.

| Node | Position | `PLC_TYPE` | `PLC_PATH` | `TRIGGER_TAG` | Signal |
|---|---|---|---|---|---|
| uw1 | UW 1 splice knife | `siemens` | `192.168.0.4` | `M158.7` | splice knife fire |
| uw2 | UW 2 splice knife | `siemens` | `192.168.0.8` | `M143.5` | splice knife fire |
| uw3 | UW 3 splice knife | `siemens` | `192.168.0.13` | `M155.6` | splice knife fire |
| uw4 | UW 4 splice knife | `siemens` | `192.168.0.14` | `M148.7` | splice knife fire |

Still to confirm on this machine:

- **CPU family, for rack/slot.** `SIEMENS_SLOT="1"` suits S7-1200/1500;
  S7-300/400 need `"2"`. A wrong slot shows up as connection refused.
- **PUT/GET permission.** CPU properties → Protection & Security → "Permit
  access with PUT/GET communication from remote partner". Without it the TCP
  connection succeeds and every read fails, which looks like a wrong address
  but is not.
- **Pulse width.** These are momentary fire bits. `--test-trigger` measures how
  long each pulse actually stays high and says whether `POLL_HZ` can catch it.

Merkers need no "optimized block access" change — that only applies to data
blocks (`DB…`). Reading `M` addresses still requires PUT/GET.

Commission each node with:

```bash
sudoedit /etc/chopcam.conf     # NODE_NAME, PLC_TYPE, PLC_PATH, TRIGGER_TAG
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --check-config
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --test-trigger 60
```

---

## Aggregators

One per install. `SITE` here must match `SITE` on that install's nodes, or
delivered clips cannot be matched to the tiles that recorded them.

| Install (`SITE`) | Aggregator IP | `INCOMING_DIR` | Nodes (`NODES`) |
|---|---|---|---|
| | | | |

Each node at that install sets `AGG_IP` to the aggregator and `AGG_DIR` to the
same path as `INCOMING_DIR`.
