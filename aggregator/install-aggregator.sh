#!/usr/bin/env bash
#
# install-aggregator.sh -- set up the chopcam aggregator (a Pi 5).
# Run from the repo root's aggregator/ directory:  sudo ./install-aggregator.sh
#
# The aggregator does three jobs:
#   * receives clips from the capture nodes over ssh/scp
#   * serves the live wall on a monitor
#   * shows which nodes are actually recording
#
# Idempotent: safe to re-run after pulling changes.

set -euo pipefail

INSTALL_DIR=/opt/chopcam-agg
CONF=/etc/chopcam-agg.conf

SEED_CONF=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --conf) SEED_CONF="${2:-}"; shift 2
                [[ -n "$SEED_CONF" ]] || { echo "--conf needs a file" >&2; exit 1; } ;;
        -h|--help)
            cat <<'USAGE'
install-aggregator.sh -- set up the chopcam aggregator.

  sudo ./install-aggregator.sh                    then edit /etc/chopcam-agg.conf
  sudo ./install-aggregator.sh --conf agg.conf    install a prepared config

An existing /etc/chopcam-agg.conf is never overwritten unless --conf is given.
USAGE
            exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install-aggregator.sh" >&2
    exit 1
fi

RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
echo "==> Installing chopcam aggregator as user: $RUN_USER"

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

echo "==> Packages"
apt-get update -qq
# No PLC libraries: the aggregator never talks to a PLC. ffmpeg is here only to
# REMUX slow-motion downloads (-itsscale with -c copy), which is a container
# rewrite taking a fraction of a second -- it never decodes or encodes video.
# The browser renders the MJPEG tiles straight from the nodes.
apt-get install -y -qq python3 ffmpeg

echo "==> Files"
mkdir -p "$INSTALL_DIR"
install -m 0755 "$HERE/wall.py" "$INSTALL_DIR/wall.py"
install -m 0755 "$HERE/purge.py" "$INSTALL_DIR/purge.py"
# The shared config reader, so the aggregator parses chopcam.conf files exactly
# as the nodes do rather than growing a second parser that can drift.
install -m 0644 "$REPO/src/chopcam_config.py" "$INSTALL_DIR/chopcam_config.py"
[[ -f "$HERE/README.md" ]] && install -m 0644 "$HERE/README.md" "$INSTALL_DIR/README.md"
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

echo "==> Config"
if [[ -n "$SEED_CONF" ]]; then
    [[ -f "$SEED_CONF" ]] || { echo "    no such file: $SEED_CONF" >&2; exit 1; }
    if [[ -f "$CONF" ]] && ! cmp -s "$SEED_CONF" "$CONF"; then
        cp "$CONF" "$CONF.bak.$(date +%Y%m%d%H%M%S)"
        echo "    existing $CONF backed up"
    fi
    install -m 0644 "$SEED_CONF" "$CONF"
    echo "    installed $CONF from $SEED_CONF"
elif [[ -f "$CONF" ]]; then
    echo "    $CONF exists, leaving it alone"
else
    install -m 0644 "$HERE/chopcam-agg.conf.example" "$CONF"
    echo "    created $CONF -- EDIT IT (at minimum SITE and NODES)"
fi

echo "==> Clip storage"
INCOMING_DIR=""
# shellcheck source=/dev/null
source "$CONF"
INCOMING_DIR="${INCOMING_DIR:-/srv/chopcam/incoming}"
mkdir -p "$INCOMING_DIR"
chown -R "$RUN_USER":"$RUN_USER" "$INCOMING_DIR"
echo "    $INCOMING_DIR (owner $RUN_USER)"

echo "==> systemd"
for unit in chopcam-wall.service chopcam-purge.service; do
    sed -e "s/^User=.*/User=$RUN_USER/" \
        -e "s/^Group=.*/Group=$RUN_USER/" \
        -e "s#^ReadWritePaths=.*#ReadWritePaths=$INCOMING_DIR#" \
        "$HERE/systemd/$unit" > "/etc/systemd/system/$unit"
done
install -m 0644 "$HERE/systemd/chopcam-purge.timer" \
        /etc/systemd/system/chopcam-purge.timer
systemctl daemon-reload

echo "==> Config check"
CHOPCAM_AGG_CONF="$CONF" sudo -u "$RUN_USER" python3 "$INSTALL_DIR/wall.py" \
    --check-config 2>&1 | sed 's/^/    /' || true

cat <<MSG

Done.

Next:
  1. Edit $CONF   (SITE, NODES, INCOMING_DIR), then re-check:
       python3 $INSTALL_DIR/wall.py --check-config

  2. Let the capture nodes deliver clips here. On EACH node:
       ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519    # if it has no key
       cat ~/.ssh/id_ed25519.pub
     then append each node's public key to this machine's
       /home/$RUN_USER/.ssh/authorized_keys
     and set on each node (/etc/chopcam.conf):
       SHIP_ENABLED="true"
       AGG_OS="linux"
       AGG_USER="$RUN_USER"
       AGG_IP="<this aggregator's IP>"
       AGG_DIR="$INCOMING_DIR"
     Confirm from a node, with NO password prompt:
       ssh -o BatchMode=yes $RUN_USER@<this-ip> true && echo ok

  3. Start the wall and the retention timer:
       sudo systemctl enable --now chopcam-wall.service
       sudo systemctl enable --now chopcam-purge.timer
       journalctl -u chopcam-wall -f

     Retention is RETENTION_DAYS in $CONF (default 7). The timer re-reads that
     file every run, so changing the number takes effect on the next pass --
     no restart. See what it would do without deleting anything:
       python3 $INSTALL_DIR/purge.py --dry-run

  4. Open it:   http://<this-aggregator>:${WALL_PORT:-8090}/
     Status:    http://<this-aggregator>:${WALL_PORT:-8090}/status
     Clips:     http://<this-aggregator>:${WALL_PORT:-8090}/clips

  5. Full screen on the monitor (Pi OS with desktop):
       mkdir -p ~/.config/autostart
       cat > ~/.config/autostart/chopcam-wall.desktop <<'DESKTOP'
       [Desktop Entry]
       Type=Application
       Name=chopcam wall
       Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:${WALL_PORT:-8090}/
       DESKTOP
     Drop LIVE_FPS to 5-8 on each node so the display is not decoding several
     full-rate streams at once.
MSG
