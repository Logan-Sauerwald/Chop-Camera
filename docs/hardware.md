# Hardware notes

## Bill of materials (per capture node)

| Item | Notes |
|---|---|
| Raspberry Pi 4 Model B, 4 GB | 4 GB is enough: ring buffer is ~400 MB |
| SVPRO `SV-USB12MP01-BL25` | 12 MP, Sony IMX577, USB **2.0**, 25 mm lens |
| USB SSD (recommended) | SD cards wear out under continuous clip writes |
| Cat6 to the switch | |
| PSU, case | |

Aggregator: Raspberry Pi 5 (8 GB) + NVMe HAT + 1–2 TB NVMe, or an Intel N100/N150
mini PC (has Quick Sync, which would make transcoding near-free if the design
ever moves back to central encoding).

## Camera

### Supported modes (from `v4l2-ctl --list-formats-ext`)

MJPEG — the full list, not just the ones in use:

| Resolution | Max fps | Notes |
|---|---|---|
| 3840×3040 | 20 | full sensor, widest |
| 3840×2880 | 20 | |
| 3840×2160 | 30 | |
| 2592×1944 | 30 | full sensor, wider FOV |
| 2048×1536 | 30 | |
| 1600×1200 | 30 | |
| **1920×1080** | **120** | in use — cropped readout, narrower FOV |
| **1280×960** | **120** | 4:3 at full rate — see below |
| 1280×720 | 120 | |
| 1024×768 | 120 | |
| 960×720 | 120 | |
| 800×600 | 120 | |
| 640×480 | 120 | |
| 320×240 | 120 | |

YUYV modes exist but are uncompressed and cap at ~5 fps at 1080p. Ignore them.

Every mode at 120 fps is a reduced readout, which is why the view is tight. The
current config chooses 1920×1080 because slow-motion review of the chop is the
point of the system.

**`1280×960` is worth an experiment and has never been tried.** It is 4:3 at
the same 120 fps. If it is a binned or scaled readout rather than a crop, it
would give a noticeably *wider* view at full rate — which is the field-of-view
problem, solved without a lens change. It is also 59% of the pixels, so roughly
half the bitrate: smaller clips, less USB load, a ring buffer nearer 250 MB
than 436.

Testing it costs nothing: point the camera at a fixed scene and compare the
framing of `1920x1080`, `1280x960` and `2592x1944`. If 1280×960 frames wider
than 1080p, it is binned and you have gained field of view.

### Camera controls, and why they decide whether 120 fps is real

The module ships with **auto-exposure on** (Aperture Priority) and a default
exposure of **15.6 ms**. At 120 fps a frame is **8.33 ms**, and exposure cannot
exceed the frame period — so in anything short of good light the camera quietly
drops to ~64 fps, and the frames it does deliver carry 15 ms of blade travel
smeared across them. Both failures look like a working camera.

Measured on the bench: with reasonable light, auto-exposure settled well inside
the budget and the node captured a true **120.00 fps, 3600 frames, zero lost**.
That is a property of the lighting, not of the camera — the same node in a dim
machine guard will not hold it.

`CAMERA_CONTROLS` in the node config pins the controls before ffmpeg opens the
device, and again on every capture restart, because UVC controls do not reliably
survive the device being reopened. They are applied one at a time and **in
order**: `exposure_time_absolute` is inactive until `auto_exposure` is manual.

A starting point for a lit chop point on 60 Hz mains:

```
CAMERA_CONTROLS="auto_exposure=1,exposure_time_absolute=40,focus_automatic_continuous=0,white_balance_automatic=0,backlight_compensation=0,power_line_frequency=2"
```

| Control | Value | Why |
|---|---|---|
| `auto_exposure` | `1` | manual (`3` is auto). Must come first |
| `exposure_time_absolute` | `40` | units are 100 µs, so 4 ms. Must be under `83` to hold 120 fps |
| `focus_automatic_continuous` | `0` | autofocus can hunt mid-chop on a fixed mount |
| `white_balance_automatic` | `0` | keeps colour consistent across nodes and over time |
| `backlight_compensation` | `0` | it works by *lengthening* exposure |
| `power_line_frequency` | `2` | 60 Hz. The camera defaults to 50, which bands under North American lighting |

