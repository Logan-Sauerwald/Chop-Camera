#!/usr/bin/env bash
#
# install.sh -- set up a chopcam capture node.
# Run from the repo root on a fresh Raspberry Pi:  sudo ./install.sh
#
# Idempotent: safe to re-run after pulling changes.

set -euo pipefail

INSTALL_DIR=/opt/chopcam
STATE_DIR=/var/lib/chopcam
CONF=/etc/chopcam.conf

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh" >&2
    exit 1
fi

# The account that will own and run the service -- the user who invoked sudo,
# not root, so the service doesn't run privileged.
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
echo "==> Installing chopcam as user: $RUN_USER"

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq ffmpeg python3-venv v4l-utils

echo "==> Directories"
mkdir -p "$INSTALL_DIR"
mkdir -p "$STATE_DIR"/{raw,encoded,sent}
chown -R "$RUN_USER":"$RUN_USER" "$STATE_DIR"

echo "==> Files"
cp -r "$REPO/src" "$INSTALL_DIR/"
cp "$REPO/README.md" "$REPO/HANDOFF.md" "$INSTALL_DIR/" 2>/dev/null || true
chmod +x "$INSTALL_DIR/src/postprocess.sh"
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

echo "==> Python venv"
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    sudo -u "$RUN_USER" python3 -m venv "$INSTALL_DIR/venv"
fi
# pymodbus is pinned: 3.9+ rewrote the datastore and the callback-datablock
# pattern the Modbus bench trigger relies on no longer works there.
sudo -u "$RUN_USER" "$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
# pycomm3      -> Allen-Bradley ControlLogix (EtherNet/IP)
# python-snap7 -> Siemens S7 (pure Python since 3.x; no libsnap7 needed)
sudo -u "$RUN_USER" "$INSTALL_DIR/venv/bin/pip" install -q \
    pycomm3 python-snap7 "pymodbus>=3.8,<3.9"

echo "==> Config"
if [[ -f "$CONF" ]]; then
    echo "    $CONF exists, leaving it alone"
else
    cp "$REPO/chopcam.conf.example" "$CONF"
    echo "    created $CONF -- EDIT IT before starting the service"
fi

echo "==> Camera access"
usermod -aG video "$RUN_USER"

echo "==> systemd units"
for unit in chopcam.service chopcam-postprocess.service chopcam-postprocess.timer; do
    sed -e "s/^User=.*/User=$RUN_USER/" \
        -e "s/^Group=.*/Group=$RUN_USER/" \
        "$REPO/systemd/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload

cat <<MSG

Done.

Next:
  1. Edit $CONF   (at minimum: NODE_NAME, PLC_PATH, TRIGGER_TAG)
  2. Confirm the PLC tag name:
       $INSTALL_DIR/venv/bin/python $INSTALL_DIR/src/capture.py --test-trigger
  3. Start it:
       sudo systemctl enable --now chopcam.service
       sudo systemctl enable --now chopcam-postprocess.timer
  4. Watch:
       journalctl -u chopcam -f
  5. Live preview:  http://<this-pi>:8080/

If $RUN_USER was just added to the "video" group, log out and back in
(or reboot) before the camera will open.
MSG
