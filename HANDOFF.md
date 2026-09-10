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
| Default Linux pipe capacity | **64 KiB = ~4 ms of stream** | 118 Mbps / 65536 B; see "The capture pipe" |
| Non-integer mux frame rates | **silently corrupt the timeline** | ffmpeg 6.1.1; see "Clips are muxed at a whole-number frame rate" |
| Ring buffer, 1080p120, 31 s window | **3721 frames, 436 MB** | measured steady state; matches the ~400 MB estimate above |
| Ring buffer append cost | **0.9 µs/frame** | 14400 appends of 123 KB; 0.01% of the 8.3 ms budget at 120 fps |

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
~436 MB measured. This is why `cv2.VideoCapture` is not used — it decodes every
frame and throws the JPEG away.

### The ring buffer is bounded by time, not frame count

The buffer trims on the timestamps it already stores, keeping
`PRE + POST + 1` seconds whatever rate the camera actually runs at.

It used to be a `deque(maxlen=FPS * (PRE + POST) + FPS)`, which quietly assumed
the configured `FPS` matched reality. It does not have to: `FPS` is a *request*
to the camera, and v4l2 is free to give you something else — and the 87-vs-120
trap above is exactly a case where the real rate is not the configured one. Set
`FPS="60"` on a camera delivering 120 and the deque held 1860 frames, which at
the real rate is **15.5 seconds of a 31 second window**. Every clip then had
roughly half the pre-roll it claimed, with nothing in the log and a
correct-looking file — the failure this system exists to prevent, caused by one
wrong number in a config file.

Measured, feeding a minute of frames at the *real* rate into both designs
(window 31 s):

| Configured `FPS` | Real rate | Old `maxlen` deque | Time-bounded ring |
|---|---|---|---|
| 120 | 120 | 31.0 s | 31.0 s |
| 60 | 120 | **15.5 s** | 31.0 s |
| 30 | 120 | **7.7 s** | 31.0 s |
| 120 | 87 | **42.7 s** (~630 MB) | 31.0 s |

The last row is the same bug in the other direction, and it is the case the
87-vs-120 trap actually produces: with `FPS="120"` configured and the camera
decode-limited to 87, the old buffer held 42.7 s instead of 31 — about 630 MB
rather than 436 MB, quietly spending 45% more of the `MemoryMax` headroom than
anyone intended. Time bounding fixes both directions at once.

A second bound caps the buffer in **bytes** (`BUFFER_MAX_MB`, default
20 MB/s × the window = 620 MB at 15+15). That is what `maxlen` was really
providing: protection against a stream fatter than expected pushing the service
into systemd's `MemoryMax`. Measured steady state at 1080p120 is 3721 frames /
436 MB over 31 s, so the byte ceiling has ~40% headroom and never binds in
normal running — the time bound is what trims.

When the byte ceiling *does* evict a frame that was still inside the time
window, the pre-roll genuinely is short. That is reported, not hidden: a
throttled warning in the journal, `buffer.memory_evictions` in `/healthz`, and
the node reports unhealthy. `capture.py` also reads the service's cgroup memory
limit at startup and warns if `BUFFER_MAX_MB` is too close to it, so lengthening
`PRE_SECONDS`/`POST_SECONDS` without raising `MemoryMax=` says so up front
instead of being OOM-killed on the first busy scene.

Appending costs 0.9 µs per frame, against an 8.3 ms budget at 120 fps.

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

### The capture pipe is widened to 1 MiB

`capture.py` reads MJPEG from ffmpeg over a pipe. Linux pipes default to
64 KiB, which at the measured 118 Mbps is only **~4 ms** of stream. If the
reader thread is descheduled for longer than that — a GC pause, a busy moment
on the preview server, the GIL held elsewhere — ffmpeg blocks on write, v4l2
backs up, and frames are dropped. `_widen_pipe()` raises it to 1 MiB
(`F_SETPIPE_SZ`, the default unprivileged ceiling), which is ~70 ms of slack
instead. This is why `Nice=-5` alone was never quite enough insurance.

