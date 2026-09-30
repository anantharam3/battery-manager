#!/usr/bin/env python3
"""
battery_manager_v3.py — Dell Server Battery Manager
=====================================================
Two modes in a single script:

  1. CALIBRATE mode  — Fully automated 3-cycle discharge→charge calibration.
                       No shutdown. No manual steps. Server stays ON throughout.
                       At 5% → plug ON automatically. At BMS Full → plug OFF.
                       Repeats for N cycles. State persists across reboots.

  2. DAEMON mode     — Normal operation: keeps battery between LOW–HIGH%
                       using smart plug (Tuya) control. Hourly Telegram alerts.

Usage:
  python3 battery_manager_v3.py                  # Auto-detect mode
  python3 battery_manager_v3.py --calibrate      # Start 3-cycle calibration
  python3 battery_manager_v3.py --calibrate 2   # Start 2-cycle calibration
  python3 battery_manager_v3.py --daemon         # Force daemon mode
  python3 battery_manager_v3.py --test           # Test Tuya + Telegram + battery
  python3 battery_manager_v3.py --status         # One-shot status and exit

Telegram commands:
  /status              — Full status (battery %, temps, load, uptime)
  /plug on             — Force charger ON
  /plug off            — Force charger OFF
  /set low N           — Daemon LOW threshold %
  /set high N          — Daemon HIGH threshold %
  /set alert N         — Alert interval (hours)
  /calibrate start     — Start battery calibration
  /calibrate status    — Check calibration progress
  /ps                  — Top 8 processes by CPU
  /stop                — Stop the battery manager script
  /shutdown            — Shut down server (needs /confirm)
  /reboot              — Reboot server   (needs /confirm)
  /confirm             — Confirm pending shutdown or reboot
  /help                — List all commands

Author: Ram / Antigravity
"""

import os
import sys
import json
import time
import logging
import argparse
import subprocess
import threading
from pathlib import Path
from datetime import datetime


# ── Graceful exit signal (used instead of sys.exit inside loop functions
#    so the PID-lock finally: block in __main__ always executes) ─────────────
class _GracefulExit(SystemExit):
    """Raised by /stop, /shutdown, /reboot handlers to exit cleanly."""
    pass

import requests
import tinytuya
from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv(Path.home() / ".env")

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR   = Path("/data")
LOG_FILE   = DATA_DIR / "battery_manager.log"
STATE_FILE = DATA_DIR / "battery_state.json"
PID_FILE   = DATA_DIR / "battery_manager.pid"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
_handlers: list = [logging.FileHandler(LOG_FILE)]
if sys.stdout.isatty():
    # Only add console handler when running interactively.
    # When launched via cron/nohup (stdout redirected to log file),
    # StreamHandler would duplicate every line into the same file.
    _handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
CFG = {
    # Daemon mode battery band
    "LOW_THRESHOLD":        45,     # Turn plug ON  when battery drops here
    "HIGH_THRESHOLD":       90,     # Turn plug OFF when battery reaches here

    # Calibration settings
    "CALIB_LOW_PCT":         5,     # Plug ON when discharge reaches this %
    "CALIB_CYCLES":          3,     # Default number of calibration cycles
    "CALIB_REST_MINUTES":   10,     # Minutes to rest at BMS Full between cycles (10 is sufficient for cell settling)

    # Alerts
    "ALERT_INTERVAL_HOURS":  1,     # Hourly status alert in daemon mode
    "POLL_INTERVAL":        60,     # Seconds between main loop iterations

    # Tuya smart plug (local control)
    "TUYA_ID":    os.getenv("TUYA_DEVICE_ID",  "d776ef2e8d51354c4cc8pw"),
    "TUYA_IP":    os.getenv("TUYA_IP_ADDRESS",  "192.168.1.15"),
    "TUYA_KEY":   os.getenv("TUYA_LOCAL_KEY",   "Bz#6yAf)IGBz]14~"),
    "TUYA_VER":   3.3,

    # Telegram
    "TG_TOKEN":   os.getenv("TELEGRAM_BOT_TOKEN", "8875622001:AAFWgNK1u9meG2GuVCh4fRkBJPttsxNzzeU"),
    "TG_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID",   "315432952"),
}

# Discharge alert thresholds (fires once per threshold descending)
DISCHARGE_ALERTS = [80, 70, 60, 50, 40, 30, 20, 15, 10, 7]
# Charge alert thresholds (fires once per threshold ascending)
CHARGE_ALERTS    = [25, 50, 75, 90]

DESIGN_MAH = 6491   # Dell Inspiron 7559 new battery design capacity


