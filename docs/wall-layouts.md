# Wall layouts

The aggregator arranges tiles from the number of cameras at that install, so
they fill the monitor rather than leaving a ragged row:

    columns = ceil(sqrt(n))      rows = ceil(n / columns)

| Cameras | Layout | | Cameras | Layout |
|---|---|---|---|---|
| 1 | 1 | | 6 | 3 × 2 |
| 2 | 2 | | 7 | 3 + 3 + 1 |
| 3 | 2 + 1 | | 8 | 3 + 3 + 2 |
| 4 | 2 × 2 | | 9 | 3 × 3 |
| 5 | 3 + 2 | | 16 | 4 × 4 |

A last row with fewer tiles than columns is **centred**, not left-aligned —
otherwise there is a hole in the corner that reads as a missing camera. Tiles
span two of a doubled column track, which makes that offset exact: centring two
tiles across three columns is half a tile, which whole tracks cannot express.

Narrow windows fall back to however many columns actually fit, so the wall stays
readable from a laptop instead of producing slivers, and it recomputes on
resize.

Adding or removing a camera is one entry in `NODES` and a restart. Nothing else
changes.

> The shots below use simulated nodes and test patterns in place of real
> cameras. All are 1920×1080.

---

## 1 camera

A single tile takes the whole screen. Healthy, with its most recent chop ready
to play.

![One camera](images/wall-01-cameras.jpg)

## 2 cameras

Side by side, full height. A 16:9 feed letterboxes top and bottom here, because
the tile shape comes from dividing the screen, not from the feed.

![Two cameras](images/wall-02-cameras.jpg)

## 3 cameras

Two over one, the bottom tile centred. This one shows three different states at
once: a delivered chop, one still transcoding on its node, and a node that is
not answering at all — covered, because an unreachable node otherwise keeps
showing its last frozen frame and reads as working.

![Three cameras](images/wall-03-cameras.jpg)

## 4 cameras

An even 2 × 2.

![Four cameras](images/wall-04-cameras.jpg)

## 5 cameras

Three over two, the bottom row centred. Every state the wall can show:

| Tile | Badge | Button | Meaning |
|---|---|---|---|
| UW1 | HEALTHY | `Last chop · 1m` | working normally |
| UW2 | HEALTHY | `Chop processing · 4m` | fired, still transcoding on the node |
| UW3 | HEALTHY | `Chop delayed · 48m` | **fired long ago and never arrived** |
| UW4 | DEGRADED | `Last chop · 1m` | node faulted, past clips still play |
| UW5 | UNREACHABLE | `No clips yet` | node offline |

![Five cameras](images/wall-05-cameras.jpg)

UW3 is the one worth understanding. The node reports itself healthy — camera
streaming, PLC connected — yet its footage is not arriving. That is a wedged
transcode, a full disk or broken key auth, and nothing else in the system would
notice it. The header counts it too: `1 delayed`.

UW4 is the opposite: the node is sick, but clips it delivered earlier still
play fine.

## 16 cameras

A clean 4 × 4, needing no centring. All sixteen MJPEG streams run at once —
browsers cap connections at six *per host*, and each node is its own host, so
that limit never applies.

![Sixteen cameras](images/wall-16-cameras.jpg)

At this scale the constraint is decode, not connections: sixteen MJPEG streams
is a lot of JPEG for a Pi 5 to unpack. That is what `LIVE_FPS` on each node is
for — 5–8 fps per node keeps the wall comfortable. It is not something five
cameras will run into.

## A note on tile shape

Tiles are sized by dividing the screen, so their aspect rarely matches the
camera's. The video uses `object-fit: contain`, so it is letterboxed rather than
stretched or cropped — deliberately, since cropping could hide the blade.

Two cameras is the worst case (tall tiles, wide feed) and six is the best fit on
a 16:9 monitor. Five lands close.
