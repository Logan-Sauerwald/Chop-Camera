# chopcam aggregator

The other half of the system. One aggregator per install, alongside however
many capture nodes that install needs.

It does three things:

- **Receives clips** from the capture nodes over ssh/scp. The nodes push; this
  machine only has to accept the key and own the directory.
- **Serves the live wall** — a grid of tiles, one per node, on the monitor.
- **Shows which nodes are actually recording**, by polling each node's
  `/healthz`.

It never decodes or encodes video. The browser pulls MJPEG straight from each
node, so a Pi 5 running this stays almost idle no matter how many cameras the
install has.

## Layout

```
chopcam-agg.conf.example   every tunable; the ONLY file that differs per install
install-aggregator.sh      one-shot setup
wall.py                    wall page, node status, clip listing
systemd/                   service unit
```

The capture-node half lives at the repo root (`src/`, `install.sh`,
`chopcam.conf.example`). Both roles read the same config format using the same
parser (`src/chopcam_config.py`), which is why a node and its aggregator cannot
disagree about how a value is spelled.

## Install

```bash
cd aggregator
sudo ./install-aggregator.sh
sudoedit /etc/chopcam-agg.conf        # SITE, NODES, INCOMING_DIR
python3 /opt/chopcam-agg/wall.py --check-config
sudo systemctl enable --now chopcam-wall.service
```

Then open `http://<aggregator>:8090/`.

Adding or removing cameras at an install is one line — `NODES` — and a
restart. Nothing else changes.

```
NODES="uw1=192.168.0.101 uw2=192.168.0.102 uw3=192.168.0.103 uw4=192.168.0.104"
```

## Letting the nodes deliver

On each capture node, create a key if it has none and print it:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Append each node's public key to the aggregator's
`~/.ssh/authorized_keys`, then set on each node in `/etc/chopcam.conf`:

```
SHIP_ENABLED="true"
AGG_OS="linux"                    # a Pi 5 aggregator is "linux"
AGG_USER="<aggregator account>"
AGG_IP="<aggregator address>"
AGG_DIR="/srv/chopcam/incoming"   # must match INCOMING_DIR here
```

Confirm from a node, with **no** password prompt:

```bash
ssh -o BatchMode=yes <agg-user>@<agg-ip> true && echo ok
```

If that prompts, the timer job fails silently every five minutes.

**`AGG_OS` must match this machine.** Set to `windows` against a Linux
aggregator, the upload still succeeds but verification returns nothing, so the
clip is never marked delivered and the *same clip re-ships every timer run
forever* — duplicates piling up here while the node's `encoded/` never drains.
`postprocess.sh` names `AGG_OS` in its log when it sees that signature.

## Endpoints

| Path | What |
|---|---|
| `/` | the wall — live tiles, one per node |
| `/status` | JSON: every node's state, camera, PLC, trigger count, clips here |
| `/clips` | JSON: newest clips delivered, newest first |
| `/healthz` | this aggregator: 200 healthy, 503 if the clip directory is unwritable or no nodes are configured |

Tiles are colour-coded and carry a badge:

- **healthy** — camera streaming and the trigger connected
- **degraded** — reachable, but something is wrong (`/status` says what)
- **unreachable** — no answer at all; the tile is covered, because a dead node
  otherwise keeps showing its last frame and reads as working

Node status is polled here rather than in the browser because the capture
nodes send no CORS headers, so page JavaScript cannot read their `/healthz`
directly. MJPEG `<img>` tags are not CORS-restricted, which is why the video
still comes straight from the nodes.

## On the monitor

Pi OS with the desktop, as the desktop user:

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/chopcam-wall.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=chopcam wall
Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:8090/
DESKTOP
```

Drop `LIVE_FPS` to 5–8 on each node first. The wall holds one MJPEG connection
per tile, and a browser decoding several full-rate 120 fps streams will
struggle long before the network does.

## Not built yet

- **Retention.** Clips accumulate in `INCOMING_DIR`; nothing purges them. At
  roughly 60 MB per clip that is about 1.4 GB per node per day at one chop an
  hour. Use an SSD or NVMe, not an SD card.
- **Playback.** `/clips` lists what has arrived but does not serve it. Copy
  clips off, or point a file manager at the directory.
