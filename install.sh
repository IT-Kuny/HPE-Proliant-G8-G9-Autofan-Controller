#!/bin/bash
# Install fan controller on Herkules
set -e

echo "=== Fan Controller Install ==="

# Dependencies
apt-get install -y lm-sensors sshpass python3-yaml 2>/dev/null || true

# Detect sensors
sensors-detect --auto 2>/dev/null || true

# Deploy files
mkdir -p /opt/fan-controller /etc/fan-controller
cp fan-controller.py /opt/fan-controller/
chmod +x /opt/fan-controller/fan-controller.py
cp config.yaml /etc/fan-controller/
cp fan-controller.service /etc/systemd/system/

# Reminder for credentials
if ! grep -q "ILO_PASSWORD" /etc/fan-controller/env 2>/dev/null; then
    echo "# Set iLO password here" > /etc/fan-controller/env
    echo 'ILO_PASSWORD=""' >> /etc/fan-controller/env
    chmod 600 /etc/fan-controller/env
    echo "⚠ Set ILO_PASSWORD in /etc/fan-controller/env"
fi

systemctl daemon-reload
echo "✅ Installed. Start with: systemctl enable --now fan-controller"