Shorter exposure is sharper but darker — **add light rather than lengthening
it.** This is a per-install setting because it depends on the light at that
camera.

Two related notes:

- **60 Hz mains gives LED lighting a 120 Hz flicker**, which beats directly
  against a 120 fps capture. If clips show pulsing brightness, that is the
  lighting, not the camera — it wants DC-driven or high-frequency fixtures.
- `power_line_frequency=0` disables anti-flicker entirely. Worth trying if the
  filter appears to be constraining exposure.

List what a given camera supports with:

```bash
v4l2-ctl -d /dev/video0 --list-ctrls
```

### Lens

The `BL25` suffix means a **25 mm** M12 lens: roughly 12° horizontal field of
view. That is telephoto on this sensor, which is why the view looks tight.

It unscrews. Approximate horizontal FOV on this sensor:

| Focal length | ~HFOV |
|---|---|
| 25 mm (fitted) | 12° |
| 8 mm | 40° |
| 6 mm | 55° |
| 4 mm | 75° |
| 2.8 mm | 95° |

If replacing: rate it for **1/2.3" or larger** (smaller and the corners
vignette) and **≥12 MP** (cheap low-res lenses look soft on this sensor).
Refocus by screwing the lens in or out, then lock the setscrew. If colours go
pink afterwards, the IR-cut filter is missing from the light path.

### Colour

VLC and similar players render this camera green/purple because they pick up the
raw YUV stream. Forcing MJPEG — which the whole pipeline does — gives correct
colour. This is a player/format issue, not a camera fault.

### Useful commands

```bash
v4l2-ctl --list-devices                       # which /dev/videoN
v4l2-ctl -d /dev/video0 --list-formats-ext    # supported modes
v4l2-ctl -d /dev/video0 --list-ctrls          # brightness, zoom, focus...
sudo fuser -v /dev/video0                     # what is holding the camera
lsusb -t                                      # bus topology and link speed
```

`zoom_absolute` exists but is already at its minimum (widest). There is no
digital zoom-out available — the lens is the constraint.

## USB topology

```
Bus 001 (480M) ── internal 4-port hub ── all four external ports
Bus 002 (5000M, USB 3.0) ── unused: the cameras are USB 2.0 devices
```

Every external port shares the single 480 Mbps bus, so plugging a camera into a
different port does not gain bandwidth. USB 3.0 versions of this module would
enumerate on Bus 002 and largely remove the constraint, if the design ever needs
more than one camera per Pi.

## Network

**Addresses are per machine, not plant-wide.** Every camera+aggregator setup
around the plant sits on its own controls network — one line may be `192.0.2.x`
while another is `198.51.100.x` — and on some machines each chop point has its
own PLC. `docs/deployments.md` shows the shape of the record to keep; the table
below is one line's layout as an example.

Addresses here are RFC 5737 documentation addresses, not defaults and not any
real machine's. Substitute your own.

| Device | Address |
|---|---|
| ControlLogix PLC | 192.0.2.10 |
| Node 1 | 192.0.2.100 |
| Nodes 2–5 | 192.0.2.101–104 (planned) |
| Aggregator | 192.0.2.200 (planned; matches `AGG_IP` in that node's config) |

Static IPs via NetworkManager, substituting this node's address:

```bash
sudo nmcli con mod "Wired connection 1" ipv4.method manual \
     ipv4.addresses <this-node-ip>/24 ipv4.never-default yes
sudo nmcli con up "Wired connection 1"
```

`ipv4.never-default yes` stops the PLC interface from taking over the default
route, so Wi-Fi keeps carrying internet and SSH.

**Bench note:** a Pi and a laptop connected by a single Ethernet cable need
static IPs on both ends (no DHCP on a direct link). The Pi's `eth0` has one
port — it can be on the PLC network or on a direct laptop cable, not both. Use
a small unmanaged switch if you need the laptop and the PLC at the same time.
