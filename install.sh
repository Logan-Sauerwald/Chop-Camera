#!/usr/bin/env bash
#
# install.sh -- set up a chopcam capture node.
# Run from the repo root on a fresh Raspberry Pi:  sudo ./install.sh
#
# Idempotent: safe to re-run after pulling changes. It does NOT restart a
# running capture service -- that would drop the ring buffer mid-shift -- so
# it tells you when a restart is pending instead.

set -euo pipefail

INSTALL_DIR=/opt/chopcam
STATE_DIR=/var/lib/chopcam
CONF=/etc/chopcam.conf

# PLC libraries are PINNED. Both drivers are the part of this system that is
# hardest to test off-site, so an unannounced major version bump between now
# and a commissioning trip is exactly the failure you cannot afford. Verified
# against pycomm3 1.2.16 and python-snap7 3.1.2.
PIN_PYCOMM3="pycomm3>=1.2.14,<2"
PIN_SNAP7="python-snap7>=3.1,<4"
# pymodbus 3.9+ rewrote the datastore and the callback-datablock pattern the
# Modbus bench trigger relies on no longer works there.
PIN_PYMODBUS="pymodbus>=3.8,<3.9"

SEED_CONF=""
APPLY_NETWORK=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --conf)
            SEED_CONF="${2:-}"; shift 2
            [[ -n "$SEED_CONF" ]] || { echo "--conf needs a file" >&2; exit 1; }
            ;;
        --apply-network) APPLY_NETWORK=true; shift ;;
        -h|--help)
            cat <<'USAGE'
install.sh -- set up a chopcam capture node.

  sudo ./install.sh                       interactive: installs, then you edit
                                          /etc/chopcam.conf
  sudo ./install.sh --conf node.conf      install with a prepared config
                                          (the repeatable path for a rollout)
  sudo ./install.sh --conf node.conf --apply-network
                                          also set this node's static IP from
                                          NODE_IP/NODE_CIDR/NODE_INTERFACE

An existing /etc/chopcam.conf is never overwritten unless --conf is given.
USAGE
            exit 0
            ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh" >&2
    exit 1
fi

# The account that will own and run the service -- the user who invoked sudo,
# not root, so the service doesn't run privileged.
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
echo "==> Installing chopcam as user: $RUN_USER"

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

WAS_ACTIVE=false
systemctl is-active --quiet chopcam.service && WAS_ACTIVE=true

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
sudo -u "$RUN_USER" "$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
# pycomm3      -> Allen-Bradley ControlLogix (EtherNet/IP)
# python-snap7 -> Siemens S7 (pure Python since 3.x; no libsnap7 needed)
sudo -u "$RUN_USER" "$INSTALL_DIR/venv/bin/pip" install -q \
    "$PIN_PYCOMM3" "$PIN_SNAP7" "$PIN_PYMODBUS"
echo "    installed:"
sudo -u "$RUN_USER" "$INSTALL_DIR/venv/bin/pip" list 2>/dev/null \
    | grep -iE 'pycomm3|snap7|pymodbus' | sed 's/^/      /'

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
    cp "$REPO/chopcam.conf.example" "$CONF"
    echo "    created $CONF -- EDIT IT before starting the service"
fi

# --- this node's static IP -------------------------------------------------
# Bringing networking into the same file is what makes commissioning a node a
# single-file job. Without it the procedure is "edit the conf AND remember the
# nmcli incantation", which is where per-node mistakes come from.
if [[ "$APPLY_NETWORK" == true ]]; then
    echo "==> Network"
    NODE_IP="" NODE_CIDR="" NODE_INTERFACE=""
    # shellcheck source=/dev/null
    source "$CONF"
    if [[ -z "${NODE_IP:-}" ]]; then
        echo "    NODE_IP is blank in $CONF -- leaving networking alone"
    elif ! command -v nmcli >/dev/null; then
        echo "    nmcli not found -- set the address by hand" >&2
    else
        conn="${NODE_INTERFACE:-}"
        if [[ -z "$conn" ]]; then
            # First active ethernet connection; the usual Pi OS name is
            # "Wired connection 1" but it is not guaranteed.
            conn="$(nmcli -t -f NAME,TYPE con show --active \
                    | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
            [[ -z "$conn" ]] && conn="$(nmcli -t -f NAME,TYPE con show \
                    | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
        fi
        if [[ -z "$conn" ]]; then
            echo "    no ethernet connection found -- set NODE_INTERFACE" >&2
        else
            echo "    $conn -> ${NODE_IP}/${NODE_CIDR:-24}"
            # never-default keeps Wi-Fi carrying the default route, so SSH and
            # internet survive putting this port on an isolated controls VLAN.
            nmcli con mod "$conn" ipv4.method manual \
                  ipv4.addresses "${NODE_IP}/${NODE_CIDR:-24}" \
                  ipv4.never-default yes
            nmcli con up "$conn" >/dev/null || \
                echo "    (bring-up reported an error; check 'ip -brief a')" >&2
        fi
    fi
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

# Address parsing and config handling are pure logic and need no PLC, so a
# broken checkout (CRLF line endings, a bad edit) shows up here rather than at
# the panel. Non-fatal: a test failure must not block getting the camera up.
if [[ -d "$REPO/tests" ]]; then
    echo "==> Self-test"
    if selftest="$(sudo -u "$RUN_USER" "$INSTALL_DIR/venv/bin/python" \
         -m unittest discover -s "$REPO/tests" -q 2>&1)"; then
        printf '%s\n' "$selftest" | grep -E '^(OK|Ran )' | sed 's/^/    /'
    else
        echo "    WARNING: self-test FAILED -- the checkout may be damaged"
        printf '%s\n' "$selftest" | tail -20 | sed 's/^/      /'
    fi
fi

echo "==> Config check"
CHOPCAM_CONF="$CONF" sudo -u "$RUN_USER" "$INSTALL_DIR/venv/bin/python" \
    "$INSTALL_DIR/src/capture.py" --check-config 2>&1 | sed 's/^/    /' || true

cat <<MSG

Done.

Next:
  1. Edit $CONF   (at minimum: SITE, NODE_NAME, PLC_TYPE, PLC_PATH, TRIGGER_TAG)
     then re-check it:
       $INSTALL_DIR/venv/bin/python $INSTALL_DIR/src/capture.py --check-config
  2. Confirm the PLC trigger (connects, then characterises the bit):
       $INSTALL_DIR/venv/bin/python $INSTALL_DIR/src/capture.py --test-trigger 60
     ControlLogix: find the exact tag name first with
       $INSTALL_DIR/venv/bin/python $INSTALL_DIR/src/capture.py --list-tags
  3. Start it:
       sudo systemctl enable --now chopcam.service
       sudo systemctl enable --now chopcam-postprocess.timer
  4. Watch:
       journalctl -u chopcam -f
  5. Live preview:  http://<this-pi>:8080/
     Node health:   http://<this-pi>:8080/healthz

If $RUN_USER was just added to the "video" group, log out and back in
(or reboot) before the camera will open.
MSG

if [[ "$WAS_ACTIVE" == true ]]; then
    cat <<'MSG'

NOTE: chopcam.service is running the code from BEFORE this install. It was not
restarted automatically, because that drops the ring buffer and any chop during
the restart window is not recorded. Restart it when the line is safe to do so:

    sudo systemctl restart chopcam.service
MSG
fi