# ══════════════════════════════════════════════════════════════════════════════
# SMART PLUG  (Tuya local control)
# ══════════════════════════════════════════════════════════════════════════════
def _plug() -> tinytuya.OutletDevice:
    if not hasattr(_plug, "instance"):
        _plug.instance = tinytuya.OutletDevice(CFG["TUYA_ID"], CFG["TUYA_IP"], CFG["TUYA_KEY"])
        _plug.instance.set_version(CFG["TUYA_VER"])
        _plug.instance.set_socketTimeout(3)
    return _plug.instance


def plug_on(reason: str = "", notify: bool = True) -> bool:
    for attempt in range(3):
        try:
            res = _plug().turn_on()
            if isinstance(res, dict) and "Error" in res:
                log.warning(f"plug_on attempt {attempt+1}/3 failed: {res['Error']}")
                time.sleep(2)
                continue
            log.info(f"⚡ Plug ON{' — ' + reason if reason else ''}")
            if notify:
                bat = battery()
                tg_send(f"⚡ *Plug ON*{' — ' + reason if reason else ''}\nBattery: {bat['cap']}% | {bat['power_w']}W")
            return True
        except Exception as e:
            log.warning(f"plug_on attempt {attempt+1}/3 exception: {e}")
            time.sleep(2)
    log.error("plug_on: all retries failed")
    return False


def plug_off(reason: str = "", notify: bool = True) -> bool:
    for attempt in range(3):
        try:
            res = _plug().turn_off()
            if isinstance(res, dict) and "Error" in res:
                log.warning(f"plug_off attempt {attempt+1}/3 failed: {res['Error']}")
                time.sleep(2)
                continue
            log.info(f"🔌 Plug OFF{' — ' + reason if reason else ''}")
            if notify:
                bat = battery()
                tg_send(f"🔌 *Plug OFF*{' — ' + reason if reason else ''}\nBattery: {bat['cap']}% | {bat['power_w']}W")
            return True
        except Exception as e:
            log.warning(f"plug_off attempt {attempt+1}/3 exception: {e}")
            time.sleep(2)
    log.error("plug_off: all retries failed")
    return False


