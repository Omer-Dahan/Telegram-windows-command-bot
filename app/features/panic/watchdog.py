"""Watchdog: monitors all panic subsystem components and auto-restarts on failure.

Runs as a single daemon thread. Checks every watchdog.check_interval_seconds.
On failure: restarts the component and notifies all owners via Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

from ...core.config import CONFIG
from . import config_store, state_machine

log = logging.getLogger(__name__)

_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None
_APP: "Application | None" = None

# Component health state: name → {alive, last_ok, restarts, last_error}
_health: dict[str, dict] = {}
_HEALTH_LOCK = threading.Lock()


def start(app: "Application") -> None:
    global _stop_event, _thread, _APP
    _APP = app
    stop()

    cfg = config_store.load().get("watchdog", {})
    if not cfg.get("enabled", True):
        log.info("PANIC watchdog: disabled in config")
        return

    stop_ev = threading.Event()
    _stop_event = stop_ev
    _thread = threading.Thread(
        target=_watchdog_loop, args=(app, stop_ev),
        name="panic_watchdog", daemon=True,
    )
    _thread.start()
    log.info("PANIC watchdog: started")


def stop() -> None:
    global _stop_event, _thread
    if _stop_event:
        _stop_event.set()
    _stop_event = None
    _thread = None


def restart_component(app: "Application", name: str) -> bool:
    """Manually restart a named component (called from UI)."""
    return _try_restart(app, name)


def get_health() -> dict[str, dict]:
    with _HEALTH_LOCK:
        return dict(_health)


def get_health_summary() -> str:
    """One-line summary for the main panel."""
    health = get_health()
    if not health:
        return "💚 OK"
    dead = [n for n, s in health.items() if not s.get("alive", True)]
    if dead:
        return f"🔴 {len(dead)} dead: {', '.join(dead)}"
    return "💚 All OK"


# ── Watchdog loop ─────────────────────────────────────────────────────────────

def _watchdog_loop(app: "Application", stop_ev: threading.Event) -> None:
    cfg = config_store.load().get("watchdog", {})
    interval = cfg.get("check_interval_seconds", 30)

    while not stop_ev.wait(interval):
        try:
            _check_all(app)
        except Exception:
            log.exception("PANIC watchdog: check_all error")


def _check_all(app: "Application") -> None:
    from . import monitors, hotkey, offline_recovery

    now_str = datetime.now().isoformat(timespec="seconds")
    cfg = config_store.load().get("watchdog", {})
    auto_restart = cfg.get("restart_on_failure", True)
    notify = cfg.get("notify_on_restart", True)

    # — Monitor threads —
    for name, alive in monitors.get_alive_status().items():
        _update(name, alive, now_str)
        if not alive:
            if monitors.is_graceful_exit(name):
                # Thread exited intentionally (e.g. no hardware) — don't restart
                _update(name + "_status", True, now_str)  # not an error
                continue
            if auto_restart:
                if _try_restart(app, name):
                    if notify:
                        _send_notify(app, f"🔧 Watchdog restarted: *{name}* (was dead)")

    # — Hotkey listener —
    hk_alive = hotkey.is_running()
    _update("hotkey_listener", hk_alive, now_str)
    if not hk_alive and config_store.load().get("hotkey", {}).get("enabled", False):
        if auto_restart and hotkey.restart(app):
            if notify:
                _send_notify(app, "🔧 Watchdog restarted: *hotkey_listener*")

    # — Offline recovery watcher —
    or_alive = offline_recovery.is_running()
    _update("offline_recovery", or_alive, now_str)
    if not or_alive and auto_restart:
        offline_recovery.restart(app)
        if notify:
            _send_notify(app, "🔧 Watchdog restarted: *offline_recovery*")

    # — PowerShell responsiveness —
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "1"],
            capture_output=True, timeout=8,
            **config_store.subprocess_kwargs(),
        )
        ps_ok = r.returncode == 0
    except Exception:
        ps_ok = False
    _update("powershell", ps_ok, now_str)

    # — Config integrity —
    try:
        cfg_data = config_store.load()
        config_ok = isinstance(cfg_data, dict) and "triggers" in cfg_data
    except Exception:
        config_ok = False
    _update("config_integrity", config_ok, now_str)

    # — Event loop —
    loop = state_machine.get_loop()
    loop_ok = loop is not None and loop.is_running()
    _update("event_loop", loop_ok, now_str)

    # — Telegram connection (non-blocking check via future) —
    _check_telegram_async(app, now_str)


def _update(name: str, alive: bool, ts: str) -> None:
    with _HEALTH_LOCK:
        entry = _health.setdefault(name, {"alive": True, "last_ok": ts, "restarts": 0})
        entry["alive"] = alive
        if alive:
            entry["last_ok"] = ts
        entry["last_check"] = ts


def _try_restart(app: "Application", name: str) -> bool:
    from . import monitors, hotkey, offline_recovery
    try:
        if name == "hotkey_listener":
            hotkey.restart(app)
        elif name == "offline_recovery":
            offline_recovery.restart(app)
        elif name.startswith("monitor_") or name in monitors.get_alive_status():
            monitors.restart_monitor(app, name)
        else:
            return False
        with _HEALTH_LOCK:
            _health.setdefault(name, {})["restarts"] = (
                _health[name].get("restarts", 0) + 1
            )
        log.info("PANIC watchdog: restarted %s", name)
        return True
    except Exception:
        log.exception("PANIC watchdog: failed to restart %s", name)
        return False


def _check_telegram_async(app: "Application", ts: str) -> None:
    async def _check():
        try:
            await app.bot.get_me()
            _update("telegram_conn", True, ts)
        except Exception:
            _update("telegram_conn", False, ts)

    loop = state_machine.get_loop()
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(_check(), loop)


def _send_notify(app: "Application", msg: str) -> None:
    loop = state_machine.get_loop()
    if not loop or not loop.is_running():
        return

    async def _send():
        for cid in CONFIG.all_owner_chat_ids:
            try:
                await app.bot.send_message(cid, msg, parse_mode="Markdown")
            except Exception:
                pass
    asyncio.run_coroutine_threadsafe(_send(), loop)