If it can't widen the pipe it logs a warning and carries on; `/proc/sys/fs/
pipe-max-size` is the limit if you ever need more.

### Clips are muxed at a whole-number frame rate

The writer measures the real frame rate from the buffer's timestamps — it is
never exactly 120 — but it rounds before handing it to ffmpeg, and that
rounding is load-bearing.

**ffmpeg's raw MJPEG demuxer silently corrupts the timeline for many
non-integer input frame rates.** After about 51 frames it stops advancing PTS
by a frame duration and starts incrementing by a single tick, so every
remaining frame piles up at almost the same timestamp. A 30 s clip then reports
a fraction of a second long and plays as a blur, with the frames all present
and `nb_read_packets` correct — which is exactly the failure the frame-count
guard in `postprocess.sh` cannot see.

Measured on ffmpeg 6.1.1 with a 900-frame stream:

| `-framerate` | Result |
|---|---|
| 120, 119.873, 60, 59.94, 30, 29.97, 15 | correct |
| 125.75, 100.25, 90.5, 31.623, 30.5 | **PTS collapses at frame 52** |

There is no warning and the exit status is 0. It is not the muxer — the broken
timestamps come out of the demuxer, and MP4, NUT and AVI are all affected. An
exact rational (`-framerate 401/4`) does not help, and neither does an output
`-r`. Whole numbers are always correct.

Rounding costs nothing: in the fractional cases that *did* work, ffmpeg had
quantised to a whole number anyway (119.873 produced a 120 fps timeline), so
the fraction was never buying accuracy. A 0.1 % speed error over a 30 s clip is
0.03 s and irrelevant to chop review. The measured rate is kept in the clip
metadata (`measured_fps=`) and in the journal line for anyone who needs it.

This also makes `PLAYBACK_MODE="slowmo"` exact: `setpts=4.0*PTS -r 30` against
a true 120 fps input is a clean 4×.

### The PLC edge detector is seeded from the current value

On connect the poll loop reads once and takes that as the baseline instead of
assuming FALSE. A latched bit that is already TRUE when we reconnect after a
network blip is not a new chop, and treating it as one wrote a phantom clip
every time the link flapped. The log says `... is already TRUE at connect` when
this happens.

### Clip filenames are UTC by default

`event_20260909_193012Z_chop1.mkv`. Five nodes recording the same chop have to
be lined up on the aggregator, and local time is both ambiguous for one hour
every autumn and not comparable across nodes whose clocks or DST state differ.
`CLIP_TIMESTAMP="local"` gives wall-clock names instead, with the UTC offset
kept so they stay unambiguous. **Run NTP on every node** — none of this helps
if the clocks disagree.

Every clip also carries `node=`, `trigger=`, `tag=`, frame count and real fps
in its container metadata, so identity survives a rename on the aggregator
(`ffprobe -show_entries format_tags`).

### Clips are written to a .part name and renamed

`capture.py` writes `.<name>.part.mkv` and renames on success. `postprocess.sh`
globs `*.mkv`, which bash does not expand to dotfiles, so a half-written clip
can never be picked up, and a crash mid-write leaves nothing that looks
complete. Stage 0 of `postprocess.sh` clears `.part` files older than an hour
(a mux takes ~0.1 s, so anything older is debris).

### The service refuses to start on a bad config

`validate_config()` runs before the camera is opened and checks the things that
fail silently otherwise: a missing or unsafe `NODE_NAME`, an unknown
`PLC_TYPE`, an unparseable Siemens address, no trigger source enabled at all.
`--check-config` runs the same checks and exits.

The motivating case is scale: nodes 2–5 get their config by copying node 1's
and editing four values. `NODE_NAME` therefore ships **blank** — a node running
under a copied name produces footage nobody can attribute to a chop point, and
there is no way to tell after the fact. Better to refuse to boot.

### /healthz exists because systemd cannot tell you this

If the camera drops off, the capture thread logs a warning and retries every
two seconds — forever — while `systemctl status` still shows the unit as
`active`, because the service *is* running fine, it just has nothing to record.
Same if the PLC config is bad: the poll thread stops and the unit stays green.

`/healthz` returns 200 only when the camera has produced a frame in the last
two seconds AND the PLC is connected (or the PLC trigger is switched off), and
503 otherwise. It reports buffer depth, frame age, achieved poll rate, trigger
count and clip count. The preview wall gets node status for free from the same
endpoint it already fetches video from.

### Library versions are pinned

`pycomm3`, `python-snap7` and `pymodbus` are all pinned in `install.sh`.
Verified against **pycomm3 1.2.16** and **python-snap7 3.1.2**. The PLC drivers
are the hardest part of this system to test off-site; a major version bump
discovered at a panel is the worst possible time to find out that `snap7.Area`
moved. (It did move once already: `snap7.types.Areas` in 1.x, `snap7.Area` in
3.x. `plc.py` tolerates both, but the pin is the real protection.)

### Two PLC families are supported

Different lines here use different PLCs, so `src/plc.py` puts both behind one
`TriggerSource` interface and `capture.py` polls it without caring which is on
the other end. `PLC_TYPE` in the config selects the driver. Adding a third
family means subclassing `TriggerSource` and one `register_source()` call —
nothing in the capture path changes, and the error message for an unknown
`PLC_TYPE` lists whatever is registered.

**Allen-Bradley ControlLogix / CompactLogix** — EtherNet/IP via `pycomm3`.
Watches a named tag. Use `--list-tags` to find the real name: Rockwell output
tags are frequently `...:O.Data.7` rather than `...:O.7`, or an alias.

Point at a single **bit**. If the tag resolves to a DINT the trigger silently
becomes "any nonzero value" and fires on unrelated data; the driver logs a
warning saying so once, rather than letting it look like a PLC fault.

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
  than letting a typo become a confusing read error at runtime. `DB100.DBX0.8`
  is really `DB100.DBX1.0`.
- **`DB100.DBX0.7`, `DB100.DBB0.7` and `DB100.0.7` are the same bit.** The
  short form was documented but did not actually parse until the unit tests
  caught it — the old pattern made only the `X`/`B` optional, not the whole
  `DB` token.

A malformed address or unknown `PLC_TYPE` is a configuration error, not a
transient one, so the poll loop logs it and **stops** instead of retrying
forever. Capture, preview and the Modbus bench trigger keep running.

---

## One install = one aggregator + N nodes

The system is deployed several times around the plant. Each install is one
aggregator (a Pi 5) and however many capture nodes that machine needs, on its
own subnet. Nothing is plant-wide except the code.

What varies per install, and where it lives:

| Varies | Setting |
|---|---|
| Subnet | `NODE_IP` per node, `PLC_PATH` per node |
| Number of nodes | just add configs; `NODES` on the aggregator |
| PLC family | `PLC_TYPE` per node — installs can even mix families |
| Number of PLCs | `PLC_PATH` per node. One shared PLC and one-PLC-per-station are the same code path |
| Trigger address | `TRIGGER_TAG` per node |
| Lighting | `CAMERA_CONTROLS` per install |
| Aggregator | `AGG_*` per node |

`SITE` is the install's name and is shared by every Pi at that install; only
the per-node values differ. That split is what makes a per-install config
template work: copy it to each Pi, change four values. Clips are labelled
`SITE-NODE_NAME`, so `chop1` at five installs stays unambiguous.

### The aggregator is deliberately dumb

It never decodes or encodes video. The browser pulls MJPEG straight from each
node, and the aggregator only polls `/healthz` and lists a directory. That is
why a Pi 5 can drive a wall of cameras while staying near idle — and why
adding a camera costs the aggregator almost nothing.

It polls node health server-side rather than from page JavaScript because the
nodes send no CORS headers, so the browser cannot read their `/healthz`
directly. MJPEG `<img>` tags are not CORS-restricted, so video still comes
straight from the nodes.

### AGG_OS has a silent failure mode

The aggregator is a Pi 5, so `AGG_OS="linux"`. Set it to `windows` against a
Linux aggregator and nothing errors: `scp` succeeds, the PowerShell verify
command produces nothing, the clip is never marked delivered, and the **same
clip re-ships every timer run forever** — duplicates accumulating on the
aggregator while the node's `encoded/` never drains.

`postprocess.sh` now distinguishes the two signatures. A verify that returns
*nothing* names `AGG_OS`; a verify that returns a *different* value is real
corruption and says so instead. Both were the same message before, which meant
a wrong `AGG_OS` looked like a bad network.

The ship path has been run end to end against a real Linux target — transcode,
scp, SHA-256 verify, move to `sent/` — including a destination path containing
a space, and a simulated truncated delivery, which was correctly refused.

### Camera controls are a per-install setting

The module ships with auto-exposure on (Aperture Priority) and a default
exposure of **15.6 ms**. At 120 fps a frame is **8.33 ms**, and exposure cannot
exceed the frame period — so in anything short of bright light the camera
quietly drops to ~64 fps, and the frames it does deliver carry 15 ms of blade
travel smeared across them. That defeats the point of the system while looking
like a working camera.

`CAMERA_CONTROLS` pins v4l2 controls before ffmpeg opens the device, and again
on every capture restart because UVC controls do not reliably survive the
device being reopened. Controls are applied one at a time and in order:
`exposure_time_absolute` is inactive until `auto_exposure` has been set to
manual, so the order matters.

Lighting differs at every install, which is why this is per-install rather
than a built-in default. Also worth setting: `power_line_frequency` (2 = 60 Hz
in North America; the camera defaults to 50 Hz), and `focus_automatic_continuous=0`
so autofocus cannot hunt mid-chop on a fixed mount.

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
- **The Modbus bench trigger binds loopback by default.** Anyone who can reach
  the port can start a recording, so `MODBUS_BIND_IP="0.0.0.0"` is opt-in for
  when you need to fire it from a laptop during commissioning.
- **The config parser handles a subset of bash.** `KEY="value"`, `KEY=value`,
  `export KEY=...` and trailing comments, all read identically by bash and by
  `capture.py` (there is a test that compares the two readers key by key over
  the shipped example). Variable expansion (`KEY="$OTHER/x"`), command
  substitution and multi-line values are NOT supported by the Python side —
  bash would read them and `capture.py` would not.
- **`FPS` is only a request to the camera.** It no longer sizes the ring
  buffer (see "The ring buffer is bounded by time, not frame count"), so
  getting it wrong cannot shorten the pre-roll any more. It must still name a
  mode the camera actually supports, or capture will not start.
- **A clip still in its post-roll is lost if the service restarts.** The ring
  buffer is RAM only, so a `systemctl restart` during the 15 s post-roll window
  drops that clip. This is why `install.sh` does not restart the service for
  you.
- **Clocks must be synchronised.** Clip names and metadata are UTC, which only
  helps correlate five nodes if all five agree on the time. Confirm `timedatectl`
  shows NTP active on every node.
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
   run against a live PLC. Start with `capture.py --check-config`, then
   `capture.py --test-trigger 60`, which connects, names the CPU it reached,
   and reports every transition plus a verdict on whether the pulses are wide
   enough to catch. Non-zero exit means marginal or absent.
   - *ControlLogix:* run `--list-tags 156N0` first; the real tag may be
     `...:O.Data.7` or an alias. If connect times out but ping works, the PLC
     is in a chassis and `PLC_PATH` needs the CPU slot (`"10.2.4.1/1"`).
     Watch for the `reads as DINT, not BOOL` warning — that means the tag is a
     word and the trigger is really testing "nonzero".
   - *Siemens:* check rack/slot, PUT/GET permission, and that the DB is not
     "optimized" — see the PLC section above.
2. **Confirm the trigger fires once per chop**, and that the chop lands near
   the middle of the clip. `--test-trigger` now measures this directly: it
   prints the width of every pulse and the poll rate it actually achieved, and
   judges the margin against the achieved rate rather than the configured one
   (a PLC that answers in 50 ms makes `POLL_HZ=30` a 20 Hz poll). If a fast
   chop is missed, the pulse was shorter than the real poll interval — raise
   `POLL_HZ` or have controls latch the bit.
3. **Test the transfer against the real aggregator.** The Linux path has been
   run end to end (transcode → scp → SHA-256 verify → `sent/`), including a
   truncated delivery, which was refused. What is untested is *this plant's*
   aggregator: key auth, the account, and that `AGG_DIR` matches
   `INCOMING_DIR`. Confirm `ssh -o BatchMode=yes <agg> true` returns without a
   prompt before enabling `SHIP_ENABLED`.
4. **Run the aggregator on real hardware.** Pi 5 + NVMe. The wall, node status
   and clip listing are built (`aggregator/`) and verified against simulated
   nodes in all three states, but not on a real Pi 5 with real cameras. Drop
   `LIVE_FPS` to 5–8 per node first. Still missing: clip retention and
   playback.
5. **Storage.** ~7 GB/day across five nodes at one chop/hour. A 1 TB SSD holds
   ~5 months. Use an SSD, not an SD card — SD cards wear out under continuous
   writes and fail in ways that lose data.
6. **Decide `PLAYBACK_MODE`.** `realtime` keeps true 120 fps (scrub to
   inspect); `slowmo` retimes to 30 fps so the clip *plays* at 4× slow motion in
   any player. If reviewers will just double-click the file, `slowmo` is
   probably what they want.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

No PLC, no camera, no PLC libraries. It covers Siemens address parsing, driver
construction for every `PLC_TYPE` alias, config parsing (including a check that
bash and `capture.py` read `chopcam.conf` identically), clip naming, JPEG frame
splitting across arbitrary chunk boundaries, the clip mux against real ffmpeg
output, and the `/healthz` gate. `install.sh` runs it on every install, which
is also how a CRLF-damaged checkout gets caught before you are at the panel.

The address-parsing tests exist because that is the only part of the trigger
path that can be validated without hardware — and they immediately found a
documented address form that never actually worked.

## If you only read one thing

Capture is the part that must not fail. Everything expensive — encoding,
transfer, purging — was deliberately pushed out of the capture path and onto a
timer running at idle priority. If you add work to the system, add it to
`postprocess.sh`, not `capture.py`.