def plug_state() -> bool | None:
    """Returns True=ON, False=OFF, None=unreachable."""
    try:
        return _plug().status().get("dps", {}).get("1", None)
    except Exception as e:
        log.warning(f"plug_state read failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# BATTERY
# ══════════════════════════════════════════════════════════════════════════════
BAT = Path("/sys/class/power_supply/BAT0")


def _bat(attr: str) -> str:
    try:
        return (BAT / attr).read_text().strip()
    except Exception:
        return ""


def battery() -> dict:
    try:
        cap         = int(_bat("capacity") or 0)
        status      = _bat("status") or "Unknown"
        voltage     = int(_bat("voltage_now") or 0)    # µV
        current     = int(_bat("current_now") or 0)    # µA
        power_w     = round(abs(voltage * current) / 1e12, 1)
        charge_now  = int(_bat("charge_now") or 0)     # µAh
        charge_full = int(_bat("charge_full") or 0)    # µAh
        return {
            "cap":         cap,
            "status":      status,
            "power_w":     power_w,
            "voltage_mv":  voltage // 1000,
            "current_ma":  current // 1000,
            "charge_now":  charge_now,
            "charge_full": charge_full,
        }
    except Exception as e:
        log.error(f"battery read error: {e}")
        return {"cap": -1, "status": "Unknown", "power_w": 0.0,
                "voltage_mv": 0, "current_ma": 0,
                "charge_now": 0, "charge_full": 0}


def eta_str(cap: int, current_ma: int, status: str) -> str:
    if current_ma <= 0:
        return "unknown"
    if status == "Discharging":
        mah_left = DESIGN_MAH * cap / 100
    else:
        mah_left = DESIGN_MAH * (100 - cap) / 100
    hours = mah_left / current_ma
    return f"~{int(hours)}h {int((hours % 1) * 60)}m"


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM STATS
# ══════════════════════════════════════════════════════════════════════════════
def cpu_temp_c() -> float | None:
    """Read CPU package temp from coretemp hwmon. Returns °C or None."""
    try:
        for hwmon in sorted(Path("/sys/class/hwmon").iterdir()):
            name_file = hwmon / "name"
            if name_file.exists() and name_file.read_text().strip() == "coretemp":
                # temp1_input is the Package (overall) temp in milli°C
                t = (hwmon / "temp1_input").read_text().strip()
                return round(int(t) / 1000, 1)
    except Exception:
        pass
    return None


def bat_temp_c() -> float | None:
    """Read battery temp if exposed by kernel. Dell 7559 may not expose this."""
    try:
        # Kernel reports in tenths of °C
        t = int((BAT / "temp").read_text().strip())
        return round(t / 10, 1)
    except Exception:
        return None


def system_stats() -> dict:
    st = {}
    try:
        secs = float(open("/proc/uptime").read().split()[0])
        st["uptime"] = f"{int(secs//3600)}h {int((secs%3600)//60)}m"
    except Exception:
        st["uptime"] = "N/A"
    try:
        p = open("/proc/loadavg").read().split()
        st["load"] = f"{p[0]}, {p[1]}, {p[2]}"
    except Exception:
        st["load"] = "N/A"
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            mem[k.strip()] = int(v.split()[0])
        total = mem["MemTotal"] // 1024
        used  = total - mem["MemAvailable"] // 1024
        st["mem"] = f"{used}MB/{total}MB"
    except Exception:
        st["mem"] = "N/A"
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        st["ip"] = r.stdout.strip().split()[0]
    except Exception:
        st["ip"] = "N/A"
    st["cpu_temp"] = cpu_temp_c()
    st["bat_temp"] = bat_temp_c()
    return st


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════
def tg_send(msg: str, retries: int = 4) -> bool:
    url      = f"https://api.telegram.org/bot{CFG['TG_TOKEN']}/sendMessage"
    full_msg = f"💻 *[DELL Server]*\n{msg}"
    for i in range(retries):
        try:
            resp = requests.post(
                url,
                json={"chat_id": CFG["TG_CHAT_ID"], "text": full_msg, "parse_mode": "Markdown"},
                timeout=10,
            )
            if resp.ok:
                log.info("📨 Telegram sent")
                return True
            log.warning(f"Telegram HTTP {resp.status_code}")
        except Exception as e:
            log.warning(f"Telegram attempt {i+1}/{retries}: {e}")
            time.sleep(5 * (i + 1))
    log.error("Telegram: all retries failed")
    return False


def status_msg() -> str:
    bat      = battery()
    plug     = plug_state()
    st       = system_stats()
    plug_sym = "⚡ ON" if plug else ("🔌 OFF" if plug is False else "❓ ?")
    state    = state_read()
    mode_line = ""
    if state.get("mode") == "calibrating":
        mode_line = f"\n🔋 Calibrating: Cycle {state['cycle']}/{state['cycles_target']} ({state['phase'].upper()})"
    cpu_t = f"{st['cpu_temp']}°C" if st["cpu_temp"] is not None else "N/A"
    bat_t = f"{st['bat_temp']}°C" if st["bat_temp"] is not None else "N/A"
    return (
        f"📊 *Dell Server — {datetime.now().strftime('%H:%M %d-%b')}*\n"
        f"🔋 Battery: *{bat['cap']}%* ({bat['status']}) | {bat['power_w']}W\n"
        f"Plug: {plug_sym}"
        f"{mode_line}\n"
        f"Band: {CFG['LOW_THRESHOLD']}%–{CFG['HIGH_THRESHOLD']}%\n"
        f"🌡️ CPU: {cpu_t} | 🔋 Bat: {bat_t}\n"
        f"⏱ Uptime: {st['uptime']} | Load: {st['load']}\n"
        f"💾 {st['mem']} | 🌐 {st['ip']}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CALIBRATION STATE  (persists across unexpected reboots)
# ══════════════════════════════════════════════════════════════════════════════
def state_read() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def state_write(data: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2))
        log.info(f"State saved: {data}")
    except OSError as e:
        log.error(f"state_write FAILED ({e}) — calibration state NOT persisted! Disk full or permissions issue?")
        raise  # Re-raise: fail at the right call site with the real error, not a KeyError one tick later


def state_clear() -> None:
    STATE_FILE.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# CALIBRATION MODE  — fully automated, no shutdown, server stays ON
# ══════════════════════════════════════════════════════════════════════════════
def run_calibration(cycles_target: int = 3) -> None:
    """
    Calibration flow per cycle:
      1. Plug OFF → idle discharge (natural, no stress)
      2. At 5%   → Plug ON automatically (server keeps running)
      3. BMS Full → Plug ON stays → rest CALIB_REST_MINUTES → Plug OFF → next cycle

    State saved to /data/battery_state.json (survives unexpected reboots).
    """
    state = state_read()

    if state.get("mode") != "calibrating":
        # ── Fresh start ───────────────────────────────────────────────────────
        state = {
            "mode":                   "calibrating",
            "cycle":                  1,
            "phase":                  "discharging",
            "cycles_target":          cycles_target,
            "started_at":             datetime.now().isoformat(),
            "discharge_alerts_fired": [],
            "charge_alerts_fired":    [],
        }
        state_write(state)
        log.info(f"=== Calibration START: {cycles_target} cycles ===")
        plug_off("calibration start — beginning discharge cycle 1", notify=False)
    else:
        # ── Resume after unexpected reboot ────────────────────────────────────
        cycle         = state["cycle"]
        phase         = state["phase"]
        cycles_target = state["cycles_target"]
        log.info(f"=== Calibration RESUMED: cycle {cycle}/{cycles_target}, phase={phase} ===")
        if phase == "discharging":
            plug_off("calibration resume — still discharging", notify=False)
        elif phase == "charging":
            plug_on("calibration resume — charging", notify=False)

    # Clear any stale /calibrate start flag — prevents an unintended extra run
    # when calibration finishes and daemon mode resumes.
    _calibrate_requested.clear()

    # ── Main calibration loop ─────────────────────────────────────────────────
    while True:
        state       = state_read()
        bat         = battery()
        cap         = bat["cap"]
        bst         = bat["status"]
        cur         = bat["current_ma"]
        charge_now  = bat["charge_now"]
        charge_full = bat["charge_full"]
        cycle       = state["cycle"]
        phase       = state["phase"]
        ct          = state["cycles_target"]

        # ── Detect "at full" ───────────────────────────────────────────────
        # Four conditions cover all Dell EC / BMS combinations:
        #  1. BMS declares "Full" explicitly
        #  2. EC charge ceiling (BIOS Adaptive): "Not charging" + 0W draw at >=85%
        #  3. Trickle done: 100% + <0.5W draw (BMS says Charging but nearly done)
        #  4. Stuck-trickle timeout: 100% for >= 60 min (Dell ECs that never
        #     drop power to near-zero — belt-and-suspenders fallback)
        # Note: BIOS is now Standard — full 6491 mAh is reachable (not 5810 mAh).

        # Track when we first reach 100% in charging phase (for condition 4).
        # Must write to disk immediately — state_read() at loop top overwrites in-memory state.
        if cap >= 100 and phase == "charging":
            if not state.get("at_full_since"):
                state["at_full_since"] = datetime.now().isoformat()
                state_write(state)

        mins_at_full = 0.0
        if state.get("at_full_since"):
            mins_at_full = (
                datetime.now() - datetime.fromisoformat(state["at_full_since"])
            ).total_seconds() / 60

        at_full = (
            bst == "Full"                                              # 1
            or (bst == "Not charging" and cap >= 85                   # 2
                and bat["power_w"] == 0.0)
            or (cap >= 100 and bat["power_w"] < 0.5)                 # 3
            or (cap >= 100 and mins_at_full >= 60)                   # 4
        )

        log.info(
            f"[CALIB] Cycle {cycle}/{ct} | {phase.upper()} | "
            f"{cap}% | {bst} | {bat['power_w']}W | "
            f"charge: {charge_now//1000}/{charge_full//1000} mAh | "
            f"ETA: {eta_str(cap, cur, bst)}"
        )

        # ── DISCHARGING ───────────────────────────────────────────────────────
        if phase == "discharging":

            if cap <= CFG["CALIB_LOW_PCT"]:
                # Low point reached — plug ON, switch phase (no shutdown!)
                log.info(f"Low point {cap}% reached — plug ON, switching to charging")
                plug_on(f"cycle {cycle} — low point reached, auto charging", notify=False)
                state["phase"]               = "charging"
                state["charge_alerts_fired"] = []
                state_write(state)
                tg_send(
                    f"🔁 *Cycle {cycle}/{ct}: Discharge DONE*\n"
                    f"Battery at {cap}% — Charger turned ON automatically.\n"
                    f"Charging to BMS Full. Server stays running ✅"
                )
            else:
                # Discharge threshold alerts (fires once per level)
                fired = state.get("discharge_alerts_fired", [])
                for t in sorted(DISCHARGE_ALERTS, reverse=True):
                    if cap <= t and t not in fired:
                        fired.append(t)
                        state["discharge_alerts_fired"] = fired
                        state_write(state)
                        tg_send(
                            f"📉 *{cap}%* — Discharging (Cycle {cycle}/{ct})\n"
                            f"ETA to {CFG['CALIB_LOW_PCT']}%: {eta_str(cap, cur, bst)}"
                        )
                        break

        # ── CHARGING ──────────────────────────────────────────────────────────
        elif phase == "charging":

            # Safety: ensure plug is still ON.
            # If plug is DEFINITELY OFF (False), turn it on.
            # If Tuya is unreachable (None), but laptop is draining, force it ON.
            ps = plug_state()
            if ps is False or (ps is None and bat["status"] not in ("Charging", "Full") and cap < 95):
                plug_on("calibration charging — safety check: plug OFF or laptop discharging, forcing ON")

            # Charge threshold alerts (fires once per level)
            fired = state.get("charge_alerts_fired", [])
            for t in sorted(CHARGE_ALERTS):
                if cap >= t and t not in fired:
                    fired.append(t)
                    state["charge_alerts_fired"] = fired
                    state_write(state)
                    tg_send(
                        f"📈 *{cap}%* — Charging (Cycle {cycle}/{ct})\n"
                        f"ETA to Full: {eta_str(cap, cur, bst)}"
                    )
                    break

            # BMS reports Full (or "Not charging" at charge_full on Dell) — calibration point
            if at_full:
                if bst == "Full":
                    full_reason = "BMS Full"
                elif bst == "Not charging":
                    full_reason = f"EC ceiling @ {cap}% ({charge_now//1000} mAh, 0W)"
                elif bat["power_w"] < 0.5:
                    full_reason = f"Trickle done @ {cap}% ({charge_now//1000} mAh, {bat['power_w']}W)"
                else:
                    full_reason = f"Timeout @ {cap}% — {int(mins_at_full)}min at full, {bat['power_w']}W"
                log.info(f"Cycle {cycle}: {full_reason}! Resting {CFG['CALIB_REST_MINUTES']} min...")
                tg_send(
                    f"✅ *Cycle {cycle}/{ct}: Charge Complete!*\n"
                    f"({full_reason})\n"
                    f"Resting {CFG['CALIB_REST_MINUTES']} min (cell balancing).\n"
                    f"Plug stays ON during rest."
                )
                # Interruptible rest — checks /stop every 60s so it can exit cleanly.
                # A plain time.sleep(3600) would block /stop for the entire rest period.
                _rest_end = time.time() + CFG["CALIB_REST_MINUTES"] * 60
                while time.time() < _rest_end:
                    if _stop_requested.is_set():
                        log.info("\U0001f6d1 /stop received during rest — exiting cleanly.")
                        tg_send(
                            "\U0001f6d1 *Stopped during rest phase.*\n"
                            "State saved — restart script to resume calibration."
                        )
                        raise _GracefulExit(0)
                    time.sleep(min(60, max(0, _rest_end - time.time())))

                if cycle >= ct:
                    # ── All cycles complete ───────────────────────────────────
                    plug_off("calibration complete", notify=False)
                    state_clear()
                    tg_send(
                        f"🎉 *Battery Calibration COMPLETE!*\n"
                        f"Completed {cycle} full discharge→charge cycles.\n"
                        f"Fuel gauge is now calibrated.\n"
                        f"Switching to Daemon mode "
                        f"(band: {CFG['LOW_THRESHOLD']}%–{CFG['HIGH_THRESHOLD']}%)"
                    )
                    log.info("=== CALIBRATION COMPLETE — starting daemon ===")
                    run_daemon()
                    return
                else:
                    # ── Start next cycle ──────────────────────────────────────
                    cycle += 1
                    state["cycle"]                  = cycle
                    state["phase"]                  = "discharging"
                    state["discharge_alerts_fired"] = []
                    state["charge_alerts_fired"]    = []
                    state.pop("at_full_since", None)   # clear — don't bleed into next cycle
                    state_write(state)
                    plug_off(f"cycle {cycle} — starting discharge", notify=False)
                    tg_send(
                        f"🔌 *Cycle {cycle}/{ct}: DISCHARGING*\n"
                        f"Charger OFF. Discharging to {CFG['CALIB_LOW_PCT']}%."
                    )

        if _stop_requested.is_set():
            log.info("🛑 /stop received — calibration loop exiting cleanly.")
            tg_send("🛑 *Battery Manager stopped* (calibration paused).\nCalibration state saved — will resume on next start.")
            raise _GracefulExit(0)

        time.sleep(CFG["POLL_INTERVAL"])


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMAND LISTENER  (background thread)
# ══════════════════════════════════════════════════════════════════════════════
_last_update_id      = 0
_calibrate_requested = threading.Event()
_stop_requested      = threading.Event()   # set by /stop Telegram command

# Pending confirmation for destructive actions (/shutdown, /reboot)
_pending_action: dict = {}   # {"action": str, "expires": float}


def _drain_old_updates() -> None:
    global _last_update_id
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{CFG['TG_TOKEN']}/getUpdates",
            params={"offset": -1}, timeout=10
        )
        if resp.ok:
            results = resp.json().get("result", [])
            if results:
                _last_update_id = results[-1]["update_id"]
    except Exception:
        pass


