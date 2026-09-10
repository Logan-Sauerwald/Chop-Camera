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

Clips are always written at true speed with every frame. Slow motion is a
viewing choice made on the way out — see "Playback, and why 0.25x is the
default".

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

### Shipping verifies by content, and says which way it failed

The aggregator is a Pi 5, so the remote helpers are plain `sha256sum` /
`stat -c%s` / `mkdir -p`. There is no Windows variant any more.

The failure that matters is quiet: `scp` succeeds, the verify command produces
nothing, the clip is never marked delivered, and the SAME clip re-ships every
timer run — duplicates piling up on the aggregator while the node's `encoded/`
never drains. `postprocess.sh` therefore distinguishes two signatures. A verify
returning *nothing* is a broken remote command and says so; a verify returning a
*different* value is real corruption and says that instead. They used to be one
message, which made a broken command look like a bad network.

The path has been run end to end against a real Linux target — transcode, scp,
SHA-256 verify, move to `sent/` — including a destination path containing a
space, and a simulated truncated delivery (scp exits 0, far end short), which
was correctly refused with the local copy kept.

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

## Playback, and why 0.25x is the default

The aggregator's player opens at quarter speed, not real speed, and that is a
measurement rather than a preference.

**On a 60 Hz monitor a 120 fps clip played at 1x can only show 60 of every 120
frames.** Half of what the camera captured is discarded at the display stage,
no matter how fast the machine is. At 0.25x the clip presents 30 frames a
second, comfortably under the refresh rate, so every captured frame is actually
displayed. Slow motion is not a convenience here; it is the only way to see
what the 120 fps capture bought.

Verified in a browser: `playbackRate = 0.25` advanced 0.75 s of video in 3.00 s
of wall clock, a ratio of exactly 0.25, with no frame duplication.

### The Pi 5 has no hardware H.264 decoder either

It kept HEVC hardware decode but the H.264 block was removed, so H.264 is
software-decoded on the CPU. That is survivable *because* of the above: at
0.25x the browser decodes about 30 frames a second, not 120. Real-time 1x
playback is the marginal case and may stutter; it is also the least useful
mode. A laptop plugged into the switch has hardware decode and handles 1x and
heavy seeking better, which is the right place for careful review.

### Slow-motion downloads are a remux, not an encode

`-itsscale 4` with `-c copy` rescales the input timestamps: a 120 fps clip is
restamped to 30 fps with every frame intact. Measured: 600 frames in, 600
frames out, 5.00 s becomes 19.98 s, **0.08 s to produce**, same file size.

This matters because the Pi 5 has no H.264 *encoder* either -- re-encoding on
demand would be hopeless, but a container rewrite is free. It also replaced `PLAYBACK_MODE="slowmo"`, which used to bake the decision into
the archive at encode time. Clips are now always stored at true speed, and slow
motion is chosen on the way out — reversible, and the archive stays honest.

### Range requests are what make the player work

`BaseHTTPRequestHandler` has no `Range:` support, so `wall.py` implements it.
Without it a browser re-fetches from byte zero on every seek, which makes the
seek bar useless. Adding it was *less* work than avoiding it: with ranges the
native `<video controls>` element does play, pause and seek for free, where
without them the seek bar has to be hidden behind hand-built controls.

Stock `python3 -m http.server` answers a Range request with `200` and the whole
file; `wall.py` answers `206` with the byte range, and `416` for an
unsatisfiable one.

### Opening the player stops the live streams

Each tile is an MJPEG `<img>` holding an open connection and decoding
continuously. Leaving several of those running while the Pi 5 software-decodes
a 120 fps clip is what makes playback stutter, so the player blanks their `src`
on open and restores it on exit. Hiding the elements is not enough -- a hidden
`<img>` keeps streaming.

### "Chop processing" comes from data the system already had

Each node's `/healthz` reports when its trigger last fired; the aggregator knows
what has landed on disk. A trigger newer than anything delivered means a chop is
in the pipeline, and the node's counts of `raw/` and `encoded/` say which stage.

