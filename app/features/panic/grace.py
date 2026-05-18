"""Grace period countdown system.

Each trigger can have its own grace period before escalation.
During grace, the owner receives a Telegram message with Cancel/Trigger Now/Ignore Once buttons.
The countdown message is edited every 30 s (single-message UX — no new messages).
Auto-cancels if the trigger condition resolves (score drops to 0).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

from . import config_store, scoring, state_machine
from .state_machine import PanicState

log = logging.getLogger(__name__)

# Per-trigger running tasks and control events
_tasks: dict[str, asyncio.Task] = {}
_cancel_events: dict[str, asyncio.Event] = {}
_force_events: dict[str, asyncio.Event] = {}
_ignore_until: dict[str, float] = {}   # monotonic deadline for ignore-once


def is_active(trigger: str) -> bool:
    task = _tasks.get(trigger)
    return task is not None and not task.done()


def is_ignored(trigger: str) -> bool:
    deadline = _ignore_until.get(trigger, 0.0)
    return time.monotonic() < deadline


def start(app: "Application", trigger: str, grace_seconds: int, chat_ids: set[int]) -> None:
    """Schedule the grace coroutine on the running event loop. Safe to call from threads."""
    if is_active(trigger):
        return
    cancel_ev = asyncio.Event()
    force_ev  = asyncio.Event()
    _cancel_events[trigger] = cancel_ev
    _force_events[trigger]  = force_ev

    coro = _grace_coroutine(app, trigger, grace_seconds, chat_ids, cancel_ev, force_ev)
    task = asyncio.ensure_future(coro)
    _tasks[trigger] = task
    log.info("PANIC grace: started for %r (%ds)", trigger, grace_seconds)


def cancel(trigger: str) -> None:
    ev = _cancel_events.get(trigger)
    if ev:
        ev.set()
        log.info("PANIC grace: cancelled for %r", trigger)


def force_trigger(trigger: str) -> None:
    ev = _force_events.get(trigger)
    if ev:
        ev.set()
        log.info("PANIC grace: force-triggered for %r", trigger)


def ignore_once(trigger: str, duration_hours: int = 1) -> None:
    cancel(trigger)
    _ignore_until[trigger] = time.monotonic() + duration_hours * 3600
    scoring.remove_trigger_score(trigger)
    log.info("PANIC grace: ignore-once %r for %dh", trigger, duration_hours)


# ── Grace coroutine ──────────────────────────────────────────────────────────

async def _grace_coroutine(
    app: "Application",
    trigger: str,
    grace_seconds: int,
    chat_ids: set[int],
    cancel_ev: asyncio.Event,
    force_ev: asyncio.Event,
) -> None:
    interval = config_store.get_grace_config().get("update_interval_seconds", 30)
    remaining = grace_seconds
    grace_start = time.monotonic()
    msg_ids: dict[int, int] = {}   # chat_id → message_id for edits

    # Send initial alert message to all owners
    for cid in chat_ids:
        try:
            from . import ui as ui_mod
            msg = await app.bot.send_message(
                cid,
                _grace_text(trigger, remaining),
                reply_markup=ui_mod.grace_keyboard(trigger),
                parse_mode="Markdown",
            )
            msg_ids[cid] = msg.message_id
        except Exception as e:
            log.warning("PANIC grace: failed to send alert to %d: %s", cid, e)

    while remaining > 0:
        wait = min(interval, remaining)
        try:
            # Wait for cancel or force events
            done, _ = await asyncio.wait(
                [
                    asyncio.ensure_future(cancel_ev.wait()),
                    asyncio.ensure_future(force_ev.wait()),
                    asyncio.ensure_future(asyncio.sleep(wait)),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception:
            break

        if cancel_ev.is_set():
            await _update_all(app, msg_ids, "✅ Panic grace period cancelled.", keyboard=None)
            _cleanup(trigger)
            try:
                state_machine.transition(PanicState.NORMAL, trigger, "grace_cancelled")
            except Exception:
                pass
            scoring.remove_trigger_score(trigger)
            log.info("PANIC grace: cancelled for %r", trigger)
            return

        if force_ev.is_set():
            await _update_all(app, msg_ids, "⚡ Panic triggered immediately.", keyboard=None)
            break

        # Auto-cancel if condition resolved
        if config_store.get_grace_config().get("auto_cancel_on_resolve", True):
            if scoring.get_trigger_score(trigger) <= 0:
                await _update_all(app, msg_ids, "✅ Threat resolved — grace cancelled.", keyboard=None)
                _cleanup(trigger)
                try:
                    state_machine.transition(PanicState.NORMAL, trigger, "condition_resolved")
                except Exception:
                    pass
                log.info("PANIC grace: auto-cancelled (condition resolved) for %r", trigger)
                return

        remaining -= wait
        if remaining > 0:
            from . import ui as ui_mod
            await _update_all(app, msg_ids, _grace_text(trigger, remaining),
                              keyboard=ui_mod.grace_keyboard(trigger))

    # Grace expired (or force) → escalate
    await _update_all(app, msg_ids, f"🚨 Grace expired — escalating for *{trigger}*…",
                      keyboard=None)
    _cleanup(trigger)

    from . import escalation
    await escalation.run_escalation(app, trigger, start_level=1)


def _grace_text(trigger: str, remaining: int) -> str:
    m, s = divmod(remaining, 60)
    time_str = f"{m}m {s}s" if m else f"{s}s"
    name = trigger.replace("_", " ").title()
    return (
        f"⚠️ *Trigger: {name}*\n\n"
        f"Panic mode activates in *{time_str}*.\n"
        "Use the buttons below to respond."
    )


async def _update_all(
    app: "Application",
    msg_ids: dict[int, int],
    text: str,
    keyboard,
) -> None:
    for cid, mid in msg_ids.items():
        try:
            await app.bot.edit_message_text(
                text, chat_id=cid, message_id=mid,
                reply_markup=keyboard, parse_mode="Markdown",
            )
        except Exception:
            pass


def _cleanup(trigger: str) -> None:
    _tasks.pop(trigger, None)
    _cancel_events.pop(trigger, None)
    _force_events.pop(trigger, None)