def _get_updates() -> list:
    global _last_update_id
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{CFG['TG_TOKEN']}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 5},
            timeout=12,
        )
        if resp.ok:
            updates = resp.json().get("result", [])
            if updates:
                _last_update_id = updates[-1]["update_id"]
            return updates
    except Exception:
        pass
    return []


def _handle_command(text: str) -> str:
    cmd = text.strip().lower()

    if cmd == "/status":
        return status_msg()

    elif cmd == "/plug on":
        plug_on("Telegram command", notify=False)
        return "⚡ Plug turned *ON* via Telegram"

    elif cmd == "/plug off":
        plug_off("Telegram command", notify=False)
        return "🔌 Plug turned *OFF* via Telegram"

    elif cmd.startswith("/set low "):
        try:
            val = int(cmd.split()[-1])
            CFG["LOW_THRESHOLD"] = val
            return f"✅ LOW threshold → *{val}%*"
        except Exception:
            return "❌ Usage: `/set low 40`"

    elif cmd.startswith("/set high "):
        try:
            val = int(cmd.split()[-1])
            CFG["HIGH_THRESHOLD"] = val
            return f"✅ HIGH threshold → *{val}%*"
        except Exception:
            return "❌ Usage: `/set high 90`"

    elif cmd.startswith("/set alert "):
        try:
            val = float(cmd.split()[-1])
            CFG["ALERT_INTERVAL_HOURS"] = val
            return f"✅ Alert interval → *{val}h*"
        except Exception:
            return "❌ Usage: `/set alert 2`"

    elif cmd == "/calibrate start":
        _calibrate_requested.set()
        return "🔋 Calibration requested — switching to calibration mode..."

    elif cmd == "/calibrate status":
        st = state_read()
        if st.get("mode") == "calibrating":
            bat = battery()
            return (
                f"🔋 *Calibration in progress*\n"
                f"Cycle: {st['cycle']}/{st['cycles_target']}\n"
                f"Phase: {st['phase'].upper()}\n"
                f"Battery: {bat['cap']}% ({bat['status']})"
            )
        return "ℹ️ No calibration in progress. Running in Daemon mode."

    elif cmd == "/stop":
        _stop_requested.set()
        return (
            "🛑 *Battery Manager stopping...*\n"
            "Plug left in current state. Script will exit within 60s.\n"
            "Restart: `nohup python3 ~/battery_manager_git/battery_manager.py >> /data/battery_manager.log 2>&1 &`"
        )

    elif cmd == "/ps":
        try:
            r = subprocess.run(
                ["ps", "aux", "--sort=-%cpu", "--no-headers", "-ww"],
                capture_output=True, text=True, timeout=5
            )
            lines = r.stdout.strip().splitlines()[:8]
            rows = []
            for l in lines:
                parts = l.split(None, 10)
                if len(parts) >= 11:
                    safe_proc = parts[10][:35].replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "")
                    rows.append(f"`{parts[1]:>5}` {float(parts[2]):4.1f}% {safe_proc}")
            return "📊 *Top Processes (by CPU)*\n" + "\n".join(rows)
        except Exception as e:
            return f"❌ ps failed: {e}"

    elif cmd in ("/shutdown", "/reboot"):
        action = "shutdown" if cmd == "/shutdown" else "reboot"
        _pending_action["action"]  = action
        _pending_action["expires"] = time.time() + 30
        icon = "⚠️" if action == "shutdown" else "🔄"
        return (
            f"{icon} *{action.capitalize()} requested.*\n"
            f"Send `/confirm` within 30s to proceed, or ignore to cancel."
        )

    elif cmd == "/confirm":
        if not _pending_action or time.time() > _pending_action.get("expires", 0):
            return "❌ No pending action (timed out or nothing to confirm)."
        action = _pending_action.pop("action", None)
        _pending_action.clear()
        if action == "shutdown":
            tg_send("⚠️ *Confirmed. Shutting down in 5s...*")
            time.sleep(5)
            subprocess.run(["sudo", "shutdown", "-h", "now"])
        elif action == "reboot":
            tg_send("🔄 *Confirmed. Rebooting in 5s...*")
            time.sleep(5)
            subprocess.run(["sudo", "reboot"])
        return ""

    elif cmd == "/help":
        return (
            "📋 *Battery Manager v3 Commands*\n"
            "`/status` — Full status (battery, temps, load)\n"
            "`/plug on` — Force charger ON\n"
            "`/plug off` — Force charger OFF\n"
            "`/set low N` — Daemon LOW threshold %\n"
            "`/set high N` — Daemon HIGH threshold %\n"
            "`/set alert N` — Alert interval hours\n"
            "`/calibrate start` — Start battery calibration\n"
            "`/calibrate status` — Calibration progress\n"
            "`/ps` — Top 8 processes by CPU\n"
            "`/stop` — Stop battery manager script\n"
            "`/shutdown` — Shutdown (needs `/confirm`)\n"
            "`/reboot` — Reboot (needs `/confirm`)\n"
            "`/confirm` — Confirm shutdown or reboot\n"
            "`/help` — This message"
        )

    return f"❓ Unknown: `{text}`\nSend `/help` for commands."


