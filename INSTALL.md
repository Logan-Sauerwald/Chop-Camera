# Installing chopcam

One **install** is one aggregator (a Pi 5) plus however many capture nodes
(Pi 4s) that machine needs — one per chop point. Several installs run around
the plant, each on its own network.

Work in this order. The node is useful on its own, so you can commission
cameras before the aggregator exists.

| | |
|---|---|
| **[Part A — a capture node](#part-a--a-capture-node)** | ~20 min per node |
| **[Part B — the aggregator](#part-b--the-aggregator)** | ~15 min, once per install |
| **[Part C — connecting them](#part-c--connecting-them)** | ~10 min |

## Before you start

Collect these first. Every one of them is a per-node value, and guessing wastes
a trip:

- [ ] **`SITE`** — a name for this install, e.g. `110`. Same on every Pi here.
- [ ] **`NODE_NAME`** — this camera, e.g. `UW1`. Unique within the site.
- [ ] **The node's IP**, free on the controls subnet. Check it is not already
      taken.
- [ ] **The PLC's IP** for *this* chop point. On some machines every station
      has its own PLC — do not assume one address serves them all.
- [ ] **The trigger address** — a ControlLogix tag (`_R1_156N0:33:O.7`) or a
      Siemens address (`M158.7`, `DB100.DBX0.7`, `Q0.7`).
- [ ] **Siemens only: the CPU family.** S7-1200/1500 use slot 1, S7-300/400 use
      slot 2. A wrong slot presents as connection refused.
- [ ] **Siemens only: PUT/GET permitted?** CPU properties → Protection &
      Security. Without it the connection succeeds and every read fails, which
      looks like a wrong address. Changing it may need a download to the CPU.

Record them in [`deployments.md`](docs/deployments.md) as you go.

---

# Part A — a capture node

## A1. Install

```bash
git clone https://github.com/Logan-Sauerwald/Chop-Camera.git chopcam
cd chopcam
sudo ./install.sh
```

The installer adds ffmpeg and v4l-utils, creates `/opt/chopcam` and
`/var/lib/chopcam`, builds a venv with `pycomm3` (ControlLogix), `python-snap7`
(Siemens) and `pymodbus` — **all pinned**, because the PLC drivers are the
hardest part to test off-site and an unannounced version bump is not something
to discover at a panel. It then copies the example config to
`/etc/chopcam.conf`, adds you to the `video` group, installs the systemd units,
runs the test suite, and prints a config check.

**Log out and back in.** The `video` group does not apply until you do, and the
camera will not open without it.

> Re-running after a `git pull` is safe and keeps your config. It does **not**
> restart a running capture service — that would drop the ring buffer — so
> restart it yourself when the line can take it.

## A2. Configure

```bash
sudoedit /etc/chopcam.conf
```

Only six fields need changing. Everything else is already right.

| Field | Set to | Note |
|---|---|---|
| `SITE` | `"110"` | the install; same on every Pi here |
| `NODE_NAME` | `"UW1"` | this camera |
| `NODE_IP` | `"192.168.0.101"` | must be free on the controls subnet |
| `PLC_TYPE` | `"siemens"` or `"controllogix"` | **ships as `controllogix`** |
| `PLC_PATH` | `"192.168.0.4"` | *this* chop point's PLC |
| `TRIGGER_TAG` | `"M158.7"` | the bit to watch |

Then check `SIEMENS_SLOT` — `1` for S7-1200/1500, `2` for S7-300/400.

> **`PLC_TYPE` is the field people miss**, because it is the only one that is
> *wrong* rather than blank. Leave it as `controllogix` with a Siemens address
> and the config check still passes — any string is a legal Logix tag name — and
> you find out at connect time, with an error that looks like a network fault.

Clips are labelled `SITE-NODE_NAME`, so `UW1` at two installs stays
distinguishable.

**Check it before going further:**

```bash
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --check-config
```

```
/etc/chopcam.conf: OK
  node   : 110-UW1   (site=110 node=UW1)
  plc    : siemens 192.168.0.4 tag M158.7 @ 30 Hz
  camera : /dev/video0 1920x1080 @ 120
  clip   : -15s/+15s -> /var/lib/chopcam/raw
  buffer : 31s window, 620 MB ceiling
```

If the `plc` line says `controllogix`, go back and fix `PLC_TYPE`.

### Commissioning several nodes

Prepare each config on your laptop and hand it to the installer instead of
editing on the Pi. That is the repeatable path:

```bash
sudo ./install.sh --conf uw1.conf --apply-network
```

`--apply-network` also sets the static IP from `NODE_IP`/`NODE_CIDR`, so
commissioning a node really is one file.

## A3. Network

```bash
sudo nmcli con mod "Wired connection 1" ipv4.method manual \
     ipv4.addresses 192.168.0.101/24 ipv4.never-default yes
sudo nmcli con up "Wired connection 1"
ip -brief a                      # eth0 should show the address
ping -c3 192.168.0.4             # the PLC
```

`ipv4.never-default yes` keeps Wi-Fi carrying the default route, so SSH and Pi
Connect survive putting eth0 on an isolated controls network.

**If ping fails**, read the error carefully — it tells you where the problem is:

| Message | Means |
|---|---|
| `Destination Host Unreachable` **from the Pi's own IP** | ARP failed — nothing at that address is on this wire. Cable, switch port, or wrong VLAN. |
| `Network is unreachable` | no route — the address or netmask is wrong |
| silence, then 100% loss | the host is there but not answering ICMP |

`ip -brief link show eth0` must show **`LOWER_UP`** — that means a cable is
actually live. To see what *is* on the wire: `sudo arp-scan --interface=eth0
--localnet`.

## A4. Camera

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext | grep -A2 "1920x1080"
```

You want `Interval: Discrete 0.008s (120.000 fps)`. Nothing else may hold the
camera — check with `sudo fuser -v /dev/video0`.

## A5. The PLC trigger

This is the step most likely to need someone from controls, so do it before the
camera work.

```bash
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --test-trigger 60
```

Pass `0` to watch until Ctrl-C, which is what you want if you cannot force a
splice on demand.

It connects, names the CPU it reached, prints every transition, then reports:

```
polls        : 1800 in 60.0 s -> 30.0 Hz achieved (POLL_HZ=30)
read latency : min 1.2 ms  median 2.1 ms  max 18.4 ms
rising edges : 4
pulse width  : min 235 ms  max 512 ms

VERDICT: OK -- shortest pulse 235 ms is 7x the 33 ms poll interval.
```

Two things decide whether the system will catch a chop:

- **Pulse width vs poll interval.** A pulse shorter than about twice the poll
  interval is missed intermittently. The tool says so and tells you what to
  raise `POLL_HZ` to. If the bit is a single PLC scan (~10 ms), no poll rate
  saves you — ask controls to latch it for ~250 ms.
- **Achieved rate vs `POLL_HZ`.** `POLL_HZ` is a request; the ceiling is how
  fast the PLC answers. If reads take 50 ms, a configured 30 Hz is really 20 Hz,
  and the verdict is judged against the rate actually achieved.

Exit status is non-zero on a marginal or absent trigger, so it can be scripted.

### When it fails

| Symptom | Cause |
|---|---|
| Connection refused | **wrong `SIEMENS_SLOT`** — try `2` (S7-300/400) or `1` (S7-1200/1500) |
| Connects, every read fails | **PUT/GET not permitted** on the CPU |
| `Cannot parse Siemens address` | use `M158.7`, `DB100.DBX0.7`, `Q0.7`, `I3.2`; bit must be 0–7 |
| ControlLogix times out, ping works | CPU is in a chassis: `PLC_PATH="10.2.4.1/1"` |
| `reads as DINT, not BOOL` | the tag is a word, so the trigger is testing "nonzero" — append the bit index |
| No rising edge seen | nothing chopped, or the wrong address. ControlLogix: `--list-tags` |

Merker (`M`) addresses need PUT/GET, but **not** the "optimized block access"
change — that only applies to data blocks (`DB…`).

## A6. Start it

```bash
sudo systemctl enable --now chopcam.service
sudo systemctl enable --now chopcam-postprocess.timer
journalctl -u chopcam -f
```

The service echoes the config it loaded, then `starting camera capture`, `live
preview on port 8080`, and `PLC connected`. A few `camera stream ended; will
restart` right after boot are normal while USB enumerates.

If the config is wrong it refuses to start and names the setting, rather than
running half-working.

**Reboot and confirm it comes back on its own.** That is the real test.

## A7. Aim the camera

Open `http://<node-ip>:8080/`. The picture fills the window with thirds and a
centre crosshair for framing.

Health, which is what the aggregator reads:

```bash
curl -s localhost:8080/healthz | python3 -m json.tool
```

200 when the camera is delivering frames and the trigger is connected, 503
otherwise. `systemctl status` cannot tell you this — a node whose camera has
dropped off still shows `active`, because the service is running fine, it just
has nothing to record.

## A8. Record a clip without the PLC

```bash
/opt/chopcam/venv/bin/python -c "
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
c.write_coil(0, True); c.close(); print('triggered')"
```

A `.mkv` appears in `/var/lib/chopcam/raw/` after the post-roll. The timer then
transcodes it to `.mp4` in `encoded/` — **that takes a couple of minutes**, it
is the slow step. Force a run rather than waiting:

```bash
sudo systemctl start chopcam-postprocess.service
journalctl -u chopcam-postprocess -n 30
ls -lh /var/lib/chopcam/encoded/
```

## A9. Check you are really getting 120 fps

```bash
journalctl -u chopcam | grep saved
```

```
saved ... (3600 frames, 120 fps, measured 120.00, 419 MB, 4.6s)
```

**`measured` is the number that matters.**

| `measured` | Means |
|---|---|
| ~119–120 | full rate, nothing to tune |
| ~60–70 | auto-exposure is stretching the shutter past the 8.33 ms a 120 fps frame allows |
| well under 60 | look closer — USB bandwidth or a busy CPU |

If it is the middle row, pin the camera controls in `/etc/chopcam.conf`:

```
CAMERA_CONTROLS="auto_exposure=1,exposure_time_absolute=40,focus_automatic_continuous=0,white_balance_automatic=0,backlight_compensation=0,power_line_frequency=2"
```

`exposure_time_absolute` is in 100 µs units, so `40` = 4 ms. It must be under
`83` (8.3 ms) to hold 120 fps. Lower is sharper but darker — **add light rather
than lengthening exposure.** `power_line_frequency=2` is 60 Hz; the camera
defaults to 50.

This is a per-install setting because it depends on the light at that camera.
See [`hardware.md`](docs/hardware.md) for the full reasoning.

> A full rate does not by itself mean a sharp picture: at 120 fps the shutter is
> at most 8.33 ms, and a blade moving 1 m/s still smears ~8 mm across a frame.
> If the chop looks soft when you review it, that is exposure — try `10`–`20`.

---

# Part B — the aggregator

One per install, on a Pi 5. Full detail in
[`../aggregator/README.md`](aggregator/README.md).

```bash
cd chopcam/aggregator
sudo ./install-aggregator.sh
sudoedit /etc/chopcam-agg.conf
```

Three fields:

```
SITE="110"                              # must match the nodes
NODES="UW1=192.168.0.101 UW2=192.168.0.102"
INCOMING_DIR="/srv/chopcam/incoming"
```

`RETENTION_DAYS` defaults to 7. Then:

```bash
python3 /opt/chopcam-agg/wall.py --check-config
sudo systemctl enable --now chopcam-wall.service
sudo systemctl enable --now chopcam-purge.timer
```

Open `http://<aggregator>:8090/`. Adding a camera later is one entry in `NODES`
and a restart — see [`wall-layouts.md`](docs/wall-layouts.md) for how the tiles
arrange themselves, and for the player and the chop log.

Two things the installer sets up that are worth knowing about:

- `INCOMING_DIR/keep/` — the player's **Keep this clip** moves a clip here and
  the purge never touches it, however old it gets. That is how footage outlives
  `RETENTION_DAYS` without changing the number.
- `INCOMING_DIR/choplog.jsonl` — every trigger every node reports, whether a
  clip came of it or not. Read it at `http://<aggregator>:8090/log`.

---

# Part C — connecting them

The nodes push clips to the aggregator over ssh. On **each node**:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519    # if it has no key
cat ~/.ssh/id_ed25519.pub
```

Append each node's public key to the aggregator's `~/.ssh/authorized_keys`,
then set on each node in `/etc/chopcam.conf`:

```
SHIP_ENABLED="true"
AGG_USER="<aggregator account>"
AGG_IP="<aggregator address>"
AGG_DIR="/srv/chopcam/incoming"     # must match INCOMING_DIR
```

Confirm from a node, with **no password prompt**:

```bash
ssh -o BatchMode=yes <agg-user>@<agg-ip> true && echo ok
```

If that prompts, the timer job fails silently every minute. The service refuses
to start with `SHIP_ENABLED="true"` and any of the `AGG_*` fields blank, because
a half-configured target is a way to lose footage — the node deletes its local
copy once delivery verifies.

Watch a clip arrive:

```bash
journalctl -u chopcam-postprocess -f          # on the node
curl -s http://<aggregator>:8090/clips | python3 -m json.tool
```

## Viewing from a laptop

Plug into the same switch and open `http://<aggregator>:8090/`. Same page, same
buttons. `http://<aggregator>:8090/log` opens straight to the chop log and
starts no live streams, which is the lighter way in over the plant switch.

- The laptop needs to reach **the nodes as well as the aggregator** — the live
  tiles stream straight from each camera. Playback and downloads come from the
  aggregator either way.
- If the controls network has no DHCP, give the laptop a static IP on that
  subnet.

A laptop is the better place to review carefully: it has hardware H.264 decode,
which the Pi 5 does not.

---

# Troubleshooting

| Symptom | Cause |
|---|---|
| `Refusing to start -- N configuration problem(s)` | run `--check-config`; it names each one |
| `camera stream ended` on a loop | something else holds the camera (`sudo fuser -v /dev/video0`), or the resolution/fps isn't supported |
| `Device or resource busy` | a stray ffmpeg or `guvcview`; `pkill ffmpeg` |
| Service fails instantly, no journal entries | bad `ExecStart` path or the script isn't executable — check `systemctl status` |
| `journalctl -u chopcam-post` shows nothing | wrong name — the unit is `chopcam-postprocess`. `chopcam-post` is only the log tag, so `journalctl -t chopcam-post` matches that |
| `/usr/bin/env: 'bash\r'` | CRLF line endings; `dos2unix src/postprocess.sh` |
| PLC `No route to host` | not reachable — normal if it isn't plugged in |
| Siemens connection refused | wrong `SIEMENS_SLOT` (S7-1200/1500 = 1, S7-300/400 = 2) |
| Siemens connects but every read fails | PUT/GET not permitted, or a DB has "optimized block access" on |
| ControlLogix times out, ping works | chassis slot missing: `PLC_PATH="10.2.4.1/1"` |
| `PLC config error ... trigger disabled` | bad `PLC_TYPE` or address — not retried; fix and restart |
| `PLC polling at N Hz, configured M Hz` | the PLC can't answer that fast; lower `POLL_HZ` or latch the bit |
| `... is already TRUE at connect` | normal after a reconnect on a latched bit; no phantom clip recorded |
| `pre-roll short by Ns` | normal for a trigger in the first seconds after start; otherwise the ring hit `BUFFER_MAX_MB` |
| `/healthz` returns 503 | camera stalled, trigger disconnected, or the ring is memory-bound; the JSON says which |
| `modbus trigger DISABLED` | port busy or pymodbus missing — capture keeps running regardless |
| `hash MISMATCH` on shipping | the copy on the aggregator differs — truncated or corrupt |
| Chop log says **missing** | a clip was recorded on the node and never arrived; check `journalctl -u chopcam-postprocess` there |
| Chop log says **purged** | it passed `RETENTION_DAYS` and was deleted on schedule — not a fault. Use **Keep this clip** on anything worth holding |
| Chop log is empty | nodes older than the log, or unreachable; `curl http://<node>:8080/triggers` should answer |
| `WARNING: ... kept clip(s) ... never deleted` | kept clips now fill the disk; release some from the player, or move them off |
| `... got no answer` on shipping | the verify command returned nothing; check the aggregator account can run `sha256sum` |
| Clip re-ships every run | it never verified — see the two rows above |
| Wall tile grey with no explanation | that node's `/healthz` is unreachable; check the node |
| `Chop delayed` on the wall | a chop fired and never arrived — check `journalctl -u chopcam-postprocess` on that node |
| Disk filling on a node | `SHIP_ENABLED="false"` keeps every clip forever; nothing purges until shipping is on |
