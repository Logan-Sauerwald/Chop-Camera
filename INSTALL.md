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
`python-snap7` (Siemens) and a pinned `pymodbus`,
copies `chopcam.conf.example` to `/etc/chopcam.conf`, adds you to the `video`
group, and installs the systemd units rewritten to run as your account.

Log out and back in afterwards so the `video` group takes effect.

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
/opt/chopcam/venv/bin/python /opt/chopcam/src/capture.py --test-trigger
```

This connects, prints the current value, then watches for 10 seconds — toggle
the bit and confirm it flips. If the PLC isn't connected yet it says so and
exits, which is expected.

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

Expect `starting camera capture`, `live preview on port 8080`, and — once the
PLC is reachable — `PLC connected`. A few `camera stream ended; will restart`
lines right after boot are normal while USB enumerates.

**Reboot and confirm it comes back on its own.** That is the real test.

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
ssh -o BatchMode=yes user@10.2.4.50 "powershell -NoProfile -Command \"echo ok\""
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
| `hash check failed (remote='empty')` | PowerShell quoting; set `VERIFY_MODE="size"` |
| Disk filling | `SHIP_ENABLED="false"` keeps every clip forever; copy them off and delete |
