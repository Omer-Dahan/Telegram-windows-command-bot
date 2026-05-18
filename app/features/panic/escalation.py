"""Escalation runner: executes L1 → L2 → L3 sequentially with inter-level delays.

Each level runs its actions in safe order (alert first, network-disable last).
Transitions are validated by the state machine before each level executes.
Telegram notifications are sent for upcoming level transitions.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

from ...core.config import CONFIG
from . import config_store, forensics, logs, scoring, service, state_machine
from .state_machine import PanicState

log = logging.getLogger(__name__)

# Tracks the cancel event for inter-level waits (allows "Escalate Now" button)
_level_skip_events: dict[int, asyncio.Event] = {}


def get_skip_event(level: int) -> asyncio.Event:
    if level not in _level_skip_events:
        _level_skip_events[level] = asyncio.Event()
    return _level_skip_events[level]


def skip_to_level(level: int) -> None:
    """Called by 'Escalate Now' button — skips the inter-level delay."""
    ev = _level_skip_events.get(level)
    if ev:
        ev.set()


# ── Action lists per level ────────────────────────────────────────────────────

_LEVEL_ACTIONS: dict[int, list[str]] = {
    1: ["telegram_alert", "lock_workstation"],
    2: ["forensic_snapshot", "kill_processes", "veracrypt_dismount",
        "ram_clear", "browser_cleanup", "delete_temp", "custom_script"],
    3: ["disable_network", "hibernate", "shutdown"],
}

_LEVEL_STATES = {1: PanicState.LEVEL1, 2: PanicState.LEVEL2, 3: PanicState.LEVEL3}


# ── Main runner ───────────────────────────────────────────────────────────────

async def run_escalation(
    app: "Application",
    trigger: str,
    start_level: int = 1,
    force_max_level: int = 3,
) -> None:
    """Coroutine: execute escalation levels with inter-level delays."""
    cfg = config_store.load()
    esc = cfg.get("escalation", {})
    chat_ids = CONFIG.all_owner_chat_ids
    test_mode = cfg.get("test_mode", False)
    snapshot = service.get_system_snapshot()

    # Clear skip events for a fresh run
    for lvl in [1, 2, 3]:
        _level_skip_events[lvl] = asyncio.Event()

    levels_executed: list[int] = []
    actions_by_level: dict[str, dict] = {}
    forensic_zip: str | None = None

    for level in range(start_level, force_max_level + 1):
        target_state = _LEVEL_STATES[level]

        if not state_machine.can_transition(target_state):
            log.info("PANIC escalation: cannot transition to %s — stopping", target_state.value)
            break

        state_machine.transition(target_state, trigger, f"escalation_l{level}")

        # Execute this level's actions
        results = await _execute_level(app, level, trigger, cfg, snapshot, chat_ids, test_mode)
        actions_by_level[str(level)] = results
        levels_executed.append(level)

        # Capture forensic zip path if done in this level
        if "forensic_snapshot" in results and isinstance(results.get("forensic_snapshot"), str):
            forensic_zip = results["forensic_snapshot"]

        # Notify upcoming level and wait inter-level delay
        if level < force_max_level and level < 3:
            next_level = level + 1
            delay_key = f"l{level}_to_l{level+1}_delay_seconds"
            delay = esc.get(delay_key, 480 if level == 1 else 1320)

            if esc.get("notify_on_transition", True):
                m, s = divmod(delay, 60)
                time_str = f"{m}m" if m else f"{s}s"
                msg = (
                    f"⏳ Level {level} complete. "
                    f"Escalating to *Level {next_level}* in *{time_str}*.\n"
                    "Use /recover or press Escalate Now to skip."
                )
                for cid in chat_ids:
                    try:
                        await app.bot.send_message(cid, msg, parse_mode="Markdown")
                    except Exception:
                        pass

            # Wait for delay or skip event
            skip_ev = get_skip_event(next_level)
            try:
                await asyncio.wait_for(skip_ev.wait(), timeout=delay)
                log.info("PANIC: level %d→%d skipped (manual escalation)", level, next_level)
            except asyncio.TimeoutError:
                pass

            # Check if recovery happened during the wait
            if state_machine.get_state() in (PanicState.RECOVERY, PanicState.NORMAL,
                                              PanicState.SAFE_MODE):
                log.info("PANIC: escalation stopped (state changed during wait)")
                break

    # Persist event log
    state_before = PanicState.NORMAL.value if start_level == 1 else f"LEVEL{start_level-1}"
    state_after = f"LEVEL{max(levels_executed)}" if levels_executed else PanicState.NORMAL.value
    logs.append_event(
        trigger=trigger,
        levels_executed=levels_executed,
        actions_by_level=actions_by_level,
        score_at_trigger=scoring.get_total(),
        test_mode=test_mode,
        state_before=state_before,
        state_after=state_after,
        forensic_zip=forensic_zip,
    )

    # Transition to LOCKDOWN if L3 completed (unless already in recovery)
    if 3 in levels_executed:
        current = state_machine.get_state()
        if current == PanicState.LEVEL3:
            try:
                state_machine.transition(PanicState.LOCKDOWN, trigger, "l3_complete")
                for cid in chat_ids:
                    try:
                        await app.bot.send_message(
                            cid,
                            "🔒 *Device is now in LOCKDOWN.*\n"
                            "Send /recover to restore normal operation.",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
            except Exception:
                log.exception("PANIC: failed to transition to LOCKDOWN")


# ── Level action executor ─────────────────────────────────────────────────────

async def _execute_level(
    app: "Application",
    level: int,
    trigger: str,
    cfg: dict,
    snapshot: dict,
    chat_ids: set[int],
    test_mode: bool,
) -> dict[str, str]:
    """Execute all enabled actions for this level. Returns {action: result}."""
    actions_cfg = cfg.get("actions", {})
    level_key = f"level{level}"
    level_actions = actions_cfg.get(level_key, {})
    results: dict[str, str] = {}

    for action_name in _LEVEL_ACTIONS.get(level, []):
        action_cfg = level_actions.get(action_name, {})
        if not action_cfg.get("enabled", False):
            continue

        log.info("PANIC L%d: executing %r (test=%s)", level, action_name, test_mode)
        try:
            result = await _run_action(
                app, action_name, action_cfg, trigger, snapshot, chat_ids, test_mode
            )
        except Exception as e:
            log.exception("PANIC L%d: %r raised exception", level, action_name)
            result = f"exception: {e}"
        results[action_name] = result
        log.info("PANIC L%d: %r → %s", level, action_name, result)

    return results


async def _run_action(
    app: "Application",
    name: str,
    action_cfg: dict,
    trigger: str,
    snapshot: dict,
    chat_ids: set[int],
    test_mode: bool,
) -> str:
    if test_mode and name not in ("telegram_alert", "forensic_snapshot"):
        return f"[TEST] would execute {name}"

    if name == "telegram_alert":
        level = int(state_machine.get_state().value.replace("LEVEL", "") or "1")
        return await service.telegram_alert(
            app.bot, chat_ids, trigger, snapshot, test_mode, level=level
        )

    if name == "lock_workstation":
        return await asyncio.to_thread(service.lock_workstation)

    if name == "forensic_snapshot":
        zip_path = await forensics.capture_and_upload(app, chat_ids, trigger, test_mode)
        return str(zip_path) if zip_path else "failed"

    if name == "kill_processes":
        process_list = action_cfg.get("process_list", [])
        return await asyncio.to_thread(service.kill_processes, process_list)

    if name == "veracrypt_dismount":
        return await asyncio.to_thread(service.veracrypt_dismount)

    if name == "ram_clear":
        return await asyncio.to_thread(service.clear_ram)

    if name == "browser_cleanup":
        browsers = action_cfg.get("browsers", ["chrome", "edge", "firefox"])
        return await asyncio.to_thread(service.browser_cleanup, browsers)

    if name == "delete_temp":
        return await asyncio.to_thread(service.delete_temp_files)

    if name == "custom_script":
        script_path = action_cfg.get("script_path")
        return await asyncio.to_thread(service.run_custom_script, script_path)

    if name == "disable_network":
        # Alert must already have been sent (it's in L1, network disable is L3)
        return await asyncio.to_thread(service.disable_network_adapters)

    if name == "hibernate":
        from ..system import service as sys_svc
        return await asyncio.to_thread(sys_svc.hibernate_pc)

    if name == "shutdown":
        delay = action_cfg.get("delay_seconds", 30)
        return await service.shutdown_with_countdown(app.bot, chat_ids, delay)

    return f"unknown action: {name}"
