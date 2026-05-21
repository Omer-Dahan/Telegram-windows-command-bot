"""Background trigger monitors — one daemon thread per enabled trigger.

Each monitor polls at its configured interval, contributes score via scoring.py,
and calls _dispatch_trigger() when threshold is reached.
_dispatch_trigger() is the single gate: validates state, starts grace or escalation.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
import urllib.request
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from telegram.ext import Application

from ...core.config import CONFIG
from . import config_store, grace, scoring, state_machine
from .state_machine import PanicState

log = logging.getLogger(__name__)

# Registry of running monitor threads
_threads: dict[str, threading.Thread] = {}
_stop_events: dict[str, threading.Event] = {}
_REGISTRY_LOCK = threading.Lock()

# Monitors that exited intentionally (e.g., no hardware found).
# Watchdog must NOT restart these automatically.
_GRACEFUL_EXITS: set[str] = set()

# Gate lock: prevents concurrent dispatch calls from racing into escalation
_GATE_LOCK = threading.Lock()

# Baseline values for comparison-based monitors
_baselines: dict[str, object] = {}


# ── Public API ───────────────────────────────────────────────────────────────

def start_all(app: "Application") -> None:
    """Stop any running monitors and restart all enabled ones."""
    stop_all()
    time.sleep(0.3)  # let threads exit cleanly

    cfg = config_store.load()
    if not cfg.get("enabled", False):
        log.info("PANIC monitors: module disabled — not starting")
        return

    triggers = cfg.get("triggers", {})
    starters = {
        "wifi_loss":        _monitor_wifi,
        "location_change":  _monitor_location,
        "usb_change":       _monitor_usb,
        "power_disconnect": _monitor_power,
        "bluetooth_loss":   _monitor_bluetooth,
        "dead_man_switch":  _monitor_dead_man,
        "lid_open":         _monitor_lid,
        "boot_source":      _monitor_boot,
    }

    with _REGISTRY_LOCK:
        for name, fn in starters.items():
            if triggers.get(name, {}).get("enabled", False):
                _start_monitor(app, name, fn)

    log.info("PANIC monitors: started %d thread(s)", len(_threads))


def stop_all() -> None:
    with _REGISTRY_LOCK:
        for ev in _stop_events.values():
            ev.set()
        _threads.clear()
        _stop_events.clear()
        _baselines.clear()
        _GRACEFUL_EXITS.clear()
    log.info("PANIC monitors: all stop events set")


def is_graceful_exit(name: str) -> bool:
    """Returns True if this monitor exited intentionally (no hardware, etc.)."""
    return name in _GRACEFUL_EXITS


def _mark_graceful_exit(name: str) -> None:
    _GRACEFUL_EXITS.add(name)
    log.info("PANIC monitors: %s marked as graceful exit (no hardware?)", name)


def restart_monitor(app: "Application", name: str) -> None:
    """Restart a single monitor thread (called by watchdog)."""
    starters = {
        "wifi_loss":        _monitor_wifi,
        "location_change":  _monitor_location,
        "usb_change":       _monitor_usb,
        "power_disconnect": _monitor_power,
        "bluetooth_loss":   _monitor_bluetooth,
        "dead_man_switch":  _monitor_dead_man,
        "lid_open":         _monitor_lid,
        "boot_source":      _monitor_boot,
    }
    fn = starters.get(name)
    if not fn:
        return
    with _REGISTRY_LOCK:
        ev = _stop_events.get(name)
        if ev:
            ev.set()
        _baselines.pop(name, None)
        _GRACEFUL_EXITS.discard(name)   # allow restart after manual trigger
        _start_monitor(app, name, fn)
    log.info("PANIC monitors: restarted %s", name)


def get_alive_status() -> dict[str, bool]:
    with _REGISTRY_LOCK:
        return {name: t.is_alive() for name, t in _threads.items()}


def _start_monitor(app: "Application", name: str, fn) -> None:
    stop_ev = threading.Event()
    _stop_events[name] = stop_ev
    t = threading.Thread(target=fn, args=(app, stop_ev), name=f"panic_{name}", daemon=True)
    _threads[name] = t
    t.start()


# ── Central dispatch gate ─────────────────────────────────────────────────────

def _dispatch_trigger(app: "Application", trigger: str) -> None:
    """Called from any monitor thread. Thread-safe. Bridges into async event loop."""
    new_score = scoring.add_trigger_score(trigger)
    threshold_status = scoring.get_threshold_status()
    log.info("PANIC dispatch: trigger=%r score=%.1f status=%s", trigger, new_score, threshold_status)

    with _GATE_LOCK:
        current = state_machine.get_state()
        if current in (PanicState.LOCKDOWN, PanicState.SAFE_MODE, PanicState.RECOVERY):
            log.debug("PANIC dispatch: blocked in state %s", current.value)
            return

        if threshold_status == "WARNING" and current == PanicState.NORMAL:
            if grace.is_active(trigger):
                return
            try:
                state_machine.transition(PanicState.WARNING, trigger, "score_threshold")
            except Exception:
                log.exception("PANIC dispatch: WARNING transition failed")
                return

            grace_s = config_store.get_trigger(trigger).get("grace_seconds", 300)
            chat_ids = CONFIG.all_owner_chat_ids
            loop = state_machine.get_loop()
            if loop:
                asyncio.run_coroutine_threadsafe(
                    grace.start(app, trigger, grace_s, chat_ids),
                    loop,
                )

        elif threshold_status in ("LEVEL1", "LEVEL2", "LEVEL3"):
            if state_machine.is_in_active_panic():
                return
            from . import escalation
            loop = state_machine.get_loop()
            if loop:
                asyncio.run_coroutine_threadsafe(
                    escalation.run_escalation(app, trigger),
                    loop,
                )


def _resolve_trigger(trigger: str) -> None:
    """Called when a monitor detects the condition has cleared."""
    new_score = scoring.remove_trigger_score(trigger)
    log.debug("PANIC resolve: trigger=%r new_total=%.1f", trigger, new_score)
    # Grace auto-cancel is handled inside grace.py (it watches scoring)


# ── Monitor implementations ───────────────────────────────────────────────────

def _ps(cmd: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            **config_store.subprocess_kwargs(),
        )
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return f"error: {e}"


# — Wi-Fi Loss —

def _wifi_has_adapter() -> bool:
    """Check whether a wireless adapter is present. Does NOT use returncode
    because Windows 11 returns exit code 1 even when an adapter exists
    (Location Permission restriction in newer builds)."""
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=8,
            **config_store.subprocess_kwargs(),
        ).stdout.lower()
        # Positive signal: at least one interface found
        if "there is 1 interface" in out or "there are" in out:
            return True
        # Negative signal: explicit no-adapter message
        if "there are 0 interface" in out or "no wireless interface" in out:
            return False
        # Fallback: check PowerShell for a physical Wi-Fi adapter
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-NetAdapter -Physical | Where-Object {$_.InterfaceDescription -match "
             "'Wi-Fi|Wireless|802\\.11|WLAN'} | Measure-Object | "
             "Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=8,
            **config_store.subprocess_kwargs(),
        )
        count_str = r.stdout.strip()
        return count_str.isdigit() and int(count_str) > 0
    except Exception:
        return False


def _get_wifi_profile() -> str | None:
    """Get the currently connected Wi-Fi profile name.

    Uses Get-NetConnectionProfile which works on Windows 11 without Location
    Permission (unlike netsh wlan which is blocked by WlanQueryInterface error 5).
    Excludes VPN/Tailscale adapters by requiring a physical Wi-Fi adapter.
    Returns the profile name (may differ slightly from SSID, e.g. 'Home 2' vs 'Home').
    """
    try:
        # Get all physical adapters that are Up and look like Wi-Fi
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$adapters = Get-NetAdapter -Physical | "
             "Where-Object {$_.Status -eq 'Up' -and "
             "($_.InterfaceDescription -match 'Wi-Fi|Wireless|802\\.11|WLAN')}; "
             "if ($adapters) { "
             "  ($adapters | ForEach-Object { "
             "    Get-NetConnectionProfile -InterfaceAlias $_.InterfaceAlias -ErrorAction SilentlyContinue"
             "  } | Select-Object -First 1).Name "
             "} else { '' }"],
            capture_output=True, text=True, timeout=10,
            **config_store.subprocess_kwargs(),
        )
        name = r.stdout.strip()
        if name and not name.startswith("error"):
            return name
    except Exception:
        pass

    # Fallback: netsh (may be empty on Win11 without Location)
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=8,
            **config_store.subprocess_kwargs(),
        ).stdout
        for line in out.splitlines():
            if "SSID" in line and "BSSID" not in line and ":" in line:
                val = line.split(":", 1)[-1].strip()
                if val:
                    return val
    except Exception:
        pass
    return None


def _wifi_ssid_matches_trusted(profile: str, trusted: list[str]) -> bool:
    """Case-insensitive match. Windows profile names may append numbers
    (e.g. 'MySSID 2') so we check substring containment both ways."""
    p = profile.lower()
    for ssid in trusted:
        s = ssid.lower()
        if s == p or s in p or p in s:
            return True
    return False


def _monitor_wifi(app: "Application", stop_ev: threading.Event) -> None:
    log.info("PANIC monitor_wifi: starting")

    # Adapter presence check — only parse output, never trust returncode
    if not _wifi_has_adapter():
        log.info("PANIC monitor_wifi: no wireless adapter found — exiting gracefully")
        _mark_graceful_exit("wifi_loss")
        return

    log.info("PANIC monitor_wifi: wireless adapter confirmed, polling every 30s")
    not_trusted_since: float | None = None

    while not stop_ev.wait(30):
        try:
            cfg = config_store.get_trigger("wifi_loss")
            if not cfg.get("enabled", False):
                continue

            profile = _get_wifi_profile()
            trusted: list[str] = cfg.get("trusted_ssids", [])

            if profile and _wifi_ssid_matches_trusted(profile, trusted):
                if not_trusted_since is not None:
                    log.debug("PANIC monitor_wifi: back on trusted network (%s)", profile)
                not_trusted_since = None
                _resolve_trigger("wifi_loss")
            else:
                if not_trusted_since is None:
                    not_trusted_since = time.monotonic()
                    label = profile if profile else "(disconnected)"
                    log.info("PANIC monitor_wifi: not on trusted network (%s), timer started", label)
                elapsed_min = (time.monotonic() - not_trusted_since) / 60
                timeout_min = cfg.get("timeout_minutes", 5)
                if elapsed_min >= timeout_min:
                    log.info("PANIC monitor_wifi: %.1fm on untrusted — dispatching", elapsed_min)
                    _dispatch_trigger(app, "wifi_loss")
        except Exception:
            log.exception("PANIC monitor_wifi: error")


# — Location Change —

def _monitor_location(app: "Application", stop_ev: threading.Event) -> None:
    log.info("PANIC monitor_location: starting")
    baseline_set = False
    baseline_ssids: set[str] = set()
    baseline_ip: str = ""

    while not stop_ev.wait(1):
        cfg = config_store.get_trigger("location_change")
        if not cfg.get("enabled", False):
            stop_ev.wait(60)
            continue

        poll = cfg.get("poll_interval_seconds", 60)
        stop_ev.wait(poll)
        try:
            # Gather visible SSID names
            out = subprocess.run(
                ["netsh", "wlan", "show", "networks"],
                capture_output=True, text=True, timeout=10,
                **config_store.subprocess_kwargs(),
            ).stdout
            current_ssids: set[str] = set()
            for line in out.splitlines():
                if line.strip().startswith("SSID") and "BSSID" not in line:
                    val = line.split(":", 1)[-1].strip()
                    if val:
                        current_ssids.add(val)

            # Public IP
            current_ip = ""
            if cfg.get("ip_change", True):
                try:
                    with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
                        current_ip = r.read().decode().strip()
                except Exception:
                    pass

            if not baseline_set:
                baseline_ssids = current_ssids
                baseline_ip = current_ip
                baseline_set = True
                continue

            threshold = cfg.get("ssid_change_threshold", 3)
            ssid_diff = len(current_ssids.symmetric_difference(baseline_ssids))
            ip_changed = cfg.get("ip_change", True) and current_ip and current_ip != baseline_ip

            if ssid_diff >= threshold or ip_changed:
                reason = f"ssid_diff={ssid_diff}" + (f" ip={current_ip}" if ip_changed else "")
                log.info("PANIC monitor_location: change detected (%s)", reason)
                _dispatch_trigger(app, "location_change")
                # Update baseline after trigger to avoid repeated firing
                baseline_ssids = current_ssids
                baseline_ip = current_ip
        except Exception:
            log.exception("PANIC monitor_location: error")


# — USB Change —

def _monitor_usb(app: "Application", stop_ev: threading.Event) -> None:
    log.info("PANIC monitor_usb: starting")
    baseline: set[str] | None = None

    while not stop_ev.wait(10):
        try:
            cfg = config_store.get_trigger("usb_change")
            if not cfg.get("enabled", False):
                continue

            out = _ps(
                "Get-PnpDevice -Class USB -PresentOnly "
                "| Select-Object -ExpandProperty InstanceId",
                timeout=10,
            )
            if out.startswith("error:"):
                log.debug("PANIC monitor_usb: skipping iteration (PS error: %s)", out)
                continue
            current = {line.strip() for line in out.splitlines() if line.strip()}

            if baseline is None:
                baseline = current
                continue

            trusted = set(cfg.get("trusted_device_ids", []))
            new_devices = current - baseline
            removed = baseline - current

            triggered = False
            if cfg.get("alert_on_new", True) and new_devices:
                unknown_new = new_devices - trusted
                if unknown_new:
                    log.info("PANIC monitor_usb: new unknown USB %s", unknown_new)
                    triggered = True

            if cfg.get("alert_on_remove_trusted", True) and removed:
                removed_trusted = removed & trusted
                if removed_trusted:
                    log.info("PANIC monitor_usb: trusted USB removed %s", removed_trusted)
                    triggered = True

            if triggered:
                _dispatch_trigger(app, "usb_change")

            baseline = current
        except Exception:
            log.exception("PANIC monitor_usb: error")


# — Power Disconnect —

def _monitor_power(app: "Application", stop_ev: threading.Event) -> None:
    log.info("PANIC monitor_power: starting")
    bat = psutil.sensors_battery()
    if bat is None:
        log.info("PANIC monitor_power: no battery (desktop?) — exiting")
        return

    was_plugged = bat.power_plugged

    while not stop_ev.wait(15):
        try:
            cfg = config_store.get_trigger("power_disconnect")
            if not cfg.get("enabled", False):
                continue

            bat = psutil.sensors_battery()
            if bat is None:
                continue

            if was_plugged and not bat.power_plugged:
                log.info("PANIC monitor_power: charger disconnected")
                _dispatch_trigger(app, "power_disconnect")
            elif not was_plugged and bat.power_plugged:
                _resolve_trigger("power_disconnect")

            was_plugged = bat.power_plugged
        except Exception:
            log.exception("PANIC monitor_power: error")


# — Bluetooth Presence Loss —

def _monitor_bluetooth(app: "Application", stop_ev: threading.Event) -> None:
    log.info("PANIC monitor_bluetooth: starting")
    absent_since: dict[str, float] = {}

    while not stop_ev.wait(20):
        try:
            cfg = config_store.get_trigger("bluetooth_loss")
            if not cfg.get("enabled", False):
                continue

            trusted: list[str] = cfg.get("trusted_devices", [])
            if not trusted:
                continue

            out = _ps(
                "Get-PnpDevice -Class Bluetooth "
                "| Where-Object {$_.Status -eq 'OK'} "
                "| Select-Object -ExpandProperty FriendlyName",
                timeout=10,
            )
            present = {line.strip().lower() for line in out.splitlines() if line.strip()}

            for device in trusted:
                dev_lower = device.lower()
                if dev_lower in present:
                    absent_since.pop(device, None)
                    _resolve_trigger("bluetooth_loss")
                else:
                    if device not in absent_since:
                        absent_since[device] = time.monotonic()
                    elapsed_min = (time.monotonic() - absent_since[device]) / 60
                    timeout_min = cfg.get("timeout_minutes", 3)
                    if elapsed_min >= timeout_min:
                        log.info("PANIC monitor_bluetooth: %r absent for %.1fm", device, elapsed_min)
                        _dispatch_trigger(app, "bluetooth_loss")
        except Exception:
            log.exception("PANIC monitor_bluetooth: error")


# — Dead-Man Switch —

def _monitor_dead_man(app: "Application", stop_ev: threading.Event) -> None:
    log.info("PANIC monitor_dead_man: starting")

    while not stop_ev.wait(300):   # check every 5 min
        try:
            cfg = config_store.get_trigger("dead_man_switch")
            if not cfg.get("enabled", False):
                continue

            from . import logs as panic_logs
            last_hb = panic_logs.get_last_heartbeat()
            if last_hb is None:
                continue   # Never had a heartbeat — don't trigger on fresh install

            import datetime
            age_hours = (datetime.datetime.now() - last_hb).total_seconds() / 3600
            timeout_hours = cfg.get("timeout_hours", 24)
            if age_hours >= timeout_hours:
                log.info("PANIC monitor_dead_man: no heartbeat for %.1fh", age_hours)
                _dispatch_trigger(app, "dead_man_switch")
        except Exception:
            log.exception("PANIC monitor_dead_man: error")


# — Lid Open —

def _monitor_lid(app: "Application", stop_ev: threading.Event) -> None:
    log.info("PANIC monitor_lid: starting")
    last_count: int | None = None

    while not stop_ev.wait(5):
        try:
            cfg = config_store.get_trigger("lid_open")
            if not cfg.get("enabled", False):
                continue

            # Use display count as proxy for lid state:
            #   lid opens  → more displays  (count increases)
            #   lid closes → fewer displays (count decreases)
            try:
                import ctypes
                count = ctypes.windll.user32.GetSystemMetrics(80)  # SM_CMONITORS
            except Exception:
                count = 0

            if last_count is None:
                last_count = count
                continue

            mode = cfg.get("detect_mode", "open")
            if mode == "open" and count > last_count:
                log.info("PANIC monitor_lid: lid opened (%d→%d displays)", last_count, count)
                _dispatch_trigger(app, "lid_open")
            elif mode == "close" and count < last_count:
                log.info("PANIC monitor_lid: lid closed (%d→%d displays)", last_count, count)
                _dispatch_trigger(app, "lid_open")

            last_count = count
        except Exception:
            log.exception("PANIC monitor_lid: error")


# — Boot Source (one-shot) —

def _monitor_boot(app: "Application", stop_ev: threading.Event) -> None:
    log.info("PANIC monitor_boot: starting (one-shot)")
    try:
        cfg = config_store.get_trigger("boot_source")
        if not cfg.get("enabled", False):
            return

        # Check last boot event
        out = _ps(
            "try { "
            "(Get-WinEvent -LogName System -FilterXPath "
            "\"*[System[EventID=12]]\" -MaxEvents 1 -ErrorAction Stop).Message"
            " } catch { '' }",
            timeout=15,
        )
        suspicious_keywords = ["pxe", "network boot", "usb boot", "external"]
        if any(kw in out.lower() for kw in suspicious_keywords):
            log.warning("PANIC monitor_boot: suspicious boot source detected: %s", out[:100])
            _dispatch_trigger(app, "boot_source")
        else:
            log.info("PANIC monitor_boot: normal boot source — OK")
    except Exception:
        log.exception("PANIC monitor_boot: error")


# — Failed Login (registered as job_queue, not a thread) —

async def check_failed_logins(context) -> None:
    """Called by PTB job_queue every 60 s. Async — runs on the event loop."""
    try:
        cfg = config_store.get_trigger("failed_login")
        if not cfg.get("enabled", False):
            return
        threshold = cfg.get("threshold", 5)
        window = cfg.get("window_minutes", 10)
        out = _ps(
            f"try {{"
            f"(Get-WinEvent -FilterHashtable @{{LogName='Security';Id=4625;"
            f"StartTime=(Get-Date).AddMinutes(-{window})}} "
            f"-ErrorAction Stop | Measure-Object).Count"
            f"}} catch {{ 0 }}",
            timeout=15,
        )
        count = int(out.strip()) if out.strip().isdigit() else 0
        if count >= threshold:
            log.info("PANIC check_failed_logins: %d failures in %dm", count, window)
            _dispatch_trigger(context.application, "failed_login")
    except Exception:
        log.exception("PANIC check_failed_logins: error")