def telegram_listener_thread() -> None:
    log.info("📡 Telegram command listener started")
    _drain_old_updates()
    while True:
        try:
            for update in _get_updates():
                msg     = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "")
                if chat_id != CFG["TG_CHAT_ID"] or not text.startswith("/"):
                    continue
                log.info(f"📩 Telegram: {text}")
                reply = _handle_command(text)
                if reply:
                    tg_send(reply)
        except Exception as e:
            log.warning(f"Telegram listener error: {e}")
        time.sleep(2)


# ══════════════════════════════════════════════════════════════════════════════
# DAEMON MODE  — normal battery band management
# ══════════════════════════════════════════════════════════════════════════════
def run_daemon() -> None:
    log.info("=" * 60)
    log.info("🚀 Battery Manager v3 — DAEMON MODE")
    log.info(f"   Band   : {CFG['LOW_THRESHOLD']}%–{CFG['HIGH_THRESHOLD']}%")
    log.info(f"   Poll   : {CFG['POLL_INTERVAL']}s")
    log.info(f"   Alerts : every {CFG['ALERT_INTERVAL_HOURS']}h")
    log.info("=" * 60)

    bat = battery()
    tg_send(
        f"🚀 *Battery Manager v3 Started (Daemon)*\n"
        f"Battery: {bat['cap']}% ({bat['status']}) | {bat['power_w']}W\n"
        f"Band: {CFG['LOW_THRESHOLD']}%–{CFG['HIGH_THRESHOLD']}%\n"
        f"Send `/help` for commands ✅"
    )

    last_alert          = datetime.min
    plug_fail_count     = 0          # consecutive plug_state() None returns
    last_plug_alert     = datetime.min  # rate-limit unreachable alerts

    while True:
        try:
            if _calibrate_requested.is_set():
                _calibrate_requested.clear()
                log.info("Calibration requested from Telegram — switching mode")
                run_calibration(CFG["CALIB_CYCLES"])
                return

            bat   = battery()
            cap   = bat["cap"]
            plugs = plug_state()
            low   = CFG["LOW_THRESHOLD"]
            high  = CFG["HIGH_THRESHOLD"]

            # ── Bug 5: Track plug unreachability and alert ─────────────────────
            if plugs is None:
                plug_fail_count += 1
                mins_since_plug_alert = (datetime.now() - last_plug_alert).total_seconds() / 60
                if plug_fail_count >= 3 and mins_since_plug_alert >= 30:
                    log.warning(f"Tuya plug unreachable for {plug_fail_count} consecutive polls")
                    tg_send(
                        f"⚠️ *Smart Plug UNREACHABLE*\n"
                        f"Failed {plug_fail_count} consecutive checks.\n"
                        f"Expected IP: {CFG['TUYA_IP']}\n"
                        f"Check plug power / Wi-Fi / IP address."
                    )
                    last_plug_alert = datetime.now()
            else:
                plug_fail_count = 0  # reset on any successful response

            # Use explicit identity checks (is True / is False) so a None return
            # from plug_state() (Tuya unreachable) never triggers a spurious toggle.
            # This is consistent with the calibration charging phase logic.
            sym = "⚡" if plugs is True else "🔌" if plugs is False else "❓"
            if cap <= low:
                if plugs is False or (plugs is None and bat["status"] not in ("Charging", "Full")):
                    plug_on(f"battery {cap}% ≤ LOW {low}% (safety force ON)")
                elif plugs is None:
                    log.warning(f"🔋 {cap}% ≤ LOW — Tuya unreachable, plug state unknown, skipping toggle")
                else:
                    log.info(f"🔋 {cap}% ({bat['status']}) | {bat['power_w']}W | Plug {sym}")
            elif cap >= high:
                if plugs is True:
                    plug_off(f"battery {cap}% ≥ HIGH {high}%")
                elif plugs is None:
                    log.warning(f"🔋 {cap}% ≥ HIGH — Tuya unreachable, plug state unknown, skipping toggle")
                else:
                    log.info(f"🔋 {cap}% ({bat['status']}) | {bat['power_w']}W | Plug {sym}")
            else:
                log.info(f"🔋 {cap}% ({bat['status']}) | {bat['power_w']}W | Plug {sym}")

            # Clock-aligned alert: fires at top of every Nth hour per ALERT_INTERVAL_HOURS.
            # e.g. interval=1 → every hour; interval=2 → every 2nd hour (12AM, 2AM, 4AM...)
            # Guard: elapsed must be within 90% of interval to handle polling jitter.
            now = datetime.now()
            interval_secs = CFG["ALERT_INTERVAL_HOURS"] * 3600
            elapsed_secs  = (now - last_alert).total_seconds()
            if now.minute == 0 and elapsed_secs >= interval_secs * 0.9:
                tg_send(status_msg())
                last_alert = now

            if _stop_requested.is_set():
                log.info("🛑 /stop received — daemon loop exiting cleanly.")
                tg_send("🛑 *Battery Manager stopped.*\nRestart with: `nohup python3 ~/battery_manager_git/battery_manager.py >> /data/battery_manager.log 2>&1 &`")
                raise _GracefulExit(0)

            time.sleep(CFG["POLL_INTERVAL"])

        except KeyboardInterrupt:
            log.info("🛑 Stopped by user")
            break
        except Exception as e:
            log.error(f"Daemon loop error: {e}")
            time.sleep(10)


