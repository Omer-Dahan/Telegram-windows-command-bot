"""Event log CRUD + analytics for the panic module.

All reads/writes go through here. Wraps data/panic_logs.json atomically.
Tracks: panic events, heartbeat, disabled adapters, score history, safe-mode counter.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

from ...core.config import DATA_DIR
from ...shared.atomic_json import read_json, write_json

log = logging.getLogger(__name__)

_PATH = DATA_DIR / "panic_logs.json"
_LOCK = threading.RLock()

MAX_EVENTS = 500
MAX_SCORE_HISTORY = 100

_DEFAULT: dict = {
    "events": [],
    "disabled_adapters": [],
    "last_triggered": None,
    "last_heartbeat": None,
    "score_history": [],
    "panic_count_last_hour": 0,
    "false_positive_count": 0,
    "safe_mode_entries": 0,
}

# Lazy reference to app (set from handlers) for safe-mode auto-notification
_APP: "Application | None" = None


def set_app(app: "Application") -> None:
    global _APP
    _APP = app


def _load() -> dict:
    data = read_json(_PATH, {})
    merged = dict(_DEFAULT)
    merged.update(data)
    return merged


def _save(data: dict) -> None:
    write_json(_PATH, data)


# ── Events ──────────────────────────────────────────────────────────────────

def append_event(
    trigger: str,
    levels_executed: list[int],
    actions_by_level: dict,
    score_at_trigger: float = 0.0,
    test_mode: bool = False,
    grace_duration_seconds: int = 0,
    grace_cancelled: bool = False,
    state_before: str = "NORMAL",
    state_after: str = "LEVEL1",
    forensic_zip: str | None = None,
) -> str:
    """Append a panic event. Returns the event ID."""
    event_id = str(uuid.uuid4())[:8]
    event = {
        "id": event_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "trigger": trigger,
        "state_before": state_before,
        "state_after": state_after,
        "score_at_trigger": round(score_at_trigger, 1),
        "grace_duration_seconds": grace_duration_seconds,
        "grace_cancelled": grace_cancelled,
        "test_mode": test_mode,
        "levels_executed": levels_executed,
        "actions_by_level": actions_by_level,
        "forensic_zip": forensic_zip,
    }
    with _LOCK:
        data = _load()
        events: list = data.setdefault("events", [])
        events.insert(0, event)                    # newest first
        if len(events) > MAX_EVENTS:
            events[:] = events[:MAX_EVENTS]
        data["last_triggered"] = event["timestamp"]
        _save(data)
        log.info("PANIC log: event %s trigger=%r levels=%s test=%s",
                 event_id, trigger, levels_executed, test_mode)

    if not test_mode:
        _check_safe_mode_threshold()

    return event_id


def get_events(page: int = 0, page_size: int = 10) -> list[dict]:
    """Return one page of events (newest first)."""
    events = _load().get("events", [])
    start = page * page_size
    return events[start: start + page_size]


def get_event_count() -> int:
    return len(_load().get("events", []))


def clear_events() -> None:
    with _LOCK:
        data = _load()
        data["events"] = []
        data["panic_count_last_hour"] = 0
        _save(data)


def mark_false_positive() -> None:
    with _LOCK:
        data = _load()
        data["false_positive_count"] = data.get("false_positive_count", 0) + 1
        _save(data)


# ── Heartbeat ────────────────────────────────────────────────────────────────

def touch_heartbeat() -> None:
    with _LOCK:
        data = _load()
        data["last_heartbeat"] = datetime.now().isoformat(timespec="seconds")
        _save(data)


def get_last_heartbeat() -> datetime | None:
    raw = _load().get("last_heartbeat")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def get_last_triggered() -> datetime | None:
    raw = _load().get("last_triggered")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# ── Disabled adapters (for recovery) ────────────────────────────────────────

def save_disabled_adapters(adapters: list[str]) -> None:
    with _LOCK:
        data = _load()
        data["disabled_adapters"] = list(adapters)
        _save(data)


def load_disabled_adapters() -> list[str]:
    return list(_load().get("disabled_adapters", []))


def clear_disabled_adapters() -> None:
    with _LOCK:
        data = _load()
        data["disabled_adapters"] = []
        _save(data)


# ── Score history ────────────────────────────────────────────────────────────

def append_score_snapshot(score: float, active_triggers: list[str]) -> None:
    with _LOCK:
        data = _load()
        history: list = data.setdefault("score_history", [])
        history.insert(0, {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "score": round(score, 1),
            "triggers_active": active_triggers,
        })
        if len(history) > MAX_SCORE_HISTORY:
            history[:] = history[:MAX_SCORE_HISTORY]
        _save(data)


def get_score_history(limit: int = 20) -> list[dict]:
    return _load().get("score_history", [])[:limit]


# ── Analytics ────────────────────────────────────────────────────────────────

def get_analytics() -> dict:
    events = _load().get("events", [])
    by_trigger: dict[str, int] = {}
    by_level: dict[str, int] = {}
    real_events = [e for e in events if not e.get("test_mode")]
    for e in real_events:
        t = e.get("trigger", "unknown")
        by_trigger[t] = by_trigger.get(t, 0) + 1
        for lvl in e.get("levels_executed", []):
            k = f"L{lvl}"
            by_level[k] = by_level.get(k, 0) + 1
    data = _load()
    return {
        "total_events": len(real_events),
        "test_events": len(events) - len(real_events),
        "by_trigger": by_trigger,
        "by_level": by_level,
        "false_positives": data.get("false_positive_count", 0),
        "safe_mode_entries": data.get("safe_mode_entries", 0),
    }


# ── Safe-mode auto-detection ─────────────────────────────────────────────────

def _check_safe_mode_threshold() -> None:
    """Auto-enter safe mode if too many panics fired in a short window."""
    from . import config_store, state_machine
    cfg = config_store.load().get("safe_mode", {})
    threshold = cfg.get("auto_enter_threshold", 5)
    window_min = cfg.get("auto_enter_window_minutes", 60)
    cutoff = datetime.now() - timedelta(minutes=window_min)

    events = _load().get("events", [])
    recent = [
        e for e in events
        if not e.get("test_mode")
        and datetime.fromisoformat(e["timestamp"]) >= cutoff
    ]
    if len(recent) < threshold:
        return

    current = state_machine.get_state()
    from .state_machine import PanicState
    if current in (PanicState.SAFE_MODE, PanicState.LOCKDOWN, PanicState.RECOVERY):
        return

    log.warning("PANIC: safe-mode threshold reached (%d panics in %dm)", len(recent), window_min)
    try:
        state_machine.force_set(PanicState.SAFE_MODE, "auto: panic loop detected")
    except Exception:
        log.exception("PANIC: failed to enter safe mode")

    with _LOCK:
        data = _load()
        data["safe_mode_entries"] = data.get("safe_mode_entries", 0) + 1
        _save(data)

    if _APP:
        import asyncio
        from ...core.config import CONFIG
        from . import state_machine as _sm
        loop = _sm.get_loop()
        msg = (
            "⚠️ *Safe Mode activated*\n"
            f"{len(recent)} panics in {window_min} min detected.\n"
            "Destructive actions *DISABLED*.\n"
            "Send /safemode\\_off to exit."
        )
        if loop and loop.is_running():
            for cid in CONFIG.all_owner_chat_ids:
                asyncio.run_coroutine_threadsafe(
                    _APP.bot.send_message(cid, msg, parse_mode="Markdown"),
                    loop,
                )
