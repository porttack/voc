#!/usr/bin/env bash
# install.sh — one-time setup for the SGP30 VOC sensor on Raspberry Pi
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_USER="$(whoami)"
PYTHON="$(which python3)"

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
sudo apt-get install -y python3-pip python3-dev i2c-tools

###############################################################################
# 3. Python packages
###############################################################################
echo ""
echo "Installing Python packages…"
pip3 install --break-system-packages -r "$INSTALL_DIR/requirements.txt" 2>/dev/null \
    || pip3 install -r "$INSTALL_DIR/requirements.txt"

###############################################################################
# 4. Make scripts executable
###############################################################################
chmod +x "$INSTALL_DIR"/{read_voc.py,save_baseline.py,load_baseline.py,monitor_voc.py,voc_web.py}

###############################################################################
# 5. Install and enable systemd service
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
ExecStart=${PYTHON} ${INSTALL_DIR}/voc_web.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

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
echo " Dashboard:  http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo " Useful commands:"
echo "   sudo systemctl status voc      # check service"
echo "   sudo systemctl restart voc     # restart"
echo "   sudo journalctl -u voc -f      # live logs"
echo ""
echo " If I2C was just enabled, or you were just added to the"
echo " i2c group, reboot for changes to take effect:"
echo "   sudo reboot"
echo "============================================================"