The same comparison is the only thing in the system that detects a **stuck
pipeline**. Past `STUCK_MINUTES` the tile reads "Chop delayed" and the node
counts as degraded -- a wedged transcode, a full disk or broken key auth would
otherwise show up as footage that quietly never appears, discovered weeks later
by someone looking for it.

Caveat: the trigger time lives in the node's memory, so a node restart clears
it and there is no badge until the next chop. The failure mode is safe -- it
shows nothing rather than something wrong.

### Deleting footage is guarded

`purge.py` is the only code that deletes clips, so it refuses to run against a
path like `/`, `/home` or `/etc`, never follows a symlink out of the clip
directory, and takes a clip's age from the timestamp in its **filename** rather
than its mtime -- a node delivering a backlog after being offline would
otherwise look like a pile of brand-new footage and reset the clock on all of
it.

`DISK_PCT_LIMIT` values below 50 are clamped up: a typo there would empty the
archive on a healthy disk.

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

Everything below needs the plant. The node pipeline itself — capture, ring
buffer, trigger, mux, transcode — has been run end to end on real hardware:
3600 frames captured at a measured 120.00 fps, 3600 packets out the far side,
duration exactly 30.000000, zero frames lost.

1. **Verify the PLC trigger against real hardware.** Neither driver has been run
   against a live PLC. `capture.py --check-config`, then
   `capture.py --test-trigger 60` (or `0` to watch until Ctrl-C, if you cannot
   force a splice). It connects, names the CPU it reached, reports every
   transition, and gives a verdict on whether the pulses are wide enough to
   catch. Non-zero exit means marginal or absent.
   - *Siemens:* rack/slot first — connection refused is almost always the wrong
     slot. Then PUT/GET permission. Merkers (`M158.7`) do **not** need the
     "optimized block access" change; that is data blocks only.
   - *ControlLogix:* `--list-tags` first; the real tag may be `...:O.Data.7` or
     an alias. Watch for `reads as DINT, not BOOL` — that means the trigger is
     testing "nonzero" rather than one bit.

2. **Confirm the trigger fires once per chop**, and that the chop lands near the
   middle of the clip. `--test-trigger` measures pulse width and the poll rate
   actually achieved, judging the margin against the achieved rate rather than
   the configured one. If the bit is a single PLC scan (~10 ms), no poll rate
   saves you — controls need to latch it for ~250 ms.

   Also worth establishing: does the bit go true when the knife *fires*, or when
   the splice *sequence begins*? If the sequence starts seconds before the cut,
   the chop lands late in the clip and `PRE_SECONDS`/`POST_SECONDS` want
   shifting.

3. **Tune the camera at the knife.** `measured` in the `saved` line is the
   number: a bench reading of 120.00 is a property of bench lighting, not of the
   camera. If it drops at the machine, auto-exposure is stretching the shutter
   past the 8.33 ms a 120 fps frame allows — pin `CAMERA_CONTROLS` and add
   light.

4. **Test shipping against this plant's aggregator.** The Linux path works end
   to end here; what is untested is key auth, the account, and that `AGG_DIR`
   matches `INCOMING_DIR`. `ssh -o BatchMode=yes <agg> true` must return without
   a prompt before enabling `SHIP_ENABLED`.

5. **Run the aggregator on real hardware.** Pi 5 + NVMe. The wall, playback,
   status and retention are built and verified against simulated nodes in every
   state, but not on a real Pi 5 with real cameras. Drop `LIVE_FPS` to 5–8 per
   node first.

   The one thing that could not be checked here: **H.264 decode in the
   browser.** Playwright's Chromium ships without proprietary codecs, so
   playback was proven with VP9 through the same code path. Pi OS's
   `chromium-browser` includes H.264 — open a clip on the real machine and
   confirm it plays.

6. **Measure the real chop rate**, which is what sizes storage. "One chop an
   hour" was always an assumption. At ~60 MB a clip and 7-day retention, four
   nodes is ~40 GB at one an hour and ~240 GB at one every ten minutes. The
   disk-pressure guard means a wrong guess cannot fill the disk, but it decides
   what drive to buy.

7. **Browsing older clips.** The wall plays the *latest* chop per camera;
   `/clips` lists everything but there is no UI for picking an older one.

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
