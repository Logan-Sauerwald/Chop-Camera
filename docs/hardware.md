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

MJPEG:

| Resolution | Max fps | Notes |
|---|---|---|
| 1920×1080 | **120** | cropped readout — narrower FOV, but the only 120 fps option |
| 1280×720 | 120 | |
| 2592×1944 | 30 | full sensor, wider FOV |
| 3840×3040 | 20 | full sensor, widest |

YUYV modes exist but are uncompressed and cap at ~5 fps at 1080p. Ignore them.

**The high frame rates only exist on the cropped modes.** Wide field of view and
120 fps are mutually exclusive on this camera. The current config chooses
120 fps because slow-motion review of the chop is the point of the system.

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

| Device | Address |
|---|---|
| ControlLogix PLC | 10.2.4.1 |
| Node 1 | 10.2.4.100 |
| Nodes 2–5 | 10.2.4.101–104 (planned) |
| Aggregator | 10.2.4.200 (planned) |

Static IPs via NetworkManager, for example:

```bash
sudo nmcli con mod "Wired connection 1" ipv4.method manual \
     ipv4.addresses 10.2.4.100/24 ipv4.never-default yes
sudo nmcli con up "Wired connection 1"
```

`ipv4.never-default yes` stops the PLC interface from taking over the default
route, so Wi-Fi keeps carrying internet and SSH.

**Bench note:** a Pi and a laptop connected by a single Ethernet cable need
static IPs on both ends (no DHCP on a direct link). The Pi's `eth0` has one
port — it can be on the PLC network or on a direct laptop cable, not both. Use
a small unmanaged switch if you need the laptop and the PLC at the same time.