# ══════════════════════════════════════════════════════════════════════════════
# TEST MODE
# ══════════════════════════════════════════════════════════════════════════════
def run_tests() -> None:
    PASS, FAIL = "✅ PASS", "❌ FAIL"
    results = []

    print("\n" + "=" * 60)
    print("  BATTERY MANAGER v3 — CONNECTION & FUNCTION TESTS")
    print("=" * 60)

    print("\n[TEST 1] Battery read")
    bat = battery()
    ok  = bat["cap"] > 0 and bat["status"] in ("Charging", "Discharging", "Full", "Not charging")
    r   = f"{PASS} — {bat['cap']}% | {bat['status']} | {bat['power_w']}W" if ok else f"{FAIL} — {bat}"
    print(f"  {r}")
    results.append(("Battery read", ok))

    print("\n[TEST 2] Smart plug OFF")
    ok  = plug_off("TEST")
    time.sleep(3)
    st  = plug_state()
    ok2 = ok and st is False
    r   = f"{PASS} — Confirmed OFF" if ok2 else f"{FAIL} — state={st}"
    print(f"  {r}")
    results.append(("Plug OFF", ok2))

    print("\n[TEST 3] Smart plug ON")
    ok  = plug_on("TEST")
    time.sleep(3)
    st  = plug_state()
    ok2 = ok and st is True
    r   = f"{PASS} — Confirmed ON" if ok2 else f"{FAIL} — state={st}"
    print(f"  {r}")
    results.append(("Plug ON", ok2))

    print("\n[TEST 4] Telegram alert")
    ok = tg_send(f"🧪 *TEST* — Battery Manager v3\n{status_msg()}")
    r  = f"{PASS} — Message sent" if ok else f"{FAIL} — send failed"
    print(f"  {r}")
    results.append(("Telegram", ok))

    print("\n[TEST 5] System stats")
    st = system_stats()
    ok = st["uptime"] != "N/A" and st["load"] != "N/A"
    r  = f"{PASS} — uptime={st['uptime']} load={st['load']}" if ok else f"{FAIL}"
    print(f"  {r}")
    results.append(("System stats", ok))

    print("\n" + "=" * 60)
    passed = sum(1 for _, v in results if v)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'}  {name}")
    print(f"\n  {passed}/{len(results)} tests passed")
    print("=" * 60 + "\n")

    tg_send(
        f"🧪 *Battery Manager v3 Test Results*\n"
        + "\n".join(f"{'✅' if v else '❌'} {n}" for n, v in results)
        + f"\n\n*{passed}/{len(results)} passed*"
    )


