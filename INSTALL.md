# Installing a capture node

Takes about 15 minutes on a fresh Raspberry Pi OS install.

## 1. Run the installer

```bash
git clone <repo> chopcam
cd chopcam
sudo ./install.sh
```

It installs ffmpeg and v4l-utils, creates `/opt/chopcam` and
`/var/lib/chopcam`, builds a venv with `pycomm3` (ControlLogix),
`python-snap7` (Siemens) and `pymodbus` — **all three pinned**, because the PLC
drivers are the hardest part of this system to test off-site and an
unannounced major version bump is not something you want to discover at a
panel. It then copies `chopcam.conf.example` to `/etc/chopcam.conf`, adds you
to the `video` group, installs the systemd units rewritten to run as your
account, runs the unit tests, and prints a config check.

Log out and back in afterwards so the `video` group takes effect.

Re-running after a `git pull` is safe. It does **not** restart a running
capture service — that would drop the ring buffer — so it tells you a restart
is pending instead.

## 2. Set the node's static IP

```bash
sudo nmcli con show                     # find the connection name
sudo nmcli con mod "Wired connection 1" ipv4.method manual \
     ipv4.addresses 10.2.4.100/24 ipv4.never-default yes
sudo nmcli con up "Wired connection 1"
ip -brief a                             # confirm eth0 has the address
ping -c3 10.2.4.1                       # confirm the PLC answers
```

`ipv4.never-default yes` keeps Wi-Fi as the default route, so SSH and internet
still work.

## 3. Configure

```bash
sudoedit /etc/chopcam.conf
```

At minimum set `NODE_NAME`, `PLC_TYPE`, `PLC_PATH`, and `TRIGGER_TAG`. Every
option is commented in the file.

`NODE_NAME` ships blank on purpose and the service refuses to start without it:
a node running under a name copied from another Pi produces clips nobody can
trace back to a chop point, and you cannot tell after the fact.

Check the file before going any further — this catches a bad `PLC_TYPE`, an
unparseable Siemens address or a missing `NODE_NAME` in a second, rather than
after the service has been failing quietly:

```bash
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --check-config
```

### If this line uses an Allen-Bradley ControlLogix

```
PLC_TYPE="controllogix"
PLC_PATH="10.2.4.1"                  # add "/1" for the CPU slot in a chassis
TRIGGER_TAG="_R1_156N0:33:O.7"
```

**Confirm the tag name rather than trusting the default** — Rockwell output tags
are often `...:O.Data.7` rather than `...:O.7`, or an alias:

```bash
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --list-tags 156N0
```

That prints the controller name and type (a good check that you're reaching the
right PLC) plus every matching tag. Paste the exact string into `TRIGGER_TAG`.

### If this line uses a Siemens S7

```
PLC_TYPE="siemens"
PLC_PATH="10.2.4.1"
TRIGGER_TAG="DB100.DBX0.7"           # or Q0.7 / I3.2 / M10.3
SIEMENS_RACK="0"
SIEMENS_SLOT="1"                     # S7-1200/1500 = 1, S7-300/400 = 2
```

Address forms: `DB100.DBX0.7` (data block), `Q0.7` or `A0.7` (outputs),
`I3.2` or `E3.2` (inputs), `M10.3` (merkers). Bit index must be 0–7.

**Two TIA Portal settings must be right**, or the TCP connection succeeds and
every read fails — which looks like a wrong address but isn't:

1. CPU properties → Protection & Security → **"Permit access with PUT/GET
   communication from remote partner"** must be enabled.
2. Any DB you read must have **"Optimized block access" disabled**
   (right-click the DB → Properties). Optimized DBs have no absolute byte
   addresses for `DB100.DBX0.7` to resolve against.

There is no tag browsing on S7 — addresses are absolute, so `--list-tags`
doesn't apply.

### Verify the trigger (either family)

```bash
# watches for 30 s by default; pass seconds, or 0 to run until Ctrl-C
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --test-trigger 60
```

This connects, prints the CPU it reached and the current value, then reports
every transition while you toggle the bit or run a chop. If the PLC isn't
connected yet it says so and exits, which is expected.

The summary answers the two questions that decide whether the system will
actually catch a chop:

```
polls        : 1800 in 60.0 s -> 30.0 Hz achieved (POLL_HZ=30)
read latency : min 1.2 ms  median 2.1 ms  max 18.4 ms
rising edges : 4
edge spacing : min 4.9 s  max 7.2 s
pulse width  : min 235 ms  max 512 ms

VERDICT: OK -- shortest pulse 235 ms is 7x the 33 ms poll interval.
```

* **Pulse width vs poll interval.** A trigger pulse shorter than about twice
  the poll interval gets missed intermittently — the tool says so and tells you
  what to raise `POLL_HZ` to. Exit status is non-zero on a marginal or absent
  trigger, so it can be scripted.
* **Achieved rate vs `POLL_HZ`.** `POLL_HZ` is a request; the real ceiling is
  how fast the PLC answers. If reads take 50 ms, a configured 30 Hz is really
  20 Hz, and the verdict is judged against the rate actually achieved rather
  than the one you asked for.

**Confirm the trigger fires once per chop, and that the chop lands near the
middle of the clip.** If a fast chop is missed, the output pulse was shorter
than the poll interval — raise `POLL_HZ` or have controls latch the bit.

