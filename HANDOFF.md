# Handoff notes

Written by the original developer at the end of the project. This is the
reasoning behind the design and the measurements that produced it — the things
that took weeks to learn and would otherwise have to be rediscovered.

If you change something here and it breaks, the explanation is probably below.

---

## Measurements (all on the actual hardware)

| What | Value | How it was measured |
|---|---|---|
| MJPEG bitrate, 1080p120 | **87–118 Mbps** | `ffmpeg -c copy` to file, 10 s, varies with scene detail |
| Sustained frame rate, 1080p120 | **true 120 fps** | `-c copy` reported `fps=120`; 3599/3600 frames in a 30 s clip |
| Frame rate when *decoding* | ~87 fps | decoding is the bottleneck, not USB — see below |
| `libx264` speed, 1080p120 material | **~6 fps** (≈10 min per 30 s clip) | timed `recording:` → `saved` in the journal |
| H.264 clip size, 30 s @ 120 fps | **~60 MB** (16 Mbps) | `ffprobe` on a real clip |
| MJPEG clip size, 30 s @ 120 fps | **~350 MB** | same clip before transcode |
| USB bus | **USB 2.0, 480 Mbps, single shared hub** | `lsusb -t`, all four ports |

### The 87 vs 120 fps trap

An early test with `-f null -` reported 87 fps and looked like a USB limit.
It was not — that path *decodes* every frame, and decoding is what capped it.
Re-running with `-c copy` gave a true 120 fps. **When measuring camera
throughput, always use `-c copy`,** or you will measure ffmpeg instead of the
camera.

---

## Why the design looks like this

### Frames are kept compressed in RAM

The ring buffer stores JPEG bytes, never decoded arrays. A decoded 1080p frame
is ~6 MB; at 120 fps for 30 s that would be **over 200 GB**. As JPEG it is
~400 MB. This is why `cv2.VideoCapture` is not used — it decodes every frame
and throws the JPEG away.

### Encoding is deferred, not skipped

`capture.py` writes MJPEG with `-c copy` (~0.1 s, no pixel work). `libx264`
runs ~6 fps on this material, so encoding inline would peg the CPU for ten
minutes *while the capture thread is trying to feed a 120 fps buffer* — the
capture would drop frames. So `postprocess.sh` does the H.264 conversion on a
timer at `Nice=19` / idle I/O, where the kernel hands the CPU straight back to
capture whenever a frame arrives.

**The H.264 encode still happens on the Pi.** Deferred ≠ moved.

### Hardware H.264 encoding does not work here

`h264_v4l2m2m` on the Pi 4 fails with `VIDIOC_STREAMON failed` / `Invalid
argument (-22)` for this pipeline. Cause: **the camera outputs `yuvj422p`
(4:2:2) and the Pi's hardware encoder only accepts 4:2:0.** A synthetic
`testsrc` (rgb24) encodes fine, which makes the encoder look healthy — it is
the format mismatch, not the encoder.

A `-vf format=yuv420p` conversion in front of it may work, but that conversion
is itself per-frame CPU work, so the win over `libx264` shrinks. Not pursued.
**A Pi 5 will not help: it has no hardware H.264 encoder at all.**

### Clips are MKV, not MP4, before transcode

MJPEG inside MP4 is poorly supported by players. `postprocess.sh` looks for
`.mkv`. After transcode, H.264 goes in `.mp4`, which plays everywhere.

### Delivery is verified by content, not exit status

`scp` can exit 0 while the far end truncated the file (out of disk). Since the
node deletes its local copy after transfer, a false success would lose footage
permanently. Both ends are compared (SHA-256, or size) before anything local is
moved aside.

### The transcode verifies frame count

`postprocess.sh` refuses to delete the raw file if the H.264 output has fewer
frames than the input. This guards against a silent 120→30 fps drop, which
would produce a clean-looking file that has thrown away the slow-motion detail
the entire system exists to capture.

### Two PLC families are supported

