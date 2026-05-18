"""Telegram glue for the panic module.

register(app) wires up:
  - /panic, /recover, /safemode_off commands
  - panic: callback namespace
  - group=-2 heartbeat interceptor (dead-man switch)
  - group=-1 text/document interceptor (multi-step input: add SSID, set PIN, etc.)
  - PTB job_queue for failed-login polling
  - All background subsystems (monitors, watchdog, hotkey, offline_recovery)

Navigation: ALL submenu transitions use q.edit_message_text() — never reply_text().
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ...core.auth import is_owner_msg, owner_only
from ...core.config import CONFIG, DATA_DIR
from ...core.menu import PANIC
from ...core.types import TextResult
from . import (
    config_store,
    emergency_config,
    escalation,
    forensics,
    grace,
    hotkey,
    logs,
    monitors,
    offline_recovery,
    scoring,
    state_machine,
    ui,
    watchdog,
)
from .state_machine import PanicState

log = logging.getLogger(__name__)

# Per-chat awaiting-input state: chat_id → what we're waiting for
_AWAITING: dict[int, str] = {}

# Script file extensions allowed
_SCRIPT_EXTS = {".ps1", ".cmd", ".bat"}


# ── Safe edit helper (never raises on "not modified") ────────────────────────

async def _safe_edit(q, text: str, markup=None, parse_mode: str | None = "Markdown") -> None:
    try:
        await q.edit_message_text(text, reply_markup=markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("PANIC edit failed: %s", e)


# ── Registration ──────────────────────────────────────────────────────────────

def register(app: Application) -> None:
    # Startup initialization
    emergency_config.initialize_if_missing()
    state_machine.set_app(app)
    logs.set_app(app)

    # Handle LOCKDOWN state that survived a reboot
    persisted = state_machine.load_persisted_state()
    if persisted == PanicState.LOCKDOWN:
        asyncio.ensure_future(_reapply_lockdown(app))

    # Commands
    app.add_handler(CommandHandler("panic",       _cmd_panic),       group=0)
    app.add_handler(CommandHandler("recover",     _cmd_recover),     group=0)
    app.add_handler(CommandHandler("safemode_off",_cmd_safemode_off),group=0)

    # Callback namespace
    app.add_handler(CallbackQueryHandler(_on_callback, pattern=r"^panic:"), group=0)

    # Heartbeat interceptor — fires before everything for all owner messages
    app.add_handler(MessageHandler(filters.ALL, _heartbeat_interceptor), group=-2)
    app.add_handler(CallbackQueryHandler(_heartbeat_interceptor_cb, pattern=r".*"), group=-2)

    # Multi-step text input interceptor (add SSID, set PIN, etc.)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text_input), group=-1)

    # Document upload (custom script)
    app.add_handler(MessageHandler(filters.Document.ALL, _on_script_upload), group=-1)

    # Failed-login polling via job_queue
    app.job_queue.run_repeating(monitors.check_failed_logins, interval=60, first=10)

    # Background subsystems
    monitors.start_all(app)
    hotkey.start(app)
    offline_recovery.start(app)
    watchdog.start(app)

    log.info("PANIC: feature registered")


def match_text(text: str, chat_id: int) -> TextResult | None:
    if text.strip() == PANIC:
        cfg = config_store.load()
        state = state_machine.get_state()
        status_line = _status_line(cfg, state)
        return TextResult(
            text=status_line,
            reply_markup=ui.main_menu(cfg),
            parse_mode="Markdown",
        )
    return None


def _status_line(cfg: dict, state: PanicState) -> str:
    enabled = cfg.get("enabled", False)
    score = round(scoring.get_total())
    return (
        f"🚨 *Panic Mode* — {'✅ Armed' if enabled else '❌ Disarmed'}\n"
        f"State: `{state.value}` | Score: `{score}/100`"
    )


# ── Heartbeat interceptors (group=-2) ─────────────────────────────────────────

async def _heartbeat_interceptor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Capture the running event loop on first call (PTB v21 has no public app.loop)
    if state_machine.get_loop() is None:
        import asyncio
        state_machine.set_loop(asyncio.get_running_loop())
    if is_owner_msg(update):
        logs.touch_heartbeat()


async def _heartbeat_interceptor_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if state_machine.get_loop() is None:
        import asyncio
        state_machine.set_loop(asyncio.get_running_loop())
    if is_owner_msg(update):
        logs.touch_heartbeat()


# ── Commands ──────────────────────────────────────────────────────────────────

@owner_only
async def _cmd_panic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⚡ Manual panic trigger sent.")
    monitors._dispatch_trigger(context.application, "manual")


@owner_only
async def _cmd_recover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_recovery(context.application, update.effective_chat.id)


@owner_only
async def _cmd_safemode_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state_machine.force_set(PanicState.NORMAL, "owner: /safemode_off")
    scoring.reset()
    await update.message.reply_text("✅ Safe Mode deactivated. Monitoring resumed.")


# ── Callback handler ──────────────────────────────────────────────────────────

async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    parts = data.split(":")
    # parts[0] == "panic" always

    section = parts[1] if len(parts) > 1 else ""
    app = context.application
    chat_id = update.effective_chat.id

    # ── Main menu controls ────────────────────────────────────────────────
    if section == "menu" or data == "panic:menu":
        cfg = config_store.load()
        state = state_machine.get_state()
        await _safe_edit(q, _status_line(cfg, state), ui.main_menu(cfg))

    elif section == "refresh":
        # Re-render whatever the current state implies = go to main menu
        cfg = config_store.load()
        state = state_machine.get_state()
        await _safe_edit(q, _status_line(cfg, state), ui.main_menu(cfg))

    elif section == "arm":
        config_store.set_trigger_field("manual", "enabled", True)
        cfg = config_store.load()
        cfg["enabled"] = True
        config_store.save(cfg)
        monitors.start_all(app)
        await _safe_edit(q, "✅ Panic Mode *armed*.", ui.main_menu(config_store.load()))

    elif section == "disarm":
        cfg = config_store.load()
        cfg["enabled"] = False
        config_store.save(cfg)
        monitors.stop_all()
        await _safe_edit(q, "🛡️ Panic Mode *disarmed*.", ui.main_menu(config_store.load()))

    elif section == "testmode":
        on = parts[2] == "on"
        cfg = config_store.load()
        cfg["test_mode"] = on
        config_store.save(cfg)
        await _safe_edit(q, f"🧪 Test mode {'ON' if on else 'OFF'}.", ui.main_menu(config_store.load()))

    elif section == "silent":
        on = parts[2] == "on"
        cfg = config_store.load()
        cfg["silent_mode"] = on
        config_store.save(cfg)
        await _safe_edit(q, f"🔇 Silent mode {'ON' if on else 'OFF'}.", ui.main_menu(config_store.load()))

    elif section == "manual":
        monitors._dispatch_trigger(app, "manual")
        await q.answer("⚡ Manual trigger dispatched!", show_alert=True)
        cfg = config_store.load()
        state = state_machine.get_state()
        await _safe_edit(q, _status_line(cfg, state), ui.main_menu(cfg))

    # ── Score / State / Health panels ─────────────────────────────────────
    elif section == "score":
        text, kb = ui.score_panel()
        await _safe_edit(q, text, kb)

    elif section == "state":
        text, kb = ui.state_panel()
        await _safe_edit(q, text, kb)

    elif section == "health":
        text, kb = ui.health_panel()
        await _safe_edit(q, text, kb)

    # ── Triggers ──────────────────────────────────────────────────────────
    elif section == "trg":
        sub = parts[2] if len(parts) > 2 else ""

        if sub == "menu":
            text, kb = ui.triggers_menu()
            await _safe_edit(q, text, kb)

        elif sub == "cfg":
            name = parts[3] if len(parts) > 3 else ""
            text, kb = ui.trigger_cfg_menu(name)
            await _safe_edit(q, text, kb)

        elif sub == "toggle":
            name = parts[3] if len(parts) > 3 else ""
            t_cfg = config_store.get_trigger(name)
            new_val = not t_cfg.get("enabled", False)
            config_store.set_trigger_field(name, "enabled", new_val)
            monitors.start_all(app)   # restarts with new config
            text, kb = ui.trigger_cfg_menu(name)
            await _safe_edit(q, text, kb)

        elif sub == "grace":
            name = parts[3] if len(parts) > 3 else ""
            secs = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 300
            config_store.set_trigger_field(name, "grace_seconds", secs)
            text, kb = ui.trigger_cfg_menu(name)
            await _safe_edit(q, text, kb)

        elif sub == "timeout":
            name = parts[3] if len(parts) > 3 else ""
            mins = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 5
            config_store.set_trigger_field(name, "timeout_minutes", mins)
            text, kb = ui.trigger_cfg_menu(name)
            await _safe_edit(q, text, kb)

        elif sub == "add_ssid":
            _AWAITING[chat_id] = "ssid"
            await _safe_edit(q, "📶 Type the SSID name to add as trusted network:")

        elif sub == "add_cur_ssid":
            ssid = await asyncio.to_thread(_get_current_ssid)
            if ssid:
                t_cfg = config_store.get_trigger("wifi_loss")
                ssids = list(t_cfg.get("trusted_ssids", []))
                if ssid not in ssids:
                    ssids.append(ssid)
                    config_store.set_trigger_field("wifi_loss", "trusted_ssids", ssids)
                await q.answer(f"✅ Added: {ssid}", show_alert=True)
            else:
                await q.answer("❌ No Wi-Fi connected", show_alert=True)
            text, kb = ui.trigger_cfg_menu("wifi_loss")
            await _safe_edit(q, text, kb)

        elif sub == "del_ssid":
            idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            t_cfg = config_store.get_trigger("wifi_loss")
            ssids = list(t_cfg.get("trusted_ssids", []))
            if 0 <= idx < len(ssids):
                removed = ssids.pop(idx)
                config_store.set_trigger_field("wifi_loss", "trusted_ssids", ssids)
                await q.answer(f"Removed: {removed}")
            text, kb = ui.trigger_cfg_menu("wifi_loss")
            await _safe_edit(q, text, kb)

        elif sub == "add_bt":
            _AWAITING[chat_id] = "bt_device"
            await _safe_edit(q, "📱 Type the Bluetooth device name to whitelist:")

        elif sub == "del_bt":
            idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            t_cfg = config_store.get_trigger("bluetooth_loss")
            devs = list(t_cfg.get("trusted_devices", []))
            if 0 <= idx < len(devs):
                removed = devs.pop(idx)
                config_store.set_trigger_field("bluetooth_loss", "trusted_devices", devs)
                await q.answer(f"Removed: {removed}")
            text, kb = ui.trigger_cfg_menu("bluetooth_loss")
            await _safe_edit(q, text, kb)

        elif sub == "add_usb":
            usb_list = await asyncio.to_thread(_get_usb_devices)
            if usb_list:
                t_cfg = config_store.get_trigger("usb_change")
                existing = list(t_cfg.get("trusted_device_ids", []))
                for uid in usb_list:
                    if uid not in existing:
                        existing.append(uid)
                config_store.set_trigger_field("usb_change", "trusted_device_ids", existing)
                await q.answer(f"✅ Added {len(usb_list)} USB device(s)", show_alert=True)
            else:
                await q.answer("No USB devices found", show_alert=True)
            text, kb = ui.trigger_cfg_menu("usb_change")
            await _safe_edit(q, text, kb)

        elif sub == "del_usb":
            idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            t_cfg = config_store.get_trigger("usb_change")
            uids = list(t_cfg.get("trusted_device_ids", []))
            if 0 <= idx < len(uids):
                uids.pop(idx)
                config_store.set_trigger_field("usb_change", "trusted_device_ids", uids)
                await q.answer("Removed")
            text, kb = ui.trigger_cfg_menu("usb_change")
            await _safe_edit(q, text, kb)

        elif sub == "test":
            name = parts[3] if len(parts) > 3 else "manual"
            from . import service as panic_service
            snapshot = await asyncio.to_thread(panic_service.get_system_snapshot)
            asyncio.ensure_future(
                panic_service.telegram_alert(
                    app.bot, CONFIG.all_owner_chat_ids, name, snapshot, test_mode=True
                )
            )
            await q.answer(f"🧪 Test alert sent for {name}", show_alert=True)

    # ── Actions ───────────────────────────────────────────────────────────
    elif section == "act":
        sub = parts[2] if len(parts) > 2 else ""

        if sub == "menu":
            text, kb = ui.actions_menu("level1")
            await _safe_edit(q, text, kb)

        elif sub == "lvl":
            level_key = parts[3] if len(parts) > 3 else "level1"
            text, kb = ui.actions_menu(level_key)
            await _safe_edit(q, text, kb)

        elif sub == "toggle":
            level_key = parts[3] if len(parts) > 3 else "level1"
            action_name = parts[4] if len(parts) > 4 else ""
            a_cfg = config_store.get_action(level_key, action_name)
            config_store.set_action_field(level_key, action_name, "enabled",
                                          not a_cfg.get("enabled", False))
            text, kb = ui.actions_menu(level_key)
            await _safe_edit(q, text, kb)

        elif sub == "cfg":
            level_key = parts[3] if len(parts) > 3 else "level1"
            action_name = parts[4] if len(parts) > 4 else ""
            from telegram import InlineKeyboardButton as IB, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[IB("⬅️ Back", callback_data="panic:act:menu"),
                                        IB("🏠 Home", callback_data="panic:menu")]])
            await _safe_edit(q,
                f"⚙️ *{action_name}* config\n_(individual action config available after trigger setup)_",
                kb)

    # ── Escalation ────────────────────────────────────────────────────────
    elif section == "esc":
        sub = parts[2] if len(parts) > 2 else ""

        if sub == "menu":
            text, kb = ui.escalation_menu()
            await _safe_edit(q, text, kb)

        elif sub == "delay":
            key_part = parts[3] if len(parts) > 3 else "l12"
            secs = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 480
            cfg = config_store.load()
            field = "l1_to_l2_delay_seconds" if key_part == "l12" else "l2_to_l3_delay_seconds"
            cfg.setdefault("escalation", {})[field] = secs
            config_store.save(cfg)
            text, kb = ui.escalation_menu()
            await _safe_edit(q, text, kb)

        elif sub == "now":
            level = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 2
            escalation.skip_to_level(level)
            await q.answer(f"⚡ Escalating to Level {level}…", show_alert=True)

        elif sub == "notify":
            on = parts[3] == "on" if len(parts) > 3 else True
            cfg = config_store.load()
            cfg.setdefault("escalation", {})["notify_on_transition"] = on
            config_store.save(cfg)
            text, kb = ui.escalation_menu()
            await _safe_edit(q, text, kb)

        elif sub == "manual":
            on = parts[3] == "on" if len(parts) > 3 else True
            cfg = config_store.load()
            cfg.setdefault("escalation", {})["allow_manual_escalate"] = on
            config_store.save(cfg)
            text, kb = ui.escalation_menu()
            await _safe_edit(q, text, kb)

    # ── Grace period responses ────────────────────────────────────────────
    elif section == "grace":
        sub = parts[2] if len(parts) > 2 else ""
        trigger = parts[3] if len(parts) > 3 else ""

        if sub == "cancel":
            grace.cancel(trigger)
            await q.answer("✅ Panic cancelled.", show_alert=True)
        elif sub == "now":
            grace.force_trigger(trigger)
            await q.answer("⚡ Triggering now…", show_alert=True)
        elif sub == "ignore":
            grace.ignore_once(trigger)
            await q.answer("🙈 Ignored for 1 hour.", show_alert=True)

    # ── Logs ──────────────────────────────────────────────────────────────
    elif section == "logs":
        sub = parts[2] if len(parts) > 2 else "0"

        if sub == "clear" and len(parts) > 3 and parts[3] == "confirm":
            logs.clear_events()
            text, kb = ui.logs_panel(0)
            await _safe_edit(q, text, kb)
        elif sub == "clear":
            text, kb = ui.logs_clear_confirm()
            await _safe_edit(q, text, kb)
        elif sub == "analytics":
            analytics = logs.get_analytics()
            lines = ["📊 *Panic Analytics*\n",
                     f"Total events: {analytics['total_events']}",
                     f"Test events: {analytics['test_events']}",
                     f"False positives: {analytics['false_positives']}",
                     f"Safe mode entries: {analytics['safe_mode_entries']}",
                     "\nBy trigger:"]
            for t, n in analytics.get("by_trigger", {}).items():
                lines.append(f"  {t}: {n}")
            lines.append("\nBy level:")
            for lvl, n in analytics.get("by_level", {}).items():
                lines.append(f"  {lvl}: {n}")
            await _safe_edit(q, "\n".join(lines),
                             ui._mk(ui._nav("panic:logs:0")))
        else:
            page = int(sub) if sub.isdigit() else 0
            text, kb = ui.logs_panel(page)
            await _safe_edit(q, text, kb)

    # ── Forensics ─────────────────────────────────────────────────────────
    elif section == "forensics":
        sub = parts[2] if len(parts) > 2 else "list"

        if sub == "list":
            text, kb = ui.forensics_panel()
            await _safe_edit(q, text, kb)
        elif sub == "send":
            idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            snaps = forensics.list_snapshots()
            if 0 <= idx < len(snaps):
                path = snaps[idx]
                await q.answer("📤 Uploading…")
                with open(path, "rb") as f:
                    await app.bot.send_document(
                        chat_id, f, caption=f"🔬 {path.name}"
                    )
            else:
                await q.answer("Not found", show_alert=True)

    # ── Hotkey config ─────────────────────────────────────────────────────
    elif section == "hotkey":
        sub = parts[2] if len(parts) > 2 else ""

        if sub in ("cfg", ""):
            text, kb = ui.hotkey_menu()
            await _safe_edit(q, text, kb)
        elif sub == "on":
            cfg = config_store.load()
            cfg.setdefault("hotkey", {})["enabled"] = True
            config_store.save(cfg)
            hotkey.restart(app)
            text, kb = ui.hotkey_menu()
            await _safe_edit(q, text, kb)
        elif sub == "off":
            cfg = config_store.load()
            cfg.setdefault("hotkey", {})["enabled"] = False
            config_store.save(cfg)
            hotkey.stop()
            text, kb = ui.hotkey_menu()
            await _safe_edit(q, text, kb)
        elif sub == "set_combo":
            _AWAITING[chat_id] = "hotkey_combo"
            await _safe_edit(q, "⌨️ Type the key combo (e.g. `<ctrl>+<alt>+<end>`):")
        elif sub == "test":
            await q.answer("⌨️ Press your hotkey now to test it.", show_alert=True)

    # ── Offline recovery ──────────────────────────────────────────────────
    elif section == "offline":
        sub = parts[2] if len(parts) > 2 else "cfg"

        if sub == "cfg":
            text, kb = ui.offline_recovery_menu()
            await _safe_edit(q, text, kb)
        elif sub == "setpin":
            _AWAITING[chat_id] = "recovery_pin"
            await _safe_edit(q, "🔑 Type your recovery PIN (min 4 digits). It will be hashed immediately.")
        elif sub == "setdir":
            _AWAITING[chat_id] = "watch_dir"
            await _safe_edit(q, "📁 Type the folder path to watch for recovery files:")

    # ── Watchdog ──────────────────────────────────────────────────────────
    elif section == "watchdog":
        sub = parts[2] if len(parts) > 2 else "menu"

        if sub == "menu":
            text, kb = ui.health_panel()
            await _safe_edit(q, text, kb)
        elif sub == "restart":
            component = parts[3] if len(parts) > 3 else ""
            if component:
                watchdog.restart_component(app, component)
                await q.answer(f"🔄 Restarted {component}", show_alert=True)
            text, kb = ui.health_panel()
            await _safe_edit(q, text, kb)
        elif sub == "restart_all":
            monitors.start_all(app)
            hotkey.restart(app)
            await q.answer("🔄 All monitors restarted", show_alert=True)
            text, kb = ui.health_panel()
            await _safe_edit(q, text, kb)
        elif sub in ("on", "off"):
            on = sub == "on"
            cfg = config_store.load()
            cfg.setdefault("watchdog", {})["enabled"] = on
            config_store.save(cfg)
            if on:
                watchdog.start(app)
            else:
                watchdog.stop()
            text, kb = ui.settings_menu()
            await _safe_edit(q, text, kb)

    # ── Settings ──────────────────────────────────────────────────────────
    elif section == "settings":
        text, kb = ui.settings_menu()
        await _safe_edit(q, text, kb)

    elif section == "cooldown":
        secs = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 300
        cfg = config_store.load()
        cfg["cooldown_seconds"] = secs
        config_store.save(cfg)
        text, kb = ui.settings_menu()
        await _safe_edit(q, text, kb)

    # ── Safe mode ─────────────────────────────────────────────────────────
    elif section == "safe":
        if parts[2] == "off":
            state_machine.force_set(PanicState.NORMAL, "owner: safe_off")
            scoring.reset()
            monitors.start_all(app)
            cfg = config_store.load()
            state = state_machine.get_state()
            await _safe_edit(q, "✅ Safe Mode deactivated.", ui.main_menu(cfg))

    # ── Recovery ──────────────────────────────────────────────────────────
    elif section == "recover":
        if len(parts) == 2:
            text, kb = ui.recover_confirm()
            await _safe_edit(q, text, kb)
        elif len(parts) > 2 and parts[2] == "confirm":
            await _do_recovery(app, chat_id)
            cfg = config_store.load()
            state = state_machine.get_state()
            await _safe_edit(q, "✅ Recovery complete.", ui.main_menu(cfg))


# ── Multi-step text input (group=-1) ─────────────────────────────────────────

async def _on_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner_msg(update):
        return
    chat_id = update.effective_chat.id
    awaiting = _AWAITING.get(chat_id)
    if not awaiting:
        return

    text = (update.message.text or "").strip()
    _AWAITING.pop(chat_id, None)

    if awaiting == "ssid":
        t_cfg = config_store.get_trigger("wifi_loss")
        ssids = list(t_cfg.get("trusted_ssids", []))
        if text not in ssids:
            ssids.append(text)
            config_store.set_trigger_field("wifi_loss", "trusted_ssids", ssids)
        await update.message.reply_text(f"✅ SSID '{text}' added to trusted list.")

    elif awaiting == "bt_device":
        t_cfg = config_store.get_trigger("bluetooth_loss")
        devs = list(t_cfg.get("trusted_devices", []))
        if text not in devs:
            devs.append(text)
            config_store.set_trigger_field("bluetooth_loss", "trusted_devices", devs)
        await update.message.reply_text(f"✅ BT device '{text}' added to trusted list.")

    elif awaiting == "recovery_pin":
        if len(text) < 4:
            await update.message.reply_text("❌ PIN must be at least 4 characters.")
            return
        await asyncio.to_thread(emergency_config.set_pin, text)
        await update.message.reply_text("✅ Recovery PIN set successfully.")

    elif awaiting == "watch_dir":
        cfg = config_store.load()
        cfg.setdefault("offline_recovery", {})["watch_dir"] = text
        config_store.save(cfg)
        offline_recovery.restart(context.application)
        await update.message.reply_text(f"✅ Watch dir set to: {text}")

    elif awaiting == "hotkey_combo":
        cfg = config_store.load()
        cfg.setdefault("hotkey", {})["combo"] = text
        config_store.save(cfg)
        hotkey.restart(context.application)
        await update.message.reply_text(f"✅ Hotkey combo set to: {text}")


# ── Script upload (group=-1) ──────────────────────────────────────────────────

async def _on_script_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner_msg(update):
        return
    chat_id = update.effective_chat.id
    if _AWAITING.get(chat_id) != "script":
        return

    doc = update.message.document
    if not doc:
        return

    ext = Path(doc.file_name or "").suffix.lower()
    if ext not in _SCRIPT_EXTS:
        await update.message.reply_text(f"❌ Unsupported extension. Use: {', '.join(_SCRIPT_EXTS)}")
        return

    dest = DATA_DIR / f"panic_custom_script{ext}"
    file = await doc.get_file()
    await file.download_to_drive(str(dest))

    config_store.set_action_field("level2", "custom_script", "script_path", str(dest))
    _AWAITING.pop(chat_id, None)
    await update.message.reply_text(f"✅ Script saved: {dest.name}")


# ── Recovery procedure ────────────────────────────────────────────────────────

async def _do_recovery(app: Application, notify_chat_id: int | None = None) -> None:
    from . import service

    service.cancel_shutdown()

    adapters = logs.load_disabled_adapters()
    results = await asyncio.to_thread(service.re_enable_adapters, adapters)
    logs.clear_disabled_adapters()

    state_machine.force_set(PanicState.NORMAL, "owner: /recover")
    scoring.reset()
    monitors.start_all(app)

    msg = "✅ *Recovery complete*\n" + "\n".join(results or ["(no adapters to restore)"])
    targets = set(CONFIG.all_owner_chat_ids)
    if notify_chat_id:
        targets.add(notify_chat_id)
    for cid in targets:
        try:
            await app.bot.send_message(cid, msg, parse_mode="Markdown")
        except Exception:
            pass


async def _reapply_lockdown(app: Application) -> None:
    """Called on startup if LOCKDOWN state was persisted."""
    import ctypes
    try:
        ctypes.windll.user32.LockWorkStation()
    except Exception:
        pass

    adapters = logs.load_disabled_adapters()
    if adapters:
        from . import service
        await asyncio.to_thread(service.disable_network_adapters)

    for cid in CONFIG.all_owner_chat_ids:
        try:
            await app.bot.send_message(
                cid,
                "🔒 *Bot restarted in LOCKDOWN state.*\n"
                "Send /recover to restore normal operation.",
                parse_mode="Markdown",
            )
        except Exception:
            pass


# ── System helpers ────────────────────────────────────────────────────────────

def _get_current_ssid() -> str | None:
    """Get connected Wi-Fi profile name using Location-permission-free method."""
    from .monitors import _get_wifi_profile
    return _get_wifi_profile()


def _get_usb_devices() -> list[str]:
    import subprocess
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-PnpDevice -Class USB -PresentOnly | Select-Object -ExpandProperty InstanceId"],
            capture_output=True, text=True, timeout=10,
            **config_store.subprocess_kwargs(),
        )
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]
    except Exception:
        return []
