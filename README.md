<h1 align="center">HPE ProLiant G8/G9 Autofan Controller</h1>

<p align="center">
  Standalone thermal fan controller for HP ProLiant Gen8/Gen9 servers with modded iLO4 firmware.
</p>

<p align="center">
  <strong>Companion to:</strong> <a href="https://github.com/IT-Kuny/HPE-G8-G9-Fan-Controller">iLO Fan Controller Web UI</a>
</p>

---

## Features

- **Per-sensor fan curves** — each sensor has its own temperature-to-fan mapping
- **Hybrid sensor reading** — `lm-sensors` for CPU temps, `ipmitool sdr` for BMC sensors
- **Fan control via SSH** to iLO4 (`fan p <n> lock <speed>`)
- **Outdoor temperature** integration via [Open-Meteo API](https://open-meteo.com/)
- **Adaptive cooling detection** — correlates inlet vs outdoor temp to detect AC / open window
- **Proactive boost** when ambient OR critical sensors are elevated
- **Escalation mode** — if temps keep rising for 3 min despite fan speed, progressively override curve limits
- **Auto de-escalation** when temps stabilize
- **Cooling discount** when active cooling (AC) is detected
- **Failsafe mode** on sensor read failure (defaults to 80%)

## Fan Curve Targets

| Season | Condition | Fan Speed |
|--------|-----------|-----------|
| Winter (night) | Low load, cold ambient | ~28% |
| Spring / Fall | Normal operation | ~34% |
| Summer (day) | High ambient + load | max 50% |
| Summer (evening + AC) | Cooling active | ~42% |
| Escalation | Temps rising >3 min | up to 100% |
| Critical | Any sensor at critical temp | 100% |

## Sensors

| Sensor | Source | Priority | Normal Range | Critical |
|--------|--------|----------|-------------|----------|
| HD Controller | ipmitool | 🔴 Primary | 50–75°C | 100°C |
| LOM Card | ipmitool | 🔴 Primary | 45–70°C | 90°C |
| CPU 1 & 2 | lm-sensors | 🟡 Secondary | 40–65°C | 90°C |
| Chipset | ipmitool | 🟡 Secondary | 40–55°C | 95°C |
| Inlet Ambient | ipmitool | ⚪ Reference | 20–30°C | 45°C |

## Requirements

- Python 3.10+
- `lm-sensors`, `sshpass`, `ipmitool`, `python3-yaml`
- HP ProLiant Gen8/Gen9 with modded iLO4 firmware (SSH fan control)
- OpenSSH 10 compatible (Debian 13 / Trixie tested)

## Install

```bash
git clone https://github.com/IT-Kuny/HPE-Proliant-G8-G9-Autofan-Controller.git
cd HPE-Proliant-G8-G9-Autofan-Controller

# Install (as root)
bash install.sh

# Set iLO credentials
echo 'ILO_PASSWORD=your_password_here' > /etc/fan-controller/env
chmod 600 /etc/fan-controller/env

# Test with dry-run first
python3 /opt/fan-controller/fan-controller.py -c /etc/fan-controller/config.yaml --dry-run --once

# Go live
systemctl enable --now fan-controller
```

> **Security:** `/etc/fan-controller/` is restricted to root (700/600). iLO credentials never leave the server.

## Usage

```bash
# One-shot dry run (test sensors + fan calculation)
python3 fan-controller.py -c config.yaml --dry-run --once

# Continuous dry run (monitor without changing fans)
python3 fan-controller.py -c config.yaml --dry-run

# Live operation
python3 fan-controller.py -c config.yaml

# Check service status
systemctl status fan-controller
journalctl -u fan-controller -f
```

## Configuration

Edit `/etc/fan-controller/config.yaml`:

- **Fan curves** — per-sensor `[temperature, fan_percent]` mappings
- **Outdoor integration** — latitude/longitude for Open-Meteo, boost thresholds
- **Adaptive cooling** — correlation-based AC detection with fan discount
- **Escalation** — window, step size, and max override for rising temps
- **Failsafe** — fallback fan speed on sensor read failures

## How It Works

```
Every 15 seconds:
  1. Read all sensor temps (IPMI + lm-sensors)
  2. Fetch outdoor temp (cached, every 10 min)
  3. Evaluate per-sensor fan curves → highest wins
  4. Apply outdoor boost if ambient + sensors elevated
  5. Apply escalation if temps rising >3 min
  6. Apply AC discount if active cooling detected
  7. Set fans via SSH to iLO4
```

## License

MIT
