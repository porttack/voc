#!/usr/bin/env bash
# install.sh — one-time setup for the SGP30 VOC sensor on Raspberry Pi
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_USER="$(whoami)"
VENV="$INSTALL_DIR/.venv"
VENV_PYTHON="$VENV/bin/python3"

###############################################################################
# 1. Enable I2C
###############################################################################
if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG=/boot/firmware/config.txt
elif [ -f /boot/config.txt ]; then
    BOOT_CONFIG=/boot/config.txt
else
    echo "ERROR: Cannot find boot config.txt" >&2; exit 1
fi

if grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG"; then
    echo "I2C already enabled in $BOOT_CONFIG"
else
    echo "Enabling I2C in $BOOT_CONFIG"
    echo "dtparam=i2c_arm=on" | sudo tee -a "$BOOT_CONFIG"
fi

if ! grep -q "^i2c-dev" /etc/modules 2>/dev/null; then
    echo "i2c-dev" | sudo tee -a /etc/modules
fi

# Add current user to i2c group so the service can access /dev/i2c-* without root
if ! groups "$INSTALL_USER" | grep -q i2c; then
    sudo usermod -aG i2c "$INSTALL_USER"
    echo "Added $INSTALL_USER to i2c group (takes effect after next login/reboot)"
fi

###############################################################################
# 2. System packages
###############################################################################
echo ""
echo "Installing system packages…"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-dev i2c-tools

# Ensure NTP time sync is active so CSV timestamps are trustworthy
sudo systemctl enable systemd-timesyncd --quiet
sudo systemctl start  systemd-timesyncd
echo "Time sync status: $(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"

###############################################################################
# 3. Create venv and install Python packages into it
###############################################################################
echo ""
if [ -d "$VENV" ]; then
    echo "Virtual environment already exists at $VENV — updating packages…"
else
    echo "Creating virtual environment at $VENV…"
    python3 -m venv "$VENV"
fi

echo "Installing Python packages into venv…"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

###############################################################################
# 4. Generate self-signed TLS certificate (skip if already present)
###############################################################################
CERT_DIR="/etc/voc"
SSL_CERT="$CERT_DIR/cert.pem"
SSL_KEY="$CERT_DIR/key.pem"
USER_CFG_DIR="$HOME/.config/voc"
USER_CFG="$USER_CFG_DIR/config.py"

if [ ! -f "$SSL_CERT" ] || [ ! -f "$SSL_KEY" ]; then
    echo ""
    echo "Generating self-signed TLS certificate (valid 10 years)…"
    sudo mkdir -p "$CERT_DIR"
    sudo openssl req -x509 -newkey rsa:2048 \
        -keyout "$SSL_KEY" \
        -out    "$SSL_CERT" \
        -days 3650 -nodes \
        -subj "/CN=$(hostname -s)" 2>/dev/null
    # Private key readable only by root and the service user
    sudo chown root:"$INSTALL_USER" "$SSL_KEY"
    sudo chmod 640 "$SSL_KEY"
    sudo chmod 644 "$SSL_CERT"
    echo "Certificate: $SSL_CERT"
else
    echo "TLS certificate already exists at $SSL_CERT — skipping"
fi

# Write cert paths into the user config override (only once)
mkdir -p "$USER_CFG_DIR"
if ! grep -q "SSL_CERT" "$USER_CFG" 2>/dev/null; then
    printf '\n# TLS — written by install.sh\nSSL_CERT = "%s"\nSSL_KEY  = "%s"\n' \
        "$SSL_CERT" "$SSL_KEY" >> "$USER_CFG"
    echo "SSL paths written to $USER_CFG"
fi

###############################################################################
# 5. Point script shebangs at the venv Python and make them executable
###############################################################################
for script in read_voc.py save_baseline.py load_baseline.py monitor_voc.py voc_web.py; do
    sed -i "1s|.*|#!${VENV_PYTHON}|" "$INSTALL_DIR/$script"
    chmod +x "$INSTALL_DIR/$script"
done
# Tell git to ignore the shebang rewrites so 'git status' stays clean
git -C "$INSTALL_DIR" update-index --assume-unchanged \
    read_voc.py save_baseline.py load_baseline.py monitor_voc.py voc_web.py 2>/dev/null || true

###############################################################################
# 6. Install and enable systemd service (uses venv Python)
###############################################################################
SERVICE=/etc/systemd/system/voc.service

echo ""
echo "Installing systemd service…"
sudo tee "$SERVICE" > /dev/null <<EOF
[Unit]
Description=SGP30 VOC Web Monitor
After=network.target

[Service]
Type=simple
User=${INSTALL_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV_PYTHON} ${INSTALL_DIR}/voc_web.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
# Allow binding port 443 without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable voc
sudo systemctl start voc

###############################################################################
# Done
###############################################################################
echo ""
echo "============================================================"
echo " Installation complete."
echo ""
echo " Service status:"
sudo systemctl status voc --no-pager -l || true
echo ""
echo " Dashboard HTTP:  http://$(hostname -I | awk '{print $1}'):8080"
echo " Dashboard HTTPS: https://$(hostname -I | awk '{print $1}')  (self-signed cert)"
echo ""
echo " Note: browsers will warn about the self-signed certificate — click"
echo " 'Advanced' and proceed.  The connection is still encrypted."
echo ""
echo " To run scripts manually (no activation needed):"
echo "   $VENV_PYTHON read_voc.py"
echo ""
echo " Service commands:"
echo "   sudo systemctl status voc      # check service"
echo "   sudo systemctl restart voc     # restart"
echo "   sudo journalctl -u voc -f      # live logs"
echo ""
echo " If I2C was just enabled, or you were just added to the"
echo " i2c group, reboot for changes to take effect:"
echo "   sudo reboot"
echo "============================================================"
