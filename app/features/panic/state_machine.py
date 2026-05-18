"""Formal FSM for the panic module.

States: NORMAL → WARNING → LEVEL1 → LEVEL2 → LEVEL3 → LOCKDOWN → RECOVERY → SAFE_MODE
All transitions are explicit; anything not in the whitelist raises IllegalTransitionError.
State is persisted to data/panic_state.json so LOCKDOWN survives a bot restart.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

from ...core.config import DATA_DIR
from ...shared.atomic_json import read_json, write_json

log = logging.getLogger(__name__)

_STATE_PATH = DATA_DIR / "panic_state.json"
_LOCK = threading.RLock()

# Reference to the running Application — set in handlers.register()
_APP: "Application | None" = None

# Event loop reference — set from the first async handler invocation.
# PTB v21 removed Application.loop; we capture it ourselves.
_EVENT_LOOP: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _EVENT_LOOP
    _EVENT_LOOP = loop


def get_loop() -> asyncio.AbstractEventLoop | None:
    return _EVENT_LOOP


class PanicState(str, Enum):
    NORMAL    = "NORMAL"
    WARNING   = "WARNING"
    LEVEL1    = "LEVEL1"
    LEVEL2    = "LEVEL2"
    LEVEL3    = "LEVEL3"
    LOCKDOWN  = "LOCKDOWN"
    RECOVERY  = "RECOVERY"
    SAFE_MODE = "SAFE_MODE"


class IllegalTransitionError(Exception):
    pass


# Explicit whitelist of valid (from, to) transitions
_ALLOWED: set[tuple[PanicState, PanicState]] = {
    (PanicState.NORMAL,    PanicState.WARNING),
    (PanicState.NORMAL,    PanicState.LEVEL1),
    (PanicState.NORMAL,    PanicState.SAFE_MODE),
    (PanicState.WARNING,   PanicState.NORMAL),
    (PanicState.WARNING,   PanicState.LEVEL1),
    (PanicState.LEVEL1,    PanicState.LEVEL2),
    (PanicState.LEVEL1,    PanicState.LOCKDOWN),   # if L2/L3 disabled, L1 → LOCKDOWN
    (PanicState.LEVEL2,    PanicState.LEVEL3),
    (PanicState.LEVEL2,    PanicState.LOCKDOWN),   # if L3 disabled
    (PanicState.LEVEL3,    PanicState.LOCKDOWN),
    (PanicState.LOCKDOWN,  PanicState.RECOVERY),
    (PanicState.RECOVERY,  PanicState.NORMAL),
    (PanicState.SAFE_MODE, PanicState.NORMAL),
    # Allow re-entry into same state without error (idempotent)
    (PanicState.NORMAL,    PanicState.NORMAL),
}

_current: PanicState = PanicState.NORMAL
_state_meta: dict = {}


def set_app(app: "Application") -> None:
    global _APP
    _APP = app


def get_state() -> PanicState:
    with _LOCK:
        return _current


def get_meta() -> dict:
    with _LOCK:
        return dict(_state_meta)


def can_transition(to: PanicState) -> bool:
    with _LOCK:
        return (_current, to) in _ALLOWED


def is_in_active_panic() -> bool:
    """True if currently executing a panic level (not safe to start a new one)."""
    with _LOCK:
        return _current in (
            PanicState.LEVEL1, PanicState.LEVEL2, PanicState.LEVEL3,
            PanicState.LOCKDOWN, PanicState.RECOVERY,
        )


def transition(to: PanicState, trigger: str = "", reason: str = "") -> None:
    """Validate and execute a state transition. Persists LOCKDOWN immediately."""
    global _current, _state_meta
    with _LOCK:
        frm = _current
        if (frm, to) not in _ALLOWED:
            raise IllegalTransitionError(
                f"Transition {frm.value} → {to.value} is not allowed "
                f"(trigger={trigger!r}, reason={reason!r})"
            )
        _current = to
        _state_meta = {
            "state": to.value,
            "from": frm.value,
            "trigger": trigger,
            "reason": reason,
            "entered_at": datetime.now().isoformat(timespec="seconds"),
        }
        log.info("FSM %s → %s  trigger=%r reason=%r", frm.value, to.value, trigger, reason)

        if to == PanicState.LOCKDOWN:
            _persist_lockdown()
        elif to == PanicState.NORMAL:
            _clear_lockdown()


def _persist_lockdown() -> None:
    saved = read_json(_STATE_PATH, {})
    saved.update(_state_meta)
    saved["startup_lock"] = True
    write_json(_STATE_PATH, saved)


def _clear_lockdown() -> None:
    saved = read_json(_STATE_PATH, {})
    saved["state"] = PanicState.NORMAL.value
    saved["startup_lock"] = False
    saved["disabled_adapters"] = []
    write_json(_STATE_PATH, saved)


def save_disabled_adapters(adapters: list[str]) -> None:
    """Persist adapter names so /recover can re-enable them after a reboot."""
    with _LOCK:
        saved = read_json(_STATE_PATH, {})
        saved["disabled_adapters"] = adapters
        write_json(_STATE_PATH, saved)


def load_disabled_adapters() -> list[str]:
    return read_json(_STATE_PATH, {}).get("disabled_adapters", [])


def load_persisted_state() -> PanicState:
    """Called on startup. Returns persisted state; sets in-memory state accordingly."""
    global _current, _state_meta
    saved = read_json(_STATE_PATH, {})
    raw = saved.get("state", PanicState.NORMAL.value)
    try:
        state = PanicState(raw)
    except ValueError:
        state = PanicState.NORMAL
    with _LOCK:
        _current = state
        _state_meta = saved
    log.info("FSM loaded persisted state: %s", state.value)
    return state


def force_set(state: PanicState, reason: str = "force") -> None:
    """Bypass transition table — only for recovery and startup resets."""
    global _current, _state_meta
    with _LOCK:
        log.warning("FSM force-set to %s (reason=%r)", state.value, reason)
        _current = state
        _state_meta = {"state": state.value, "reason": reason,
                       "entered_at": datetime.now().isoformat(timespec="seconds")}
