"""Offline recovery: watch a folder + all removable drives for a recovery file.

Recovery file format (panic_recovery.json):
  {"pin": "1234", "timestamp": "2026-05-19T14:35:00"}

The file must be < 1 hour old (anti-replay). The PIN is verified via emergency_config.
On success: initiate recovery flow. File is deleted immediately after reading.
Rate limiting: inherits emergency_config's lockout logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from telegram.ext import Application

from ...core.config import CONFIG
from . import config_store, emergency_config, state_machine

log = logging.getLogger(__name__)

_stop_event: threading.Event | None = None
_thread: threading.Thread | None = None
_APP: "Application | None" = None


def start(app: "Application") -> None:
    global _stop_event, _thread, _APP
    _APP = app
    stop()

    cfg = config_store.load().get("offline_recovery", {})
    # Always start the watcher — it will only act when in LOCKDOWN
    stop_ev = threading.Event()
    _stop_event = stop_ev
    _thread = threading.Thread(
        target=_watch_loop, args=(app, stop_ev),
        name="panic_offline_recovery", daemon=True,
    )
    _thread.start()
    log.info("PANIC offline_recovery: watcher started")


def stop() -> None:
    global _stop_event, _thread
    if _stop_event:
        _stop_event.set()
    _stop_event = None
    _thread = None


def restart(app: "Application") -> None:
    log.info("PANIC offline_recovery: restarting watcher")
    start(app)


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


# ── Watcher loop ─────────────────────────────────────────────────────────────

def _watch_loop(app: "Application", stop_ev: threading.Event) -> None:
    while not stop_ev.wait(10):
        # Only act when in LOCKDOWN
        if state_machine.get_state() != state_machine.PanicState.LOCKDOWN:
            continue

        try:
            cfg = config_store.load().get("offline_recovery", {})
            filename = cfg.get("usb_token_filename", "panic_recovery.json")

            # Check configured watch directory
            watch_dir_str = cfg.get("watch_dir", "")
            if watch_dir_str:
                _check_path(app, Path(watch_dir_str) / filename)

            # Check all removable drives
            for part in psutil.disk_partitions(all=False):
                if "removable" in part.opts.lower() or "cdrom" in part.opts.lower():
                    _check_path(app, Path(part.mountpoint) / filename)

        except Exception:
            log.exception("PANIC offline_recovery: watcher error")


def _check_path(app: "Application", path: Path) -> None:
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return

    # Anti-replay: file must be < 1 hour old
    ts_str = data.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_str)
        if datetime.now() - ts > timedelta(hours=1):
            log.warning("PANIC offline_recovery: stale file at %s (ts=%s)", path, ts_str)
            return
    except ValueError:
        return

    pin = data.get("pin", "")
    if not pin:
        return

    log.info("PANIC offline_recovery: recovery file found at %s", path)

    if emergency_config.is_locked_out():
        log.warning("PANIC offline_recovery: locked out — ignoring recovery file")
        return

    # Delete the file immediately (before verification to avoid re-read race)
    try:
        path.unlink()
    except Exception:
        pass

    loop = state_machine.get_loop()
    if emergency_config.verify_pin(str(pin)):
        log.info("PANIC offline_recovery: PIN correct — initiating recovery")
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(_do_recovery(app), loop)
    else:
        log.warning("PANIC offline_recovery: incorrect PIN from %s", path)
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                _notify_failed_attempt(app),
                loop,
            )


async def _do_recovery(app: "Application") -> None:
    """Perform the recovery procedure (same as /recover command)."""
    from . import service
    from . import logs as panic_logs

    service.cancel_shutdown()

    adapters = panic_logs.load_disabled_adapters()
    results = await asyncio.to_thread(service.re_enable_adapters, adapters)
    panic_logs.clear_disabled_adapters()

    state_machine.force_set(state_machine.PanicState.NORMAL, "offline_pin_recovery")

    msg = "✅ *Offline recovery successful*\n" + "\n".join(results or ["(no adapters to restore)"])
    for cid in CONFIG.all_owner_chat_ids:
        try:
            await app.bot.send_message(cid, msg, parse_mode="Markdown")
        except Exception:
            pass


async def _notify_failed_attempt(app: "Application") -> None:
    for cid in CONFIG.all_owner_chat_ids:
        try:
            await app.bot.send_message(
                cid, "⚠️ Failed offline recovery attempt (wrong PIN)."
            )
        except Exception:
            pass
