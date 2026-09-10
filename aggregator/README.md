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
| `/` | the wall — live tiles, one per node, each with a **Last chop** button |
| `/clip/<file>` | play a clip (Range-capable, so the player can seek) |
| `/clip/<file>?slowmo=1` | download a 4× slow-motion copy |
| `/clip/<file>?download=1` | download the original, true speed |
| `/status` | JSON: every node's state, camera, PLC, last clip, pending chop |
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

## Watching a chop

Each tile carries a button showing that camera's most recent clip and its age.
Click it and the clip fills the screen, playing at **0.25×**.

- **Speed** — 0.1× / 0.25× / 0.5× / 1×
- **Pause** — the button, or the spacebar
- **Back to live** — the button, or Esc
- **Download slow motion** — a 4× slow copy that plays slowly in *any* player
- **Original speed** — the true-speed file

Slow motion is not just convenience. On a 60 Hz monitor a 120 fps clip played
at 1× can only show 60 of every 120 frames — half of what the camera captured
is dropped at the display. At 0.25× the clip presents 30 frames a second, so
**every frame actually reaches your eye**. That is why the player opens at
0.25× rather than 1×.

Opening the player tears down the live MJPEG streams and restores them on
exit. That is deliberate: each tile holds an open connection and keeps
decoding, and leaving several running while the Pi 5 software-decodes a
120 fps clip is what makes playback stutter.

### The button's states

| Shows | Means |
|---|---|
| **Last chop · 12m** | that clip is here and ready to play |
| **Chop processing · 3m** | the trigger fired; the node is still transcoding |
| **Chop sending · 4m** | transcoded, waiting to be delivered |
| **Chop delayed · 42m** | fired long ago and never arrived — **something is wrong** |
| **No clips yet** | nothing delivered from this camera |

A chop takes a few minutes to appear: the node records the post-roll, then
transcodes to H.264, which is minutes of Pi 4 CPU, then ships. The button says
where it is rather than looking broken while you wait.

**"Chop delayed" is a fault, not a slow day.** It means a trigger fired and the
clip never arrived — a wedged transcode, a full disk, broken key auth. Nothing
else in the system notices that; check `journalctl -u chopcam-postprocess` on that
node.

## From a laptop

Plug into the same switch and open `http://<aggregator>:8090/`. Same page, same
buttons, same downloads.

Two things to know:

- The laptop needs to reach **the nodes as well as the aggregator** — the live
  tiles stream straight from each camera. Playback and downloads come from the
  aggregator, so those work either way.
- If the controls network has no DHCP, give the laptop a static IP on that
  subnet.

A laptop is the better place to review carefully: it has hardware H.264 decode,
where the Pi 5 does not, so 1× playback and heavy seeking behave better.

For frame-by-frame work, download the clip and open it in **mpv** — `,` and `.`
step exactly one frame back and forward. VLC's `E` only steps *forward*, with
no reliable way back, which is maddening when you are hunting the exact frame
of contact.

## Retention

`RETENTION_DAYS` in `/etc/chopcam-agg.conf`, default 7. That one number is the
whole interface:

```bash
sudoedit /etc/chopcam-agg.conf     # RETENTION_DAYS="14"
```

The purge timer re-reads the file every run, so the change takes effect on the
next pass — no restart, nothing to reload. See what it would do first:

```bash
python3 /opt/chopcam-agg/purge.py --dry-run
```

Age comes from the timestamp in each clip's filename, not the file's mtime, so
a node delivering a backlog after being offline does not reset the clock on old
footage.

`DISK_PCT_LIMIT` (default 85) is the guard: if the filesystem reaches it,
oldest clips go first regardless of age, so a retention window that turns out
to be too long for the disk cannot fill it. Values below 50 are clamped up — a
typo there would otherwise empty the archive on a healthy disk.

Sizing, at roughly 60 MB per clip:

| Chops/hour | 4 nodes, 7 days | 8 nodes, 7 days |
|---|---|---|
| 1 | ~40 GB | ~81 GB |
| 2 | ~81 GB | ~161 GB |
| 6 | ~242 GB | ~484 GB |

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

- **Browsing older clips.** The wall plays the *latest* chop per camera.
  `/clips` lists everything delivered, but there is no UI for picking an older
  one — download it from that listing, or copy it off the disk.
- **Authentication.** Anyone who can reach the aggregator can watch and
  download. Fine on an isolated controls network; worth revisiting if that
  changes.
