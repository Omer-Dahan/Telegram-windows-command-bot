"""Single reader/writer for data/panic_config.json.

All other modules import from here — they never call read_json/write_json directly.
Config is HMAC-SHA256 signed. On signature mismatch → load .bak → load _DEFAULT.
deep_merge ensures new config keys always appear with defaults even for old saves.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import subprocess
import threading
from copy import deepcopy
from pathlib import Path

from ...core.config import DATA_DIR
from ...shared.atomic_json import read_json, write_json

log = logging.getLogger(__name__)

_PATH = DATA_DIR / "panic_config.json"
_BAK  = DATA_DIR / "panic_config.json.bak"
_LOCK = threading.RLock()

_DEFAULT: dict = {
    "enabled": False,
    "silent_mode": False,
    "test_mode": False,
    "cooldown_seconds": 300,
    "config_hmac": "",

    "scoring": {
        "enabled": True,
        "decay_per_minute": 5,
        "threshold_warning": 30,
        "threshold_level1": 60,
        "threshold_level2": 80,
        "threshold_level3": 100,
        "trigger_weights": {
            "wifi_loss": 20, "location_change": 40, "usb_change": 30,
            "power_disconnect": 15, "bluetooth_loss": 20, "dead_man_switch": 50,
            "failed_login": 35, "lid_open": 10, "boot_source": 60, "manual": 100,
        },
    },

    "grace_period": {
        "default_seconds": 300,
        "update_interval_seconds": 30,
        "auto_cancel_on_resolve": True,
    },

    "escalation": {
        "l1_to_l2_delay_seconds": 480,
        "l2_to_l3_delay_seconds": 1320,
        "notify_on_transition": True,
        "allow_manual_escalate": True,
    },

    "triggers": {
        "wifi_loss":        {"enabled": False, "timeout_minutes": 5,  "trusted_ssids": [],        "grace_seconds": 300},
        "location_change":  {"enabled": False, "ssid_change_threshold": 3, "ip_change": True, "poll_interval_seconds": 60, "grace_seconds": 120},
        "usb_change":       {"enabled": False, "trusted_device_ids": [], "alert_on_new": True, "alert_on_remove_trusted": True, "grace_seconds": 60},
        "power_disconnect": {"enabled": False, "grace_seconds": 60},
        "bluetooth_loss":   {"enabled": False, "timeout_minutes": 3,  "trusted_devices": [],      "grace_seconds": 180},
        "dead_man_switch":  {"enabled": False, "timeout_hours": 24,   "grace_seconds": 0},
        "failed_login":     {"enabled": False, "threshold": 5,        "window_minutes": 10,       "grace_seconds": 0},
        "lid_open":         {"enabled": False, "grace_seconds": 30, "detect_mode": "open"},
        "boot_source":      {"enabled": False, "grace_seconds": 0},
        "manual":           {"enabled": True,  "grace_seconds": 0},
    },

    "actions": {
        "level1": {
            "telegram_alert":   {"enabled": True, "include_screenshot": True, "include_webcam": False},
            "lock_workstation": {"enabled": True},
        },
        "level2": {
            "kill_processes":     {"enabled": False, "process_list": ["chrome.exe", "firefox.exe", "keepass.exe"]},
            "veracrypt_dismount": {"enabled": False},
            "ram_clear":          {"enabled": False},
            "browser_cleanup":    {"enabled": False, "browsers": ["chrome", "edge", "firefox"]},
            "delete_temp":        {"enabled": False},
            "custom_script":      {"enabled": False, "script_path": None},
            "forensic_snapshot":  {"enabled": True},
        },
        "level3": {
            "disable_network": {"enabled": False},
            "hibernate":       {"enabled": False},
            "shutdown":        {"enabled": False, "delay_seconds": 30},
        },
    },

    "hotkey": {
        "enabled": False,
        "combo": "<ctrl>+<alt>+<end>",
        "action": "level1",
        "require_confirm": False,
        "debounce_seconds": 2,
    },

    "offline_recovery": {
        "watch_dir": "",
        "usb_token_filename": "panic_recovery.json",
        "failed_attempt_lockout_count": 5,
        "lockout_duration_minutes": 30,
    },

    "watchdog": {
        "enabled": True,
        "check_interval_seconds": 30,
        "restart_on_failure": True,
        "notify_on_restart": True,
    },

    "safe_mode": {
        "auto_enter_threshold": 5,
        "auto_enter_window_minutes": 60,
    },

    "forensics": {
        "include_webcam": False,
        "include_processes": True,
        "include_screenshot": True,
        "upload_to_telegram": True,
        "keep_local_copies": 20,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: base keys filled in where override is missing (recursive)."""
    result = deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = deepcopy(v)
    return result


def _signing_key() -> str:
    from . import emergency_config  # late import avoids circular deps at module level
    return emergency_config.get_signing_key()


def _compute_hmac(data: dict) -> str:
    key = _signing_key()
    if not key:
        return ""
    payload = {k: v for k, v in data.items() if k != "config_hmac"}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hmac.new(bytes.fromhex(key), raw, hashlib.sha256).hexdigest()


def _verify_hmac(data: dict) -> bool:
    key = _signing_key()
    if not key:
        return True  # no key yet (first run) — skip verification
    stored = data.get("config_hmac", "")
    expected = _compute_hmac(data)
    if not stored:
        return True  # first save after key generation
    return hmac.compare_digest(stored, expected)


def load() -> dict:
    """Load, deep-merge over defaults, verify HMAC. Falls back to .bak then _DEFAULT."""
    with _LOCK:
        raw = read_json(_PATH, {})
        if raw and not _verify_hmac(raw):
            log.error("PANIC: Config HMAC mismatch — loading backup")
            raw = read_json(_BAK, {})
            if raw and not _verify_hmac(raw):
                log.error("PANIC: Backup config also invalid — using defaults")
                raw = {}
        return _deep_merge(_DEFAULT, raw)


def save(cfg: dict) -> None:
    """Compute HMAC, write atomic, then write .bak. Never raises."""
    with _LOCK:
        try:
            cfg["config_hmac"] = _compute_hmac(cfg)
            if _PATH.exists():
                import shutil
                shutil.copy2(_PATH, _BAK)
            write_json(_PATH, cfg)
        except Exception:
            log.exception("PANIC: Failed to save config")


def is_enabled() -> bool:
    return bool(load().get("enabled"))


def is_test_mode() -> bool:
    return bool(load().get("test_mode"))


def is_silent_mode() -> bool:
    return bool(load().get("silent_mode"))


def subprocess_kwargs() -> dict:
    """Add CREATE_NO_WINDOW in silent mode so no cmd windows flash."""
    if is_silent_mode():
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def get_trigger(name: str) -> dict:
    return load().get("triggers", {}).get(name, {})


def set_trigger_field(name: str, key: str, value) -> None:
    with _LOCK:
        cfg = load()
        cfg.setdefault("triggers", {}).setdefault(name, {})[key] = value
        save(cfg)


def get_action(level: str, name: str) -> dict:
    return load().get("actions", {}).get(level, {}).get(name, {})


def set_action_field(level: str, name: str, key: str, value) -> None:
    with _LOCK:
        cfg = load()
        cfg.setdefault("actions", {}).setdefault(level, {}).setdefault(name, {})[key] = value
        save(cfg)


def get_score_config() -> dict:
    return load().get("scoring", {})


def get_grace_config() -> dict:
    return load().get("grace_period", {})


def get_escalation_config() -> dict:
    return load().get("escalation", {})
