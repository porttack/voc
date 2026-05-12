#!/usr/bin/env bash
# install.sh — one-time setup for the SGP30 VOC sensor on Raspberry Pi
set -euo pipefail

###############################################################################
# 1. Enable I2C in /boot/config.txt (or /boot/firmware/config.txt on Bookworm)
###############################################################################
if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG=/boot/firmware/config.txt
elif [ -f /boot/config.txt ]; then
    BOOT_CONFIG=/boot/config.txt
else
    echo "ERROR: Cannot find /boot/config.txt or /boot/firmware/config.txt" >&2
    exit 1
fi

if grep -q "^dtparam=i2c_arm=on" "$BOOT_CONFIG"; then
    echo "I2C already enabled in $BOOT_CONFIG"
else
    echo "Enabling I2C in $BOOT_CONFIG"
    echo "dtparam=i2c_arm=on" | sudo tee -a "$BOOT_CONFIG"
fi

# Make sure the i2c-dev module loads at boot
if ! grep -q "^i2c-dev" /etc/modules 2>/dev/null; then
    echo "i2c-dev" | sudo tee -a /etc/modules
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
pip3 install --break-system-packages -r requirements.txt 2>/dev/null \
    || pip3 install -r requirements.txt

###############################################################################
# 4. Make scripts executable
###############################################################################
chmod +x read_voc.py save_baseline.py load_baseline.py monitor_voc.py

###############################################################################
# Done
###############################################################################
echo ""
echo "============================================================"
echo " Installation complete."
echo ""
echo " If I2C was just enabled, REBOOT now:"
echo "   sudo reboot"
echo ""
echo " After reboot, verify the sensor is detected (should show '58'):"
echo "   i2cdetect -y 1"
echo ""
echo " Quick start:"
echo "   python3 read_voc.py            # single reading"
echo "   python3 monitor_voc.py         # continuous monitoring"
echo "   python3 save_baseline.py       # save baseline (after 12 h)"
echo "   python3 load_baseline.py       # inspect / reload baseline"
echo "============================================================"