Different lines here use different PLCs, so `src/plc.py` puts both behind one
`TriggerSource` interface and `capture.py` polls it without caring which is on
the other end. `PLC_TYPE` in the config selects the driver. Adding a third
family means subclassing `TriggerSource` and registering it in
`make_trigger_source()` — nothing in the capture path changes.

**Allen-Bradley ControlLogix / CompactLogix** — EtherNet/IP via `pycomm3`.
Watches a named tag. Use `--list-tags` to find the real name: Rockwell output
tags are frequently `...:O.Data.7` rather than `...:O.7`, or an alias.

**Siemens S7-300/400/1200/1500** — ISO-on-TCP (port 102) via `python-snap7`.
Watches an absolute address: `DB100.DBX0.7`, `Q0.7`/`A0.7` (outputs),
`I3.2`/`E3.2` (inputs), or `M10.3` (merkers). Both English and German
mnemonics are accepted. There is no tag browsing — S7 addresses are absolute,
so `--test-trigger` is the way to confirm one.

Siemens specifics that cost time if you don't know them:

- **`python-snap7` 3.x is pure Python.** No `libsnap7` native library, despite
  what most guides online say. `pip install python-snap7` is the whole install.
- **Rack/slot differ by CPU family.** S7-1200/1500 are usually rack 0 slot 1;
  S7-300/400 are rack 0 slot 2. Wrong slot = connection refused.
- **PUT/GET must be permitted.** On S7-1200/1500, CPU properties → Protection &
  Security → "Permit access with PUT/GET communication from remote partner".
  Without it the TCP connection succeeds and every read fails, which looks like
  an addressing problem but isn't.
- **DBs must not be "optimized".** Optimized block access gives a DB no absolute
  byte addresses, so `DB100.DBX0.7` cannot resolve. Right-click the DB →
  Properties → uncheck "Optimized block access".
- **Bit index is 0–7.** `parse_siemens_address` rejects anything higher rather
  than letting a typo become a confusing read error at runtime.

A malformed address or unknown `PLC_TYPE` is a configuration error, not a
transient one, so the poll loop logs it and **stops** instead of retrying
forever. Capture, preview and the Modbus bench trigger keep running.

---

## Why one Pi per camera

Two independent reasons.

**Bandwidth.** All four USB ports on a Pi 4 hang off *one* internal hub on a
single 480 Mbps bus (`lsusb -t` confirms it — Bus 002, the USB 3.0 controller,
sits unused because the cameras are USB 2.0 devices). Real-world isochronous
throughput is ~300–350 Mbps. At 87–118 Mbps per stream that is two cameras,
maybe three at reduced settings, never five. UVC reserves bandwidth up front,
so an over-subscribed camera doesn't degrade — it refuses to start
(`No space left on device` on STREAMON, which is a bandwidth error despite the
wording).

**Distance.** The five chop points span roughly 50 ft. USB 3.0 is spec'd to
~3 m per segment, USB 2.0 to 5 m. Covering that needs USB-over-Ethernet
extenders at $100–150 each — *more* than the $55–70 Pi you would otherwise put
at that camera, plus a device that fails intermittently under sustained
isochronous load. A Pi at each camera uses a short USB cable and sends video
over Ethernet, which is what Ethernet is for.

Options that were considered and rejected:

- **One box with 5 cameras (Pi 5 or N100 mini PC).** Attractive for simplicity,
  but requires extenders for the distant cameras, and makes one failure take
  out all five chop points.
- **USB extenders through a switch.** Does not work. USB-over-Ethernet
  extenders are point-to-point; they use Cat6 as a medium but do not speak
  Ethernet, so a switch drops the signal.
- **2–3 cameras per Pi.** Fine on bandwidth for two, but only helps if those
  cameras are physically close together. They aren't.
- **GigE Vision industrial cameras.** The right tool (PoE, 100 m runs, global
  shutter) but $500–1500 per camera.

---

## Known issues and gotchas

- **`/dev/video0` is not stable across reboots** if more than one camera is
  ever attached to a node. With one camera per Pi this is fine; if that changes,
  add udev rules pinning cameras by USB port path or clips will silently get
  mislabelled.
