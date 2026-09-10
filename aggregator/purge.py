#!/usr/bin/env python3
"""
purge.py -- chopcam aggregator clip retention.

Deletes delivered clips older than RETENTION_DAYS, and purges oldest-first
beyond that if the filesystem is filling up.

Run from a systemd timer. The config is re-read on EVERY run, so changing
RETENTION_DAYS in /etc/chopcam-agg.conf takes effect on the next pass -- no
restart, no service to touch.

Age comes from the timestamp in the clip's own filename, not its mtime: mtime
is when the file was delivered here, which can be much later than when the
chop happened (the node transcodes first, and a node that was offline delivers
a backlog all at once). Falling back to mtime only when the name cannot be
parsed keeps a stray file from living forever.

The keep/ subdirectory is NEVER touched, by age or by disk pressure. Retention
that deletes everything on a fixed schedule will eventually delete the one clip
somebody needed, working exactly as designed; keep/ is the escape hatch, and
this file is the only place that promise can be kept. Two things enforce it:
this scan is not recursive, and the directory is skipped by name as well.

USAGE
  purge.py               delete what is due
  purge.py --dry-run     report what would be deleted, delete nothing
  purge.py --config PATH use a specific config file
"""

import os
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chopcam_config import load_config            # noqa: E402
from wall import parse_clip_name                  # noqa: E402

CONFIG_SEARCH = [
    os.environ.get("CHOPCAM_AGG_CONF"),
    "/etc/chopcam-agg.conf",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "chopcam-agg.conf"),
]

# Deleting files in a loop deserves a guard: a mistyped INCOMING_DIR should
# stop the run, not empty something important.
_FORBIDDEN = {"/", "/home", "/root", "/etc", "/usr", "/var", "/boot", "/srv",
              "/opt", "/bin", "/sbin", "/lib", "/tmp", "/dev", "/proc", "/sys"}

# Clips the wall's "Keep this clip" button has moved here are exempt. The name
# is shared with wall.py, which does the moving.
KEEP_DIRNAME = "keep"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def clip_age_days(entry, now):
    """Age in days, from the filename's timestamp where possible."""
    _, when = parse_clip_name(entry.name)
    if when is not None:
        return (now - when).total_seconds() / 86400.0
    try:
        return (time.time() - entry.stat().st_mtime) / 86400.0
    except OSError:
        return 0.0


def disk_pct(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0
    if usage.total == 0:
        return 0
    return round(100.0 * usage.used / usage.total)


def collect(incoming):
    """(entry, age_days) for every DELETABLE clip, oldest first.

    Deliberately not recursive, so nothing inside keep/ can ever appear in the
    list this function's callers delete from.
    """
    now = datetime.now(timezone.utc)
    out = []
    try:
        with os.scandir(incoming) as it:
            for entry in it:
                # Belt and braces with the is_file() test below: if this scan
                # is ever made recursive, this line is what still saves the
                # kept clips.
                if entry.name == KEEP_DIRNAME:
                    continue
                # is_file(follow_symlinks=False): never delete through a
                # symlink that points outside the clip directory.
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.endswith(".mp4"):
                    continue
                out.append((entry, clip_age_days(entry, now)))
    except OSError as exc:
        log(f"cannot read {incoming}: {exc}")
        return []
    out.sort(key=lambda pair: pair[1], reverse=True)      # oldest first
    return out


def kept_stats(incoming):
    """(count, bytes) of clips in keep/ -- reported, never deleted."""
    count = total = 0
    try:
        with os.scandir(os.path.join(incoming, KEEP_DIRNAME)) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.endswith(".mp4"):
                    continue
                count += 1
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass                    # no keep directory yet, which is the normal case
    return count, total


def warn_if_kept_fill_the_disk(pct, limit, kept_n, kept_bytes):
    """Say so when there is nothing left to free.

    The one way "keep this clip" can hurt: keep enough and the purge has
    nothing to delete when the disk fills. It is otherwise silent, and nothing
    else in the system would ever notice.
    """
    if pct < limit:
        return
    log(f"WARNING: disk {pct}% full and no deletable clips left")
    if kept_n:
        log(f"WARNING: {kept_n} kept clip(s) hold "
            f"{kept_bytes / 1048576:.0f} MB and are never deleted. "
            f"Release some from the wall, or move them off this disk.")


def main():
    dry = "--dry-run" in sys.argv
    path = None
    if "--config" in sys.argv:
        idx = sys.argv.index("--config") + 1
        if idx < len(sys.argv):
            path = sys.argv[idx]
    cfg = load_config(path, search=CONFIG_SEARCH)

    incoming = cfg.get("INCOMING_DIR", "/srv/chopcam/incoming")
    try:
        days = max(1, int(cfg.get("RETENTION_DAYS", 7)))
    except (TypeError, ValueError):
        days = 7
    try:
        limit = max(50, min(99, int(cfg.get("DISK_PCT_LIMIT", 85))))
    except (TypeError, ValueError):
        limit = 85

    real = os.path.realpath(incoming)
    if real in _FORBIDDEN or real == os.path.expanduser("~"):
        log(f"refusing to purge {real!r} -- INCOMING_DIR looks wrong")
        return 2
    if not os.path.isdir(real):
        log(f"{real} does not exist; nothing to purge")
        return 0

    kept_n, kept_bytes = kept_stats(real)
    if kept_n:
        log(f"{kept_n} clip(s) kept ({kept_bytes / 1048576:.0f} MB) -- "
            f"exempt from retention and from disk pressure")

    clips = collect(real)
    if not clips:
        log(f"no deletable clips in {real}")
        # Not just an early return: "nothing to delete" plus a full disk is
        # precisely the state kept clips can produce, and it is the only
        # moment anything in the system could report it.
        warn_if_kept_fill_the_disk(disk_pct(real), limit, kept_n, kept_bytes)
        return 0

    removed = freed = 0

    # 1. past retention
    for entry, age in list(clips):
        if age <= days:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        if dry:
            log(f"would delete {entry.name} ({age:.1f}d)")
        else:
            try:
                os.remove(entry.path)
            except OSError as exc:
                log(f"could not delete {entry.name}: {exc}")
                continue
        removed += 1
        freed += size
        clips.remove((entry, age))

    if removed:
        log(f"{'would purge' if dry else 'purged'} {removed} clip(s) past "
            f"{days}d retention, {freed / 1048576:.0f} MB")

    # 2. disk pressure -- oldest first, regardless of age
    pct = disk_pct(real)
    emergency = 0
    while pct >= limit and clips:
        entry, age = clips.pop(0)
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        if dry:
            log(f"would emergency-delete {entry.name} ({age:.1f}d, disk {pct}%)")
            emergency += 1
            break                    # a dry run cannot change the percentage
        try:
            os.remove(entry.path)
        except OSError as exc:
            log(f"could not delete {entry.name}: {exc}")
            continue
        emergency += 1
        freed += size
        pct = disk_pct(real)

    if emergency:
        log(f"disk at or above {limit}%: {'would remove' if dry else 'removed'} "
            f"{emergency} more clip(s), oldest first")
    if not clips:
        warn_if_kept_fill_the_disk(pct, limit, kept_n, kept_bytes)

    if not removed and not emergency:
        log(f"nothing due ({len(clips)} clip(s) within {days}d retention, "
            f"{kept_n} kept, disk {pct}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
