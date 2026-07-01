#!/usr/bin/env python3
"""Standalone thermal fan controller for HP DL360p Gen8 with modded iLO4.

Changes (2026-07-01):
  - Seasonal profile system: summer / spring / autumn / winter
  - Profile modes: calendar (astronomical), auto (outdoor temp), manual (--profile)
  - Global max-wins: all fans move together to single target speed
  - Ramp-rate limiter: ±2%/cycle (Fan Mod gives full control)
  - Outdoor temperature as direct curve input per profile
  - Summer baseline: min 33%, Winter: 28%
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections import deque
from datetime import date

import yaml

LOG = logging.getLogger("fan-controller")

# ---------------------------------------------------------------------------
# Astronomical season boundaries (Northern Hemisphere)
# Approximate fixed dates — good enough for fan control
# ---------------------------------------------------------------------------

SEASON_DATES = {
    # (month, day) → season starting on that date
    (3, 20):  "spring",
    (6, 21):  "summer",
    (9, 23):  "autumn",
    (12, 21): "winter",
}


def get_calendar_season() -> str:
    """Return current astronomical season based on date."""
    today = date.today()
    m, d = today.month, today.day

    if (m, d) >= (12, 21) or (m, d) < (3, 20):
        return "winter"
    elif (m, d) < (6, 21):
        return "spring"
    elif (m, d) < (9, 23):
        return "summer"
    else:
        return "autumn"


def get_auto_season(outdoor_temp: float | None, cfg: dict) -> str:
    """Return season based on outdoor temperature thresholds."""
    if outdoor_temp is None:
        return get_calendar_season()  # fallback to calendar

    auto_cfg = cfg.get("auto_profile", {})
    winter_below  = auto_cfg.get("winter_below", 8)
    spring_below  = auto_cfg.get("spring_below", 18)
    summer_above  = auto_cfg.get("summer_above", 24)

    if outdoor_temp < winter_below:
        return "winter"
    elif outdoor_temp < spring_below:
        # spring or autumn depending on calendar half-year
        return "spring" if date.today().month <= 6 else "autumn"
    elif outdoor_temp >= summer_above:
        return "summer"
    else:
        return "spring" if date.today().month <= 6 else "autumn"


def resolve_profile(cfg: dict, outdoor_temp: float | None,
                    cli_override: str | None = None) -> tuple[str, dict]:
    """Return (profile_name, profile_cfg) based on mode + override."""
    mode = cfg.get("profile_mode", "calendar")
    profiles = cfg.get("profiles", {})

    if cli_override:
        name = cli_override
    elif mode == "calendar":
        name = get_calendar_season()
    elif mode == "auto":
        name = get_auto_season(outdoor_temp, cfg)
    else:  # manual
        name = cfg.get("profile", "summer")

    if name not in profiles:
        LOG.warning("Profile '%s' not found, falling back to summer", name)
        name = "summer"

    return name, profiles[name]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
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
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("adaptive", {})
        self.enabled = self.cfg.get("enabled", False)
        window = self.cfg.get("history_window", 3600)
        self.history: deque = deque(maxlen=max(window // 15, 60))
        self.cooling_mode = "unknown"

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
        old = self.cooling_mode
        if corr is None:
            self.cooling_mode = "unknown"
        elif corr >= threshold:
            self.cooling_mode = "passive"
        else:
            self.cooling_mode = "active"
        if self.cooling_mode != old:
            LOG.info("Cooling mode: %s → %s (corr: %.2f)", old, self.cooling_mode, corr or 0)

    @staticmethod
    def _pearson(x, y) -> float | None:
        n = len(x)
        if n < 2:
            return None
        mx, my = sum(x) / n, sum(y) / n
        sx = sum((xi - mx) ** 2 for xi in x) ** 0.5
        sy = sum((yi - my) ** 2 for yi in y) ** 0.5
        if sx == 0 or sy == 0:
            return None
        return sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / (sx * sy)


# ---------------------------------------------------------------------------
# Fan curve interpolation
# ---------------------------------------------------------------------------

def interpolate_fan(curve: list, temp: float, min_pct: float) -> float:
    if temp <= curve[0][0]:
        return max(curve[0][1], min_pct)
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, f0 = curve[i]
        t1, f1 = curve[i + 1]
        if t0 <= temp <= t1:
            ratio = (temp - t0) / (t1 - t0)
            return max(f0 + ratio * (f1 - f0), min_pct)
    return max(curve[-1][1], min_pct)


# ---------------------------------------------------------------------------
# Escalation tracker
# ---------------------------------------------------------------------------

class EscalationTracker:
    def __init__(self, cfg: dict):
        esc = cfg.get("escalation", {})
        self.enabled = esc.get("enabled", True)
        self.window = esc.get("window_seconds", 180)
        self.step = esc.get("step_percent", 8)
        self.max_override = esc.get("max_percent", 100)
        self.history: dict[str, deque] = {}
        self.escalation_pct: float = 0.0

    def record(self, temps: dict[str, float]):
        if not self.enabled:
            return
        now = time.time()
        for sid, temp in temps.items():
            if sid not in self.history:
                self.history[sid] = deque(maxlen=200)
            self.history[sid].append((now, temp))
        self._evaluate(now)

    def _evaluate(self, now: float):
        cutoff = now - self.window
        rising = []
        for sid, hist in self.history.items():
            readings = [t for _, t in hist if _ >= cutoff]
            if len(readings) < 6:
                continue
            half = len(readings) // 2
            avg1 = sum(readings[:half]) / half
            avg2 = sum(readings[half:]) / (len(readings) - half)
            if avg2 - avg1 >= 2.0:
                rising.append(f"{sid} ({avg1:.0f}→{avg2:.0f}°C)")
        old = self.escalation_pct
        if rising:
            self.escalation_pct = min(self.escalation_pct + self.step, self.max_override)
            if self.escalation_pct != old:
                LOG.warning("ESCALATION +%d%%: %s", int(self.escalation_pct), ", ".join(rising))
        elif self.escalation_pct > 0:
            self.escalation_pct = max(self.escalation_pct - self.step, 0)
            if self.escalation_pct != old:
                LOG.info("De-escalation → %d%%", int(self.escalation_pct))


# ---------------------------------------------------------------------------
# Global target computation
# ---------------------------------------------------------------------------

def compute_target_fan(cfg: dict, profile: dict, temps: dict[str, float],
                       outdoor_temp: float | None = None,
                       cooling_mode: str = "unknown",
                       escalation_pct: float = 0.0) -> float:
    """Single global fan target from all sensor curves + outdoor curve (max-wins)."""
    min_pct = profile.get("min_fan_percent", cfg.get("min_fan_percent", 28))
    sensors_cfg = profile.get("sensors", {})
    candidates = []

    for sensor_id, temp in temps.items():
        scfg = sensors_cfg.get(sensor_id, {})
        critical = scfg.get("critical_temp", 100)
        curve = scfg.get("fan_curve")
        if not curve:
            continue
        if temp >= critical:
            LOG.warning("CRITICAL: %s %.1f°C → 100%%", sensor_id, temp)
            return 100.0
        pct = interpolate_fan(curve, temp, min_pct)
        LOG.debug("  %s: %.1f°C → %.0f%%", sensor_id, temp, pct)
        candidates.append(pct)

    # Outdoor curve (per profile)
    outdoor_curve = profile.get("outdoor_curve")
    if outdoor_temp is not None and outdoor_curve:
        pct = interpolate_fan(outdoor_curve, outdoor_temp, min_pct)
        LOG.debug("  outdoor: %.1f°C → %.0f%%", outdoor_temp, pct)
        candidates.append(pct)

    target = max(candidates) if candidates else min_pct

    # Boost: outdoor AND sensors both elevated
    outdoor_cfg = cfg.get("outdoor", {})
    if outdoor_temp is not None and outdoor_cfg.get("enabled"):
        boost_thr = outdoor_cfg.get("boost_outdoor_temp", 30)
        sensor_thrs = outdoor_cfg.get("boost_sensor_thresholds", {})
        boost_pct = outdoor_cfg.get("boost_percent", 3)
        if outdoor_temp >= boost_thr and any(
            temps.get(s, 0) >= t for s, t in sensor_thrs.items()
        ):
            old = target
            target = min(target + boost_pct, 100.0)
            LOG.info("Boost +%d%%: outdoor %.0f°C + sensors hot (%.0f→%.0f%%)",
                     boost_pct, outdoor_temp, old, target)

    if escalation_pct > 0:
        target = min(target + escalation_pct, 100.0)
        LOG.warning("Escalation +%.0f%% → %.0f%%", escalation_pct, target)

    if cooling_mode == "active":
        discount = cfg.get("adaptive", {}).get("discount_percent", 4)
        old = target
        target = max(target - discount, min_pct)
        LOG.info("Active cooling: -%.0f%% (%.0f→%.0f%%)", discount, old, target)

    return target


# ---------------------------------------------------------------------------
# Ramp-rate limiter
# ---------------------------------------------------------------------------

def apply_ramp(current: float | None, target: float,
               up_per_cycle: float, down_per_cycle: float) -> float:
    """Limit speed change per cycle — smooth transitions."""
    if current is None:
        return target
    if target > current:
        return min(target, current + up_per_cycle)
    elif target < current:
        return max(target, current - down_per_cycle)
    return current


# ---------------------------------------------------------------------------
# Temperature reading
# ---------------------------------------------------------------------------

def read_ipmi_temps() -> dict[str, float]:
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
            try:
                temps[parts[0]] = float(parts[4].replace("degrees C", "").strip())
            except ValueError:
                continue
    return temps


def read_lmsensors_temps() -> dict[str, float]:
    try:
        result = subprocess.run(["sensors", "-j"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
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
        for val in chip_data.values():
            if not isinstance(val, dict):
                continue
            for sk, sv in val.items():
                if "input" in sk and isinstance(sv, (int, float)) and sv > 0:
                    if max_temp is None or sv > max_temp:
                        max_temp = sv
        if max_temp is not None:
            temps[chip] = max_temp
    return temps


def read_all_temps(profile: dict) -> dict[str, float]:
    ipmi_temps = sensor_temps = None
    results = {}
    for sensor_id, scfg in profile.get("sensors", {}).items():
        source = scfg["source"]
        name = scfg["name"]
        if source == "ipmi":
            if ipmi_temps is None:
                ipmi_temps = read_ipmi_temps()
            temp = ipmi_temps.get(name)
        elif source == "sensors":
            if sensor_temps is None:
                sensor_temps = read_lmsensors_temps()
            temp = next((v for k, v in sensor_temps.items()
                         if k.startswith(name) or name in k), None)
        else:
            continue
        if temp is not None:
            results[sensor_id] = temp
        else:
            LOG.warning("Sensor '%s' (%s) not found", sensor_id, name)
    return results


# ---------------------------------------------------------------------------
# Fan control via SSH to iLO
# ---------------------------------------------------------------------------

class IloSshSession:
    """Persistent SSH connection to iLO4 (Fan Mod firmware)."""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._proc = None
        self._consecutive_failures = 0
        self._max_failures = 3
        self._send_timeout = 10

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
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
            "-tt",
            f"{ilo['username']}@{ilo['host']}",
        ]

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _drain_stdout(self):
        if self._proc and self._proc.stdout:
            import select
            while select.select([self._proc.stdout], [], [], 0)[0]:
                try:
                    self._proc.stdout.read1(4096)
                except Exception:
                    break

    def connect(self) -> bool:
        if self._is_alive():
            return True
        self.close()
        try:
            self._proc = subprocess.Popen(
                self._build_ssh_cmd(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(3)
            if not self._is_alive():
                stderr = ""
                if self._proc.stderr:
                    try:
                        stderr = self._proc.stderr.read1(4096).decode(errors="replace")
                    except Exception:
                        pass
                LOG.error("SSH session failed: %s", stderr.strip())
                self._proc = None
                return False
            self._consecutive_failures = 0
            LOG.info("Persistent SSH to iLO established (PID %d)", self._proc.pid)
            return True
        except Exception as e:
            LOG.error("Failed to open SSH session: %s", e)
            self._proc = None
            return False

    def send_commands(self, commands: list[str]) -> bool:
        if not self._is_alive():
            if not self.connect():
                self._consecutive_failures += 1
                return False
        self._drain_stdout()
        try:
            import threading
            data = "\n".join(commands) + "\n"
            write_ok = threading.Event()
            write_err = [None]

            def _do_write():
                try:
                    self._proc.stdin.write(data.encode())
                    self._proc.stdin.flush()
                    write_ok.set()
                except Exception as e:
                    write_err[0] = e
                    write_ok.set()

            t = threading.Thread(target=_do_write, daemon=True)
            t.start()
            t.join(timeout=self._send_timeout)
            if not write_ok.is_set():
                LOG.error("SSH write timed out — reconnecting")
                self._force_kill()
                self._consecutive_failures += 1
                return False
            if write_err[0]:
                raise write_err[0]
            time.sleep(0.3)
            if not self._is_alive():
                LOG.warning("SSH session died after sending")
                self._proc = None
                self._consecutive_failures += 1
                return False
            self._consecutive_failures = 0
            return True
        except (BrokenPipeError, OSError) as e:
            LOG.warning("SSH pipe broken: %s", e)
            self.close()
            self._consecutive_failures += 1
            return False

    def _force_kill(self):
        if self._proc:
            try:
                self._proc.kill()
                self._proc.wait(timeout=3)
            except Exception:
                pass
            finally:
                self._proc = None

    @property
    def healthy(self) -> bool:
        return self._consecutive_failures < self._max_failures

    def close(self):
        if self._proc:
            pid = self._proc.pid
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
            LOG.info("SSH session closed (PID %d)", pid)


def set_fans_ssh(cfg: dict, fan_percent: float, fan_count: int = 8,
                 dry_run: bool = False, ssh_session: IloSshSession = None) -> bool:
    """Set all fans to the same global speed via SSH to iLO4 (Fan Mod)."""
    speed_raw = max(0, min(255, int(round((fan_percent / 100.0) * 255))))
    commands = ["fan p global unlock"] + [f"fan p {i} lock {speed_raw}" for i in range(fan_count)]

    if dry_run:
        LOG.info("[DRY-RUN] All %d fans → %d%% (raw %d/255)", fan_count, fan_percent, speed_raw)
        return True

    if ssh_session:
        ok = ssh_session.send_commands(commands)
        if ok:
            LOG.info("All %d fans → %d%% (raw %d/255)", fan_count, fan_percent, speed_raw)
        else:
            LOG.error("Failed to set fans")
        return ok

    # Fallback: one-shot SSH
    ilo = cfg["ilo"]
    ssh_cmd = [
        "sshpass", "-p", ilo["password"], "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"KexAlgorithms={ilo['ssh_kex']}",
        "-o", "HostKeyAlgorithms=ssh-rsa",
        "-o", "Ciphers=aes256-ctr",
        "-o", "PubkeyAcceptedAlgorithms=ssh-rsa",
        "-o", "ConnectTimeout=10",
        f"{ilo['username']}@{ilo['host']}",
    ]
    try:
        result = subprocess.run(
            ["timeout", "20"] + ssh_cmd,
            input="\n".join(commands) + "\nexit\n",
            capture_output=True, text=True, timeout=25
        )
        out = result.stdout + result.stderr
        if "Permission denied" in out:
            LOG.error("SSH auth failed")
            return False
        if "Connection refused" in out or "No route" in out:
            LOG.error("SSH connection failed")
            return False
        LOG.info("All %d fans → %d%% (raw %d/255)", fan_count, fan_percent, speed_raw)
        return True
    except subprocess.TimeoutExpired:
        LOG.error("SSH to iLO timed out")
        return False
    except Exception as e:
        LOG.error("SSH error: %s", e)
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
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=handlers)


def run_once(cfg: dict, dry_run: bool = False, state: dict = None,
             outdoor: OutdoorTemp = None,
             cooling: CoolingDetector = None,
             escalation: EscalationTracker = None,
             ssh_session: IloSshSession = None,
             profile_override: str | None = None) -> dict:
    if state is None:
        state = {"failures": 0, "last_pct": None, "last_profile": None}

    ramp_cfg = cfg.get("ramp", {})
    ramp_up   = ramp_cfg.get("up_per_cycle", 2)
    ramp_down = ramp_cfg.get("down_per_cycle", 2)

    outdoor_temp = outdoor.get() if outdoor else None

    # Resolve active profile
    profile_name, profile = resolve_profile(cfg, outdoor_temp, profile_override)
    if profile_name != state.get("last_profile"):
        LOG.info("Profile switched: %s → %s", state.get("last_profile", "—"), profile_name)
        state["last_profile"] = profile_name

    temps = read_all_temps(profile)

    if not temps:
        state["failures"] += 1
        if state["failures"] >= cfg.get("max_read_failures", 3):
            failsafe = cfg.get("failsafe_percent", 80)
            LOG.warning("No sensor data for %d cycles → failsafe %d%%",
                        state["failures"], failsafe)
            set_fans_ssh(cfg, failsafe, dry_run=dry_run, ssh_session=ssh_session)
            state["last_pct"] = failsafe
        return state

    state["failures"] = 0

    inlet_temp = temps.get("inlet")
    if cooling:
        cooling.record(inlet_temp, outdoor_temp)
    if escalation:
        escalation.record(temps)

    cooling_mode = cooling.cooling_mode if cooling else "unknown"
    esc_pct = escalation.escalation_pct if escalation else 0.0

    raw_target = compute_target_fan(cfg, profile, temps,
                                    outdoor_temp=outdoor_temp,
                                    cooling_mode=cooling_mode,
                                    escalation_pct=esc_pct)

    ramped = round(apply_ramp(state["last_pct"], raw_target, ramp_up, ramp_down), 1)

    temp_str = " | ".join(f"{k}: {v:.0f}°C" for k, v in sorted(temps.items()))
    extra = f" | profile: {profile_name}"
    if outdoor_temp is not None:
        extra += f" | outdoor: {outdoor_temp:.0f}°C"
    if cooling_mode != "unknown":
        extra += f" | cooling: {cooling_mode}"
    if esc_pct > 0:
        extra += f" | ESC: +{esc_pct:.0f}%"
    if ramped != raw_target:
        extra += f" | ramp: {raw_target:.0f}→{ramped:.0f}%"

    if state["last_pct"] is not None and abs(ramped - state["last_pct"]) < 1.0:
        LOG.debug("Fans unchanged at %.0f%%", state["last_pct"])
        return state

    LOG.info("Temps: %s%s → Global fans: %.0f%%", temp_str, extra, ramped)
    set_fans_ssh(cfg, ramped, dry_run=dry_run, ssh_session=ssh_session)
    state["last_pct"] = ramped
    return state


def main():
    parser = argparse.ArgumentParser(
        description="Thermal fan controller for HP DL360p Gen8 — seasonal profiles"
    )
    parser.add_argument("-c", "--config", default="/etc/fan-controller/config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--profile", choices=["summer", "spring", "autumn", "winter"],
                        help="Override profile (ignores profile_mode in config)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)

    mode = cfg.get("profile_mode", "calendar")
    LOG.info("Fan controller starting (interval=%ds, mode=%s, dry_run=%s)",
             cfg["interval"], args.profile or mode, args.dry_run)

    running = True
    def handle_signal(sig, frame):
        nonlocal running
        LOG.info("Shutting down (signal %d)", sig)
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    outdoor   = OutdoorTemp(cfg)
    cooling   = CoolingDetector(cfg)
    escalation = EscalationTracker(cfg)
    state = {"failures": 0, "last_pct": None, "last_profile": None}

    if args.once:
        run_once(cfg, dry_run=args.dry_run, state=state,
                 outdoor=outdoor, cooling=cooling, escalation=escalation,
                 profile_override=args.profile)
        return

    ssh_session = IloSshSession(cfg) if not args.dry_run else None

    try:
        while running:
            try:
                state = run_once(cfg, dry_run=args.dry_run, state=state,
                                 outdoor=outdoor, cooling=cooling, escalation=escalation,
                                 ssh_session=ssh_session, profile_override=args.profile)
            except Exception as e:
                LOG.exception("Unexpected error: %s", e)
            time.sleep(cfg["interval"])
    finally:
        if ssh_session:
            ssh_session.close()


if __name__ == "__main__":
    main()