## 4. Check the camera

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext | grep -A4 MJPG
```

Confirm `CAMERA_DEVICE` matches and that the configured resolution/fps combination
is actually listed. Nothing else may hold the camera — check with
`sudo fuser -v /dev/video0`.

## 5. Start

```bash
sudo systemctl enable --now chopcam.service
sudo systemctl enable --now chopcam-postprocess.timer
journalctl -u chopcam -f
```

The service logs the configuration it actually loaded on startup — node name,
camera mode, clip geometry, PLC target — so "which value did this node really
read" never needs answering over SSH. Expect `starting camera capture`, `live
preview on port 8080`, and — once the PLC is reachable — `PLC connected`. A few
`camera stream ended; will restart` lines right after boot are normal while USB
enumerates.

If the config is wrong the service refuses to start and says exactly which
setting, rather than starting up half-working.

**Reboot and confirm it comes back on its own.** That is the real test.

### Check node health

```bash
curl -s http://localhost:8080/healthz | python3 -m json.tool
```

Returns HTTP 200 when the camera is delivering frames and the PLC is connected
(or the PLC trigger is switched off), and 503 otherwise — so a wedged node is
visible without reading the journal. `systemctl status` cannot tell you this:
a node whose camera has dropped off still shows as `active`, because the
service is running fine, it just has nothing to record.

```json
{
  "node": "chop1",
  "healthy": true,
  "camera": { "state": "streaming", "frame_age_s": 0.01, "restarts": 0 },
  "buffer": { "frames": 3720, "seconds": 31.0 },
  "plc": { "state": "connected", "poll_hz": 29.9, "configured_poll_hz": 30 },
  "triggers": { "count": 4, "last_utc": "2026-09-09T19:30:12+00:00" },
  "clips": { "written": 4, "failed": 0 }
}
```

## 6. Verify a clip end to end

Fire a trigger without the PLC:

```bash
/opt/chopcam/venv/bin/python -c "
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020); c.connect()
c.write_coil(0, True)
print('coil now:', c.read_coils(0, count=1).bits[0])   # False = ack worked
c.close()"
```

Then follow it through:

```
/var/lib/chopcam/raw/       .mkv appears seconds after the post-roll
                            (written as .<name>.part.mkv, then renamed)
/var/lib/chopcam/encoded/   .mp4 after the next timer run (or force one below)
/var/lib/chopcam/sent/      only when SHIP_ENABLED="true"
```

```bash
sudo systemctl start chopcam-postprocess.service   # force a run now
journalctl -u chopcam-post -n 30
```

Copy a clip off by hand:

```bash
scp <pi>:/var/lib/chopcam/encoded/*.mp4 .
```

## 7. Automatic transfer (optional)

Only when the aggregator exists. Set `SHIP_ENABLED="true"` plus `AGG_USER`,
`AGG_IP`, `AGG_DIR` in `/etc/chopcam.conf`.

For a **Windows** aggregator: enable OpenSSH Server (Settings → Optional
features), then in an Administrator PowerShell:

```powershell
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server" `
  -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

Then key auth from the Pi:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Add that line to `C:\Users\<user>\.ssh\authorized_keys`.

> If the Windows account is an **administrator**, that file is ignored. Use
> `C:\ProgramData\ssh\administrators_authorized_keys` and fix the ACL:
> ```powershell
> icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r `
>   /grant "Administrators:F" /grant "SYSTEM:F"
> ```
> This catches almost everyone the first time.

This must print `ok` with **no prompt**, or the timer job fails silently every
five minutes:

```bash
ssh -o BatchMode=yes user@10.2.4.200 "powershell -NoProfile -Command \"echo ok\""
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `camera stream ended` on a loop | something else holds the camera (`sudo fuser -v /dev/video0`), or the resolution/fps isn't supported |
| `Device or resource busy` | a stray ffmpeg or `guvcview`; `pkill ffmpeg` |
| Service fails instantly, no journal entries | bad `ExecStart` path or the script isn't executable — check `systemctl status`, not `journalctl -u chopcam-post` |
| `/usr/bin/env: 'bash\r'` | CRLF line endings; `dos2unix src/postprocess.sh` |
| PLC `No route to host` | PLC not reachable — normal if it isn't plugged in |
| ControlLogix connects but times out, ping works | chassis slot missing: `PLC_PATH="10.2.4.1/1"` |
| Siemens connects but every read fails | PUT/GET not permitted, or the DB has "optimized block access" on |
| Siemens connection refused | wrong `SIEMENS_SLOT` (S7-1200/1500 = 1, S7-300/400 = 2) |
| `Cannot parse Siemens address` | use `DB100.DBX0.7`, `Q0.7`, `I3.2` or `M10.3`; bit must be 0–7 |
| `PLC config error ... trigger disabled` | bad `PLC_TYPE` or address — this is not retried; fix and restart |
| `Refusing to start -- N configuration problem(s)` | run `--check-config`; it names each one |
| `tag ... reads as DINT, not BOOL` | `TRIGGER_TAG` points at a word, not a bit — append the bit index, e.g. `...:O.Data.7` |
| `PLC polling at N Hz, configured M Hz` | the PLC can't answer that fast; lower `POLL_HZ` or latch the bit |
| `... is already TRUE at connect` | normal after a reconnect on a latched bit; no phantom clip is recorded |
| `pre-roll short by Ns` | the buffer didn't reach back `PRE_SECONDS` — `FPS` is probably set below the camera's real rate |
| `/healthz` returns 503 | camera stalled or PLC disconnected; the JSON says which |
| `hash check failed (remote='empty')` | PowerShell quoting; set `VERIFY_MODE="size"` |
| Disk filling | `SHIP_ENABLED="false"` keeps every clip forever; copy them off and delete |