- **Anything else holding the camera blocks capture.** `guvcview` left open
  caused a confusing restart loop. `sudo fuser -v /dev/video0` shows the holder.
  `KillMode=control-group` in the unit prevents the service's own ffmpeg from
  becoming that holder.
- **pymodbus is pinned to `>=3.8,<3.9`.** 3.9+ rewrote the datastore; the
  callback-datablock pattern the bench trigger uses no longer works there.
- **`StartLimitIntervalSec` belongs in `[Unit]`.** systemd silently ignores it
  in `[Service]` — it looks correct and does nothing.
- **Windows OpenSSH + admin accounts.** If the aggregator account is an
  administrator, Windows ignores `~/.ssh/authorized_keys` and uses
  `C:\ProgramData\ssh\administrators_authorized_keys` (with strict ACLs).
- **Editing `postprocess.sh` in Notepad** gives it CRLF line endings and bash
  fails with `/usr/bin/env: 'bash\r': No such file or directory`. Use `nano` on
  the Pi, or `dos2unix`.
- **The camera is 25 mm (narrow, ~12° horizontal).** The view is tight because
  of the lens, not the software. It is a screw-in M12 mount — a 6 mm or 4 mm
  lens widens it considerably.
- **1080p is a cropped sensor readout.** That is why it reaches 120 fps while
  full-sensor modes (2592×1944, 3840×3040) cap at 20–30. Wider field of view
  and 120 fps are mutually exclusive on this camera.
- **Rolling shutter.** These modules are almost certainly rolling shutter, which
  skews fast-moving objects in slow motion. If a blade looks bent, that is the
  sensor and no software change fixes it — it needs a global-shutter camera.

---

## Open items, roughly in order

1. **Verify the PLC trigger against real hardware.** Neither driver has been
   run against a live PLC. Start with `capture.py --test-trigger`, which
   connects, prints the current value, and then watches for 10 s so you can
   toggle the bit and see it change.
   - *ControlLogix:* run `--list-tags 156N0` first; the real tag may be
     `...:O.Data.7` or an alias. If connect times out but ping works, the PLC
     is in a chassis and `PLC_PATH` needs the CPU slot (`"10.2.4.1/1"`).
   - *Siemens:* check rack/slot, PUT/GET permission, and that the DB is not
     "optimized" — see the PLC section above.
2. **Confirm the trigger fires once per chop**, and that the chop lands near
   the middle of the clip. If a fast chop is missed, the output pulse was
   shorter than the ~33 ms poll interval — raise `POLL_HZ` or have controls
   latch the bit.
3. **Test the transfer end to end.** The PowerShell `Get-FileHash` call over
   SSH is written but untested; quoting through ssh → cmd → powershell is
   finicky. If the log shows `hash check failed (remote='empty')`, that's the
   quoting — set `VERIFY_MODE="size"`, which still catches the realistic
   failure since SSH guarantees integrity in transit.
4. **Build the aggregator.** Pi 5 + NVMe. The preview wall is mostly free: each
   node already serves MJPEG-over-HTTP, so a page with five `<img
   src="http://10.2.4.10X:8080/stream">` tags in Chromium kiosk mode is close
   to the whole job. Drop `LIVE_FPS` to 5–8 per node so the display isn't
   decoding five full-rate streams.
5. **Storage.** ~7 GB/day across five nodes at one chop/hour. A 1 TB SSD holds
   ~5 months. Use an SSD, not an SD card — SD cards wear out under continuous
   writes and fail in ways that lose data.
6. **Decide `PLAYBACK_MODE`.** `realtime` keeps true 120 fps (scrub to
   inspect); `slowmo` retimes to 30 fps so the clip *plays* at 4× slow motion in
   any player. If reviewers will just double-click the file, `slowmo` is
   probably what they want.

## If you only read one thing

Capture is the part that must not fail. Everything expensive — encoding,
transfer, purging — was deliberately pushed out of the capture path and onto a
timer running at idle priority. If you add work to the system, add it to
`postprocess.sh`, not `capture.py`.
