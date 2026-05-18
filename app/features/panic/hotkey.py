"""Global hotkey listener using pynput.keyboard.GlobalHotKeys.

Registers a system-wide key combo (default Ctrl+Alt+End).
Debounced to prevent accidental double-triggers.
Safe to call start() multiple times — stops previous listener first.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

from . import config_store

log = logging.getLogger(__name__)

_listener = None          # pynput.keyboard.GlobalHotKeys instance
_last_trigger_time: float = 0.0
_APP: "Application | None" = None


def start(app: "Application") -> bool:
    """Start the hotkey listener. Returns True if started successfully."""
    global _listener, _APP
    _APP = app
    stop()

    cfg = config_store.load().get("hotkey", {})
    if not cfg.get("enabled", False):
        log.debug("PANIC hotkey: disabled in config")
        return False

    combo = cfg.get("combo", "<ctrl>+<alt>+<end>")
    action = cfg.get("action", "level1")

    try:
        from pynput.keyboard import GlobalHotKeys
    except ImportError:
        log.warning("PANIC hotkey: pynput not available")
        return False

    def _on_hotkey():
        global _last_trigger_time
        debounce = config_store.load().get("hotkey", {}).get("debounce_seconds", 2)
        now = time.monotonic()
        if now - _last_trigger_time < debounce:
            return
        _last_trigger_time = now
        log.info("PANIC hotkey: triggered (%s) action=%s", combo, action)

        from . import state_machine as sm
        loop = sm.get_loop()
        if loop and loop.is_running():
            from . import escalation
            asyncio.run_coroutine_threadsafe(
                escalation.run_escalation(app, "manual", start_level=1),
                loop,
            )

    try:
        _listener = GlobalHotKeys({combo: _on_hotkey})
        _listener.daemon = True
        _listener.start()
        log.info("PANIC hotkey: registered %s", combo)
        return True
    except Exception as e:
        log.error("PANIC hotkey: failed to register %s — %s", combo, e)
        return False


def stop() -> None:
    global _listener
    if _listener is not None:
        try:
            _listener.stop()
        except Exception:
            pass
        _listener = None


def restart(app: "Application") -> bool:
    log.info("PANIC hotkey: restarting listener")
    return start(app)


def is_running() -> bool:
    return _listener is not None and getattr(_listener, "running", False)
