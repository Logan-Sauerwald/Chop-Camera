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
wall.py                    wall page, player, chop log, node status, clip listing
purge.py                   retention; the only thing here that deletes footage
systemd/                   service and timer units
```

On disk, under `INCOMING_DIR`:

```
incoming/                  clips as the nodes deliver them
incoming/keep/             clips somebody kept; the purge never looks in here
incoming/choplog.jsonl     every trigger any node has reported
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
AGG_USER="<aggregator account>"
AGG_IP="<aggregator address>"
AGG_DIR="/srv/chopcam/incoming"   # must match INCOMING_DIR here
```

Confirm from a node, with **no** password prompt:

```bash
ssh -o BatchMode=yes <agg-user>@<agg-ip> true && echo ok
```

If that prompts, the timer job fails silently every five minutes.

**Verification has to be able to read the file back.** If it cannot, the upload
still succeeds but the clip is never marked delivered, and the *same clip
re-ships every timer run* — duplicates piling up here while the node's
`encoded/` never drains. `postprocess.sh` distinguishes the two signatures: a
verify returning *nothing* means the remote command is broken (check the
account can run `sha256sum` on `INCOMING_DIR`), while a verify returning a
*different* value is real corruption.

## Endpoints

| Path | What |
|---|---|
| `/` | the wall — live tiles, one per node, each with a **Last chop** button |
| `/log` | the same page with the chop log open; bookmarkable, and it starts no live streams |
| `/clip/<file>` | play a clip (Range-capable, so the player can seek) |
| `/clip/<file>?slowmo=1` | download a 4× slow-motion copy |
| `/clip/<file>?download=1` | download the original, true speed |
| `POST /keep/<file>` | move a clip where the purge cannot delete it |
| `POST /unkeep/<file>` | move it back |
| `/status` | JSON: every node's state, camera, PLC, last clip, pending chop, clip shape |
| `/clips` | JSON: newest clips delivered, newest first; `?node=<name>` for one camera |
| `/choplog` | JSON: every trigger any node reported, with what became of it |
| `/healthz` | this aggregator: 200 healthy, 503 if the clip directory is unwritable or no nodes are configured |

Keep and release are `POST`, not `GET`: they move a file, and a `GET` that
changes state would also fire for anything that prefetches links — which on a
page left open on a wall monitor is not theoretical.

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

Each tile carries one button, showing that camera's most recent clip and its
age. Click it and the clip fills the screen, playing at **0.25×**. Older clips
from that camera are reached from inside the player — the tile bar has to stay
readable across a room, so it keeps only the button people actually press.

- **Speed** — 0.1× / 0.25× / 0.5× / 1×
- **Pause** — the button, or the spacebar
- **Step** — `‹` / `›`, or `,` and `.`
- **Jump to chop** — the button, or `c`
- **Keep this clip** — the button, or `k`
- **Back to live** — the button, or Esc
- **Download slow motion** — a 4× slow copy that plays slowly in *any* player
- **Download original speed** — the true-speed file

The bar under the video is the player's own rather than the browser's, so the
**trigger instant can be marked on it** — the red `CHOP` line — and the readout
is in seconds from the chop rather than from the start of the file. The mark
sits at `duration − POST_SECONDS`, measured back from the end because the
post-roll is always complete while the pre-roll can be short if the ring buffer
had not filled; each node publishes its own `POST_SECONDS` at `/healthz`.

Slow motion is not just convenience. On a 60 Hz monitor a 120 fps clip played
at 1× can only show 60 of every 120 frames — half of what the camera captured
is dropped at the display. At 0.25× the clip presents 30 frames a second, so
**every frame actually reaches your eye**. That is why the player opens at
0.25× rather than 1×.

Opening the player — or the chop log — tears down the live MJPEG streams and
restores them on exit. That is deliberate: each tile holds an open connection
and keeps decoding, and leaving several running while the Pi 5 software-decodes
a 120 fps clip is what makes playback stutter. Opening `/log` directly never
starts them at all.

### Older clips

**Older clips**, in the player's top bar, lists that camera's delivered clips
newest first, each with its date and time, its age, its size, and a star if it
is kept; the button carries the count. Clicking a row loads that clip without
leaving the player. It is fed by `/clips?node=<name>`.

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

### Keeping a clip

Retention deletes everything past the window. Left at that, the first clip that
genuinely matters is deleted a week later by a system working exactly as
designed.

**Keep this clip** in the player moves the file to `INCOMING_DIR/keep/`, which
`purge.py` never touches — not on age, not under disk pressure. Nothing else
about the clip changes: it plays, downloads and counts as that camera's last
chop exactly as before, and the button releases it again. Two things enforce
it: the purge's scan is not recursive, and it skips the directory by name as
well.

Every run reports what is protected:

```
[03:00:12] 4 clip(s) kept (612 MB) -- exempt from retention and from disk pressure
```

The one way this can bite is keeping so much that there is nothing left to
free. The purge says so rather than failing quietly:

```
[03:00:12] WARNING: disk 86% full and no deletable clips left
[03:00:12] WARNING: 91 kept clip(s) hold 13904 MB and are never deleted.
           Release some from the wall, or move them off this disk.
```

## The chop log

`http://<aggregator>:8090/log`, or **Chop log** in the header. Every trigger
every camera has reported, whether a clip came of it or not.

The aggregator can only see files, so a chop that produced nothing leaves no
trace on disk anywhere — which is why the log is fed from each node's
`/triggers` rather than from the directory. Nodes remember their own triggers,
but only the last few hundred and only in RAM; draining them here makes the
record survive a node reboot, a node replacement, an aggregator restart, and
the footage itself.

| Clip | Means |
|---|---|
| **on disk** | delivered, and here now |
| **kept** | delivered, and exempt from the purge |
| **in transit** | recorded on the node, still transcoding or on its way |
| **recording** | fired just now; the post-roll is still being recorded |
| **coalesced** | fired while the previous chop was still recording, so that clip covers it |
| **failed** | the node could not record it — usually the camera had stopped |
| **missing** | recorded on the node but never arrived; the pipeline is stuck |
| **purged** | deleted on schedule after passing retention |

The outcome is worked out on every read from what is actually on disk, never
stored, so the log cannot go stale against the filesystem: a clip that was here
yesterday and has since been purged reads as *purged* today without anything
rewriting anything. **purged** and **missing** being different answers is what
makes the log worth reading — one is the system working, the other is a fault.

Settings: `CHOPLOG` (path, defaults to `choplog.jsonl` inside `INCOMING_DIR`),
`CHOPLOG_MAX` (entries kept, default 5000, a couple of hundred bytes each) and
`CHOPLOG_SYNC_SECONDS` (how often a node is re-read even when its trigger count
has not moved, default 30 — a chop goes *recording* to *recorded* a post-roll
later without the count changing).

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

- **Authentication.** Anyone who can reach the aggregator can watch, download,
  keep and release. Fine on an isolated controls network; worth revisiting if
  that changes.
- **Exporting kept clips.** They stay on the aggregator's disk. Getting a set
  of them off it is still `scp`, or the download button one at a time.
