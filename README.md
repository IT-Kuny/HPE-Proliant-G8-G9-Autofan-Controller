# Herkules Fan Controller

Standalone thermal fan controller for HP DL360p Gen8 with modded iLO4 firmware.

## Features
- Per-sensor fan curves (each sensor has its own temperature-to-fan mapping)
- Hybrid sensor reading: `lm-sensors` for CPU temps, `ipmitool sdr` for BMC sensors
- Fan control via SSH to iLO4 (`fan p <n> lock <speed>`)
- Outdoor temperature integration (Open-Meteo API, Basel)
- Adaptive cooling detection (correlates inlet vs outdoor temp to detect AC/open window)
- Proactive boost when ambient OR critical sensors elevated
- Cooling discount when active cooling detected
- Failsafe mode on sensor read failure

## Sensors
| Sensor | Source | Normal Range | Critical |
|--------|--------|-------------|----------|
| Inlet Ambient | ipmitool | 20-30°C | 45°C |
| CPU 1 & 2 | lm-sensors | 40-60°C | 90°C |
| HD Controller | ipmitool | 55-75°C | 100°C |
| LOM Card | ipmitool | 50-70°C | 90°C |
| Chipset | ipmitool | 40-55°C | 95°C |

## Install
```bash
bash install.sh
# Set iLO password:
echo 'ILO_PASSWORD=xxx' > /etc/fan-controller/env
# Remove --dry-run from service file when ready:
systemctl edit fan-controller  # or edit /etc/systemd/system/fan-controller.service
systemctl restart fan-controller
```

## Usage
```bash
# One-shot dry run
python3 fan-controller.py -c config.yaml --dry-run --once

# Continuous dry run
python3 fan-controller.py -c config.yaml --dry-run

# Live (actually sets fans)
python3 fan-controller.py -c config.yaml
```

## Config
Edit `/etc/fan-controller/config.yaml` — per-sensor fan curves, boost thresholds, outdoor location.

## License
MIT
