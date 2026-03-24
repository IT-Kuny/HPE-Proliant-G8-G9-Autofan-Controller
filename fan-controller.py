#!/usr/bin/env python3
"""Standalone thermal fan controller for HP DL360p Gen8 with modded iLO4."""

import argparse
import json
import logging
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
from base64 import b64encode
from pathlib import Path

import yaml

from collections import deque

LOG = logging.getLogger("fan-controller")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # env override for password
    if os.environ.get("ILO_PASSWORD"):
        cfg["ilo"]["password"] = os.environ["ILO_PASSWORD"]
    return cfg

# ---------------------------------------------------------------------------
# Outdoor temperature (Open-Meteo)
# ---------------------------------------------------------------------------

class OutdoorTemp:
    """Cached outdoor temperature from Open-Meteo API."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("outdoor", {})
        self.enabled = self.cfg.get("enabled", False)
        self.last_fetch = 0
        self.temp = None
        self.poll_interval = self.cfg.get("poll_interval", 600)

    def get(self) -> float | None:
        if not self.enabled:
            return None
        now = time.time()
        if self.temp is not None and (now - self.last_fetch) < self.poll_interval:
            return self.temp
        lat = self.cfg.get("latitude", 47.56)
        lon = self.cfg.get("longitude", 7.59)
        url = (f"https://api.open-meteo.com/v1/forecast?"
               f"latitude={lat}&longitude={lon}&current_weather=true")
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            self.temp = float(data["current_weather"]["temperature"])
            self.last_fetch = now
            LOG.info("Outdoor temp: %.1f°C", self.temp)
        except Exception as e:
            LOG.warning("Outdoor temp fetch failed: %s", e)
        return self.temp


# ---------------------------------------------------------------------------
# Adaptive cooling detection
# ---------------------------------------------------------------------------

class CoolingDetector:
    """Detect active cooling (AC/open window) by correlating inlet vs outdoor."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("adaptive", {})
        self.enabled = self.cfg.get("enabled", False)
        window = self.cfg.get("history_window", 3600)
        # Store (timestamp, inlet_temp, outdoor_temp) tuples
        self.history: deque = deque(maxlen=max(window // 15, 60))
        self.cooling_mode = "unknown"  # unknown, passive, active

    def record(self, inlet_temp: float | None, outdoor_temp: float | None):
        if not self.enabled or inlet_temp is None or outdoor_temp is None:
            return
        self.history.append((time.time(), inlet_temp, outdoor_temp))
        self._update()

    def _update(self):
        if len(self.history) < 10:
            return
        inlets = [h[1] for h in self.history]
        outdoors = [h[2] for h in self.history]

        corr = self._pearson(inlets, outdoors)
        threshold = self.cfg.get("correlation_threshold", 0.7)

        old_mode = self.cooling_mode
        if corr is None:
            self.cooling_mode = "unknown"
        elif corr >= threshold:
            self.cooling_mode = "passive"
        else:
            self.cooling_mode = "active"

        if self.cooling_mode != old_mode:
            LOG.info("Cooling mode changed: %s → %s (correlation: %.2f)",
                     old_mode, self.cooling_mode, corr or 0)

    @staticmethod
    def _pearson(x: list[float], y: list[float]) -> float | None:
        n = len(x)
        if n < 2:
            return None
        mx, my = sum(x) / n, sum(y) / n
        sx = sum((xi - mx) ** 2 for xi in x) ** 0.5
        sy = sum((yi - my) ** 2 for yi in y) ** 0.5
        if sx == 0 or sy == 0:
            return None
        cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        return cov / (sx * sy)


def compute_boost(cfg: dict, temps: dict[str, float], outdoor_temp: float | None) -> float:
    """Calculate proactive fan boost based on ambient temp OR critical sensors.
    
    Uses outdoor temp if available, falls back to inlet temp as ambient proxy.
    Boost activates when EITHER condition is true:
    1) Ambient (outdoor or inlet) exceeds threshold
    2) At least one critical sensor is elevated
    """
    outdoor_cfg = cfg.get("outdoor", {})
    if not outdoor_cfg.get("enabled"):
        return 0.0

    boost_pct = outdoor_cfg.get("boost_percent", 10)
    reasons = []

    # Check ambient (outdoor or inlet fallback)
    ambient = outdoor_temp
    ambient_source = "outdoor"
    if ambient is None:
        ambient = temps.get("inlet")
        ambient_source = "inlet"

    threshold = outdoor_cfg.get("boost_outdoor_temp", 28)
    if ambient is not None and ambient >= threshold:
        reasons.append(f"{ambient_source} {ambient:.0f}°C >= {threshold}°C")

    # Check critical sensors
    sensor_thresholds = outdoor_cfg.get("boost_sensor_thresholds", {})
    for sensor_id, temp_threshold in sensor_thresholds.items():
        if sensor_id in temps and temps[sensor_id] >= temp_threshold:
            reasons.append(f"{sensor_id} {temps[sensor_id]:.0f}°C >= {temp_threshold}°C")

    if reasons:
        LOG.info("Boost +%d%%: %s", boost_pct, " | ".join(reasons))
        return boost_pct
    return 0.0


# ---------------------------------------------------------------------------
# Temperature reading
# ---------------------------------------------------------------------------

def read_ipmi_temps() -> dict[str, float]:
    """Read temperatures from ipmitool sdr."""
    try:
        result = subprocess.run(
            ["ipmitool", "sdr", "type", "Temperature"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            LOG.error("ipmitool failed: %s", result.stderr.strip())
            return {}
    except Exception as e:
        LOG.error("ipmitool read failed: %s", e)
        return {}

    temps = {}
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 5 and "degrees C" in parts[4]:
            name = parts[0]
            try:
                temp = float(parts[4].replace("degrees C", "").strip())
                temps[name] = temp
            except ValueError:
                continue
    return temps


def read_lmsensors_temps() -> dict[str, float]:
    """Read temperatures from lm-sensors."""
    try:
        result = subprocess.run(
            ["sensors", "-j"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            LOG.error("sensors failed: %s", result.stderr.strip())
            return {}
        data = json.loads(result.stdout)
    except Exception as e:
        LOG.error("lm-sensors read failed: %s", e)
        return {}

    temps = {}
    for chip, chip_data in data.items():
        if not isinstance(chip_data, dict):
            continue
        max_temp = None
        for key, val in chip_data.items():
            if not isinstance(val, dict):
                continue
            for subkey, subval in val.items():
                if "input" in subkey and isinstance(subval, (int, float)) and subval > 0:
                    if max_temp is None or subval > max_temp:
                        max_temp = subval
        if max_temp is not None:
            temps[chip] = max_temp
    return temps


def read_all_temps(cfg: dict) -> dict[str, float]:
    """Read all configured sensor temperatures."""
    ipmi_temps = None
    sensor_temps = None
    results = {}

    for sensor_id, sensor_cfg in cfg.get("sensors", {}).items():
        source = sensor_cfg["source"]
        name = sensor_cfg["name"]

        if source == "ipmi":
            if ipmi_temps is None:
                ipmi_temps = read_ipmi_temps()
            temp = ipmi_temps.get(name)
        elif source == "sensors":
            if sensor_temps is None:
                sensor_temps = read_lmsensors_temps()
            temp = None
            for chip, val in sensor_temps.items():
                if chip.startswith(name) or name in chip:
                    temp = val
                    break
        else:
            continue

        if temp is not None:
            results[sensor_id] = temp
        else:
            LOG.warning("Sensor '%s' (%s) not found", sensor_id, name)

    return results

# ---------------------------------------------------------------------------
# Fan curve interpolation
# ---------------------------------------------------------------------------

def interpolate_fan(curve: list[list[float]], temp: float, min_pct: float) -> float:
    """Linear interpolation on fan curve, respecting minimum."""
    if temp <= curve[0][0]:
        return max(curve[0][1], min_pct)
    if temp >= curve[-1][0]:
        return curve[-1][1]

    for i in range(len(curve) - 1):
        t0, f0 = curve[i]
        t1, f1 = curve[i + 1]
        if t0 <= temp <= t1:
            ratio = (temp - t0) / (t1 - t0)
            pct = f0 + ratio * (f1 - f0)
            return max(pct, min_pct)

    return max(curve[-1][1], min_pct)


# ---------------------------------------------------------------------------
# Escalation tracker — if temps keep rising despite fan speed, break limits
# ---------------------------------------------------------------------------

class EscalationTracker:
    """Track per-sensor temp trends. If temps rise continuously for
    escalation_window seconds, override curve limits progressively."""

    def __init__(self, cfg: dict):
        esc_cfg = cfg.get("escalation", {})
        self.enabled = esc_cfg.get("enabled", True)
        self.window = esc_cfg.get("window_seconds", 180)  # 3 minutes
        self.step = esc_cfg.get("step_percent", 10)
        self.max_override = esc_cfg.get("max_percent", 100)
        # Per-sensor: deque of (timestamp, temp)
        self.history: dict[str, deque] = {}
        self.escalation_pct: float = 0.0

    def record(self, temps: dict[str, float]):
        if not self.enabled:
            return
        now = time.time()
        for sensor_id, temp in temps.items():
            if sensor_id not in self.history:
                self.history[sensor_id] = deque(maxlen=200)
            self.history[sensor_id].append((now, temp))

        self._evaluate(now)

    def _evaluate(self, now: float):
        """Check if ANY sensor has been continuously rising over the window."""
        cutoff = now - self.window
        rising_sensors = []

        for sensor_id, hist in self.history.items():
            # Get readings within window
            window_readings = [(t, temp) for t, temp in hist if t >= cutoff]
            if len(window_readings) < 6:  # Need at least 6 readings (~90s)
                continue

            # Check if trend is consistently upward
            temps_in_window = [temp for _, temp in window_readings]
            first_half = temps_in_window[:len(temps_in_window)//2]
            second_half = temps_in_window[len(temps_in_window)//2:]
            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)

            # Rising if second half is >2°C hotter than first half
            if avg_second - avg_first >= 2.0:
                rising_sensors.append(
                    f"{sensor_id} ({avg_first:.0f}→{avg_second:.0f}°C)")

        old_pct = self.escalation_pct
        if rising_sensors:
            self.escalation_pct = min(
                self.escalation_pct + self.step,
                self.max_override
            )
            if self.escalation_pct != old_pct:
                LOG.warning("ESCALATION +%d%%: temps still rising after %ds — %s",
                            int(self.escalation_pct), self.window,
                            ", ".join(rising_sensors))
        else:
            # Temps stabilized or dropping — de-escalate
            if self.escalation_pct > 0:
                self.escalation_pct = max(self.escalation_pct - self.step, 0)
                if self.escalation_pct != old_pct:
                    LOG.info("De-escalation: temps stabilized → override now %d%%",
                             int(self.escalation_pct))


def compute_target_fan(cfg: dict, temps: dict[str, float],
                       boost: float = 0.0,
                       cooling_mode: str = "unknown",
                       escalation_pct: float = 0.0) -> float:
    """Compute target fan percentage from sensor readings using per-sensor curves."""
    min_pct = cfg["min_fan_percent"]
    sensors_cfg = cfg.get("sensors", {})
    target = min_pct

    for sensor_id, temp in temps.items():
        scfg = sensors_cfg.get(sensor_id, {})
        critical = scfg.get("critical_temp", 100)
        curve = scfg.get("fan_curve")

        if not curve:
            LOG.warning("No fan_curve for sensor '%s', skipping", sensor_id)
            continue

        # Critical override
        if temp >= critical:
            LOG.warning("CRITICAL: %s at %.1f°C (limit %d°C) → 100%%",
                        sensor_id, temp, critical)
            return 100.0

        fan_pct = interpolate_fan(curve, temp, min_pct)
        LOG.debug("  %s: %.1f°C → %.0f%%", sensor_id, temp, fan_pct)
        if fan_pct > target:
            target = fan_pct

    # Apply proactive boost
    if boost > 0:
        target = min(target + boost, 100.0)
        LOG.info("Boost +%.0f%% applied → %.0f%%", boost, target)

    # Apply escalation override (temps still rising despite fan speed)
    if escalation_pct > 0:
        target = min(target + escalation_pct, 100.0)
        LOG.warning("Escalation +%.0f%% → %.0f%%", escalation_pct, target)

    # Apply cooling discount (active cooling detected = can run quieter)
    if cooling_mode == "active":
        discount = cfg.get("adaptive", {}).get("discount_percent", 5)
        min_pct = cfg["min_fan_percent"]
        old = target
        target = max(target - discount, min_pct)
        LOG.info("Active cooling: -%.0f%% (%.0f%% → %.0f%%)", discount, old, target)

    return target

# ---------------------------------------------------------------------------
# Fan control via SSH to iLO
# ---------------------------------------------------------------------------


class IloSshSession:
    """Persistent SSH connection to iLO4.

    Keeps a single SSH session open and sends fan commands via stdin.
    Reconnects automatically if the session dies. This avoids opening
    a new SSH connection every cycle, which can crash iLO's embedded
    SSH daemon over time (memory leak / session table exhaustion).
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._proc = None

    def _build_ssh_cmd(self) -> list[str]:
        ilo = self._cfg["ilo"]
        return [
            "sshpass", "-p", ilo["password"],
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"KexAlgorithms={ilo['ssh_kex']}",
            "-o", "HostKeyAlgorithms=ssh-rsa",
            "-o", "Ciphers=aes256-ctr",
            "-o", "PubkeyAcceptedAlgorithms=ssh-rsa",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-tt",
            f"{ilo['username']}@{ilo['host']}",
        ]

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def connect(self) -> bool:
        """Open persistent SSH session to iLO."""
        if self._is_alive():
            return True
        self.close()
        try:
            self._proc = subprocess.Popen(
                self._build_ssh_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(3)
            if not self._is_alive():
                stderr = ""
                if self._proc.stderr:
                    try:
                        stderr = self._proc.stderr.read1(4096).decode(errors="replace")
                    except Exception:
                        pass
                LOG.error("SSH session failed to start: %s", stderr.strip())
                self._proc = None
                return False
            LOG.info("Persistent SSH session to iLO established")
            return True
        except Exception as e:
            LOG.error("Failed to open SSH session: %s", e)
            self._proc = None
            return False

    def send_commands(self, commands: list[str]) -> bool:
        """Send commands to the persistent SSH session."""
        if not self._is_alive():
            if not self.connect():
                return False
        try:
            data = "\n".join(commands) + "\n"
            self._proc.stdin.write(data.encode())
            self._proc.stdin.flush()
            time.sleep(0.5)
            if not self._is_alive():
                LOG.warning("SSH session died after sending commands, reconnecting next cycle")
                self._proc = None
                return False
            return True
        except (BrokenPipeError, OSError) as e:
            LOG.warning("SSH pipe broken (%s), reconnecting next cycle", e)
            self.close()
            return False

    def close(self):
        """Close the SSH session."""
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    try:
                        self._proc.stdin.write(b"exit\n")
                        self._proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=3)
            except Exception:
                pass
            finally:
                self._proc = None
            LOG.info("SSH session closed")


def set_fans_ssh(cfg: dict, fan_percent: float, fan_count: int = 8, dry_run: bool = False,
                ssh_session: IloSshSession = None) -> bool:
    """Set fan speeds via SSH to iLO4.

    iLO SSH is an interactive CLI, not a shell. Commands must be sent
    via stdin, one per line, followed by 'exit'.
    """
    ilo = cfg["ilo"]
    speed_raw = int(round((fan_percent / 100.0) * 255))
    speed_raw = max(0, min(255, speed_raw))

    commands = ["fan p global unlock"]
    for i in range(fan_count):
        commands.append(f"fan p {i} lock {speed_raw}")

    if dry_run:
        LOG.info("[DRY-RUN] Would SSH to %s and run:", ilo["host"])
        for cmd in commands:
            LOG.info("[DRY-RUN]   %s", cmd)
        LOG.info("[DRY-RUN] Fan speed: %d%% (raw: %d/255)", fan_percent, speed_raw)
        return True

    # Use persistent session if available, fall back to one-shot
    if ssh_session is not None:
        ok = ssh_session.send_commands(commands)
        if ok:
            LOG.info("Fans set to %d%% (raw %d/255) via persistent SSH", fan_percent, speed_raw)
        else:
            LOG.error("Failed to set fans via persistent SSH")
        return ok

    # Fallback: one-shot SSH (for --once mode or when no session provided)
    ssh_cmd = [
        "sshpass", "-p", ilo["password"],
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", f"KexAlgorithms={ilo['ssh_kex']}",
        "-o", "HostKeyAlgorithms=ssh-rsa",
        "-o", "Ciphers=aes256-ctr",
        "-o", "PubkeyAcceptedAlgorithms=ssh-rsa",
        "-o", "ConnectTimeout=10",
        f"{ilo['username']}@{ilo['host']}",
    ]
    stdin_data = "\n".join(commands) + "\nexit\n"
    try:
        result = subprocess.run(
            ["timeout", "20"] + ssh_cmd,
            input=stdin_data,
            capture_output=True, text=True, timeout=25
        )
        output = result.stdout + result.stderr
        if "Permission denied" in output:
            LOG.error("SSH auth failed: %s", result.stderr.strip())
            return False
        if "Connection refused" in output or "No route" in output:
            LOG.error("SSH connection failed: %s", result.stderr.strip())
            return False
        LOG.info("Fans set to %d%% (raw %d/255)", fan_percent, speed_raw)
        return True
    except subprocess.TimeoutExpired:
        LOG.error("SSH to iLO timed out completely")
        return False
    except Exception as e:
        LOG.error("SSH fan set error: %s", e)
        return False

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def setup_logging(cfg: dict):
    level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout)]
    log_file = cfg.get("log_file")
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file))
        except OSError:
            pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def run_once(cfg: dict, dry_run: bool = False, state: dict = None,
             outdoor: "OutdoorTemp" = None,
             cooling: "CoolingDetector" = None,
             escalation: "EscalationTracker" = None,
             ssh_session: "IloSshSession" = None) -> dict:
    """Single control loop iteration. Returns updated state."""
    if state is None:
        state = {"failures": 0, "last_pct": None}

    temps = read_all_temps(cfg)

    if not temps:
        state["failures"] += 1
        max_failures = cfg.get("max_read_failures", 3)
        if state["failures"] >= max_failures:
            failsafe = cfg.get("failsafe_percent", 80)
            LOG.warning("No sensor data for %d cycles → failsafe %d%%",
                        state["failures"], failsafe)
            set_fans_ssh(cfg, failsafe, dry_run=dry_run, ssh_session=ssh_session)
            state["last_pct"] = failsafe
        return state

    state["failures"] = 0

    # Outdoor temp + adaptive cooling
    outdoor_temp = outdoor.get() if outdoor else None
    inlet_temp = temps.get("inlet")
    if cooling:
        cooling.record(inlet_temp, outdoor_temp)

    # Track escalation
    if escalation:
        escalation.record(temps)

    boost = compute_boost(cfg, temps, outdoor_temp)

    # Log temps
    temp_str = " | ".join(f"{k}: {v:.0f}°C" for k, v in sorted(temps.items()))
    extra = ""
    if outdoor_temp is not None:
        extra += f" | outdoor: {outdoor_temp:.0f}°C"
    if cooling and cooling.cooling_mode != "unknown":
        extra += f" | cooling: {cooling.cooling_mode}"
    if escalation and escalation.escalation_pct > 0:
        extra += f" | ESCALATION: +{escalation.escalation_pct:.0f}%"
    LOG.debug("Temps: %s%s", temp_str, extra)

    cooling_mode = cooling.cooling_mode if cooling else "unknown"
    esc_pct = escalation.escalation_pct if escalation else 0.0
    target = compute_target_fan(cfg, temps, boost=boost,
                                cooling_mode=cooling_mode,
                                escalation_pct=esc_pct)
    target = round(target, 1)

    # Only update if changed by >= 1%
    if state["last_pct"] is not None and abs(target - state["last_pct"]) < 1.0:
        LOG.debug("Fan unchanged at %.0f%%", state["last_pct"])
        return state

    LOG.info("Temps: %s%s → Fan: %.0f%%", temp_str, extra, target)
    set_fans_ssh(cfg, target, dry_run=dry_run, ssh_session=ssh_session)
    state["last_pct"] = target
    return state


def main():
    parser = argparse.ArgumentParser(description="Thermal fan controller for HP DL360p Gen8")
    parser.add_argument("-c", "--config", default="/etc/fan-controller/config.yaml",
                        help="Config file path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read temps but don't set fans")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)

    LOG.info("Fan controller starting (interval=%ds, min=%d%%, dry_run=%s)",
             cfg["interval"], cfg["min_fan_percent"], args.dry_run)

    running = True
    def handle_signal(sig, frame):
        nonlocal running
        LOG.info("Shutting down (signal %d)", sig)
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    outdoor = OutdoorTemp(cfg)
    cooling = CoolingDetector(cfg)
    escalation = EscalationTracker(cfg)
    state = {"failures": 0, "last_pct": None}

    if args.once:
        run_once(cfg, dry_run=args.dry_run, state=state,
                 outdoor=outdoor, cooling=cooling, escalation=escalation)
        return

    # Persistent SSH session — one connection, reused across cycles
    ssh_session = IloSshSession(cfg) if not args.dry_run else None

    try:
        while running:
            try:
                state = run_once(cfg, dry_run=args.dry_run, state=state,
                                 outdoor=outdoor, cooling=cooling,
                                 escalation=escalation,
                                 ssh_session=ssh_session)
            except Exception as e:
                LOG.exception("Unexpected error: %s", e)
            time.sleep(cfg["interval"])
    finally:
        if ssh_session:
            ssh_session.close()


if __name__ == "__main__":
    main()
