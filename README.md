# chopcam

PLC-triggered high-speed video capture for chop-point monitoring.

Each capture node continuously buffers 120 fps video in RAM. When the PLC
energizes that chop point's output, the node saves a 30-second clip spanning
15 seconds *before* and 15 seconds *after* the trigger — so the event is
already recorded by the time anything reacts to it. Clips are converted to
H.264 and collected on an aggregator for review in slow motion.

## Status

| Piece | State |
|---|---|
| Capture, ring buffer, clip writing | **working** |
| Live preview (HTTP) | **working** |
| Modbus bench trigger | **working** |
| Auto-start on boot, crash recovery | **working** |
| H.264 transcode on a timer | **working** |
| PLC trigger — ControlLogix (EtherNet/IP) | **written, never tested against a real PLC** |
| PLC trigger — Siemens S7 (ISO-on-TCP) | **written, never tested against a real PLC** |
| Transfer to aggregator | **written, never tested end to end** |
| Aggregator / preview wall | **not built** |
| Nodes 2–5 | **not purchased** |

Read `HANDOFF.md` before changing anything. It records what was measured, what
was tried and rejected, and why the design looks the way it does.

## Architecture

```
  camera ──USB──> Pi 4 (node)  ──Ethernet──┐
  camera ──USB──> Pi 4 (node)  ──Ethernet──┤
  camera ──USB──> Pi 4 (node)  ──Ethernet──┼──> switch ──> Pi 5 aggregator
  camera ──USB──> Pi 4 (node)  ──Ethernet──┤                 - holds clips
  camera ──USB──> Pi 4 (node)  ──Ethernet──┘                 - preview wall
                       ▲
                       └── polls its own boolean on the PLC.
                           ControlLogix (EtherNet/IP) or Siemens S7
                           (ISO-on-TCP) — set PLC_TYPE per node.
```

One Pi per camera. That is a deliberate choice, not an accident — see
"Why one Pi per camera" in `HANDOFF.md`.

Inside a node:

```
  ffmpeg ──MJPEG frames──> ring buffer (RAM, ~400 MB, 30 s at 120 fps)
                                │
   PLC tag goes TRUE ───────────┤
                                ▼
                    writer: MJPEG stream-copy to disk   (~0.1 s, no CPU)
                                │
              postprocess timer (every 5 min, idle priority)
                                ▼
                    H.264 transcode ──> ship ──> purge
```

The split matters: capture must never drop frames, and `libx264` on a Pi 4
takes about **ten minutes** per clip. So the writer only copies compressed
bytes, and all encoding happens later at idle priority.

## Layout

```
chopcam.conf.example   every tunable; the ONLY file that differs between nodes
install.sh             one-shot node setup
src/capture.py         capture service (buffer, triggers, preview, clip writer)
src/plc.py             PLC drivers: ControlLogix + Siemens behind one interface
src/postprocess.sh     transcode, ship, purge
systemd/               service + timer units
docs/hardware.md       parts, measurements, camera and lens notes
HANDOFF.md             design reasoning and open items — read this
INSTALL.md             step-by-step deployment
```

## Quick start

```bash
git clone <repo> && cd chopcam
sudo ./install.sh
sudoedit /etc/chopcam.conf          # NODE_NAME, PLC_PATH, TRIGGER_TAG
sudo systemctl enable --now chopcam.service
sudo systemctl enable --now chopcam-postprocess.timer
journalctl -u chopcam -f
```

Live preview: `http://<pi>:8080/`

Fire a test clip without the PLC:

```bash
/opt/chopcam/venv/bin/python -c "
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
c.write_coil(0, True); c.close(); print('triggered')"
```

## Adding nodes 2–5

Clone, `sudo ./install.sh`, then change these in `/etc/chopcam.conf`:
`NODE_NAME`, `PLC_TYPE`, `PLC_PATH`, `TRIGGER_TAG` — plus the node's static IP
(set via NetworkManager, not this file). Everything else is identical.

Different lines use different PLCs, so `PLC_TYPE` is per node:

```
PLC_TYPE="controllogix"   TRIGGER_TAG="_R1_156N0:33:O.7"
PLC_TYPE="siemens"        TRIGGER_TAG="DB100.DBX0.7"   # + SIEMENS_RACK/SLOT
```

Verify the trigger before starting the service — works for either family:

```bash
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --test-trigger
```