# ══════════════════════════════════════════════════════════════════════════════
# PID LOCK  — prevents duplicate instances (e.g. double @reboot)
# ══════════════════════════════════════════════════════════════════════════════
def acquire_pid_lock() -> bool:
    """Returns True if this process acquired the lock, False if another instance is running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if that PID is still alive (signal 0 = existence check only)
            os.kill(old_pid, 0)
            log.error(f"Another instance is already running (PID {old_pid}). Exiting.")
            return False
        except (ProcessLookupError, ValueError, PermissionError):
            # ProcessLookupError: PID does not exist — stale file, safe to overwrite
            # ValueError: corrupt PID value in file — safe to overwrite
            # PermissionError: PID exists but belongs to another user (OS PID reuse) —
            #                  not our process, treat as stale and overwrite
            log.warning("Stale PID file found (process gone or PID reused) — overwriting.")
    PID_FILE.write_text(str(os.getpid()))
    return True


def release_pid_lock() -> None:
    PID_FILE.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dell Server Battery Manager v3")
    parser.add_argument("--calibrate", nargs="?", const=3, type=int, metavar="CYCLES",
                        help="Run N-cycle calibration (default: 3)")
    parser.add_argument("--daemon",  action="store_true", help="Force daemon mode")
    parser.add_argument("--test",    action="store_true", help="Run connection tests")
    parser.add_argument("--status",  action="store_true", help="Print status and exit")
    args = parser.parse_args()

    if args.test:
        run_tests()
    elif args.status:
        print(status_msg().replace("*", ""))
    else:
        # ── PID lock: abort if already running ────────────────────────────────
        if not acquire_pid_lock():
            sys.exit(1)

        try:
            log.info("Testing Tuya plug connection...")
            if plug_state() is None:
                log.warning("Tuya plug is currently UNREACHABLE! Check IP, Wi-Fi, and router settings.")
                tg_send("⚠️ *Startup Warning*\nTuya smart plug is unreachable! Check network connection.")
            else:
                log.info("Tuya plug connection OK.")

            # Start telegram listener for all long-running modes
            threading.Thread(target=telegram_listener_thread, daemon=True).start()

            if args.calibrate is not None:
                run_calibration(args.calibrate)
            elif args.daemon:
                run_daemon()
            else:
                # Auto-detect: resume calibration if in progress, else daemon
                state = state_read()
                if state.get("mode") == "calibrating":
                    log.info("Calibration state found — resuming")
                    run_calibration()
                else:
                    run_daemon()
        finally:
            release_pid_lock()
