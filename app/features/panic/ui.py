"""All InlineKeyboardMarkup builders for the panic module.

Navigation rule: every action uses q.edit_message_text() — never send a new message.
Back/Home/Refresh are present on every submenu.
All callback_data strings are ≤ 64 bytes.
Dynamic toggle buttons show ✅/❌ based on live config.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton as IB
from telegram import InlineKeyboardMarkup

from . import config_store, scoring, state_machine, watchdog
from .state_machine import PanicState

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mk(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(list(rows))


def _nav(back: str | None = None) -> list[IB]:
    btns = []
    if back:
        btns.append(IB("⬅️ Back", callback_data=back))
    btns.append(IB("🏠 Home", callback_data="panic:menu"))
    btns.append(IB("🔄 Refresh", callback_data="panic:refresh"))
    return btns


def _tog(enabled: bool, label_on: str, label_off: str,
         cb_on: str, cb_off: str) -> IB:
    if enabled:
        return IB(f"✅ {label_on}", callback_data=cb_off)
    return IB(f"❌ {label_off}", callback_data=cb_on)


# ── Main panel ────────────────────────────────────────────────────────────────

def main_menu(cfg: dict | None = None) -> InlineKeyboardMarkup:
    if cfg is None:
        cfg = config_store.load()
    enabled = cfg.get("enabled", False)
    test_mode = cfg.get("test_mode", False)
    silent = cfg.get("silent_mode", False)
    state = state_machine.get_state()
    score = scoring.get_total()
    health = watchdog.get_health_summary()
    n_triggers = sum(
        1 for t in cfg.get("triggers", {}).values() if t.get("enabled", False)
    )

    arm_btn = (
        IB("🛡️ ✅ ARMED — tap to disarm", callback_data="panic:disarm")
        if enabled else
        IB("🛡️ ❌ DISARMED — tap to arm", callback_data="panic:arm")
    )

    return _mk(
        [arm_btn],
        [IB(f"📊 Score: {round(score)}/100", callback_data="panic:score"),
         IB(f"🗺️ State: {state.value}", callback_data="panic:state"),
         IB(f"{health}", callback_data="panic:health")],
        [IB(f"🎯 Triggers ({n_triggers}/10)", callback_data="panic:trg:menu"),
         IB("🔒 Actions", callback_data="panic:act:menu")],
        [IB("⬆️ Escalation", callback_data="panic:esc:menu"),
         IB("🔑 Recovery", callback_data="panic:offline:cfg")],
        [IB("⌨️ Hotkey: " + ("ON" if cfg.get("hotkey", {}).get("enabled") else "OFF"),
            callback_data="panic:hotkey:cfg"),
         IB("🔬 Forensics", callback_data="panic:forensics:list")],
        [IB(f"📋 Logs ({_log_count()})", callback_data="panic:logs:0"),
         IB("⚙️ Settings", callback_data="panic:settings")],
        [IB("🧪 Test: " + ("ON" if test_mode else "OFF"),
            callback_data="panic:testmode:off" if test_mode else "panic:testmode:on"),
         IB("🔇 Silent: " + ("ON" if silent else "OFF"),
            callback_data="panic:silent:off" if silent else "panic:silent:on"),
         IB("⚡ Trigger!", callback_data="panic:manual")],
    )


def _log_count() -> int:
    try:
        from . import logs
        return logs.get_event_count()
    except Exception:
        return 0


# ── Score panel ───────────────────────────────────────────────────────────────

def score_panel() -> tuple[str, InlineKeyboardMarkup]:
    snap = scoring.get_snapshot()
    total = snap["total"]
    thr = snap["thresholds"]
    status = snap["status"]
    decay = snap["decay_per_min"]

    lines = [f"📊 *Confidence Score: {round(total)}/100*  {'⚠️' if status == 'WARNING' else ('🔒' if 'LEVEL' in status else '🟢')} {status}\n"]
    if snap["breakdown"]:
        for t, s in snap["breakdown"].items():
            lines.append(f"├─ {t.replace('_', ' ').title()}: {round(s)} pts")
        lines.append(f"└─ Decay: -{decay} pts/min")
    else:
        lines.append("_No active trigger scores._")

    lines.append(f"\nThresholds: {thr['warning']}=⚠️  {thr['level1']}=L1  {thr['level2']}=L2  {thr['level3']}=L3")

    text = "\n".join(lines)
    kb = _mk(_nav("panic:menu"))
    return text, kb


# ── State panel ───────────────────────────────────────────────────────────────

def state_panel() -> tuple[str, InlineKeyboardMarkup]:
    current = state_machine.get_state()
    meta = state_machine.get_meta()
    lines = [
        f"🗺️ *FSM State: {current.value}*\n",
        f"Trigger: `{meta.get('trigger', 'N/A')}`",
        f"Reason:  `{meta.get('reason', 'N/A')}`",
        f"Since:   `{meta.get('entered_at', 'N/A')}`",
        "",
        "NORMAL → WARNING → L1 → L2 → L3 → LOCKDOWN",
    ]
    btns = [_nav("panic:menu")]
    if current == PanicState.LOCKDOWN:
        btns.insert(0, [IB("🔓 Recover", callback_data="panic:recover")])
    if current == PanicState.SAFE_MODE:
        btns.insert(0, [IB("🔓 Exit Safe Mode", callback_data="panic:safe:off")])
    return "\n".join(lines), _mk(*btns)


# ── Watchdog health panel ─────────────────────────────────────────────────────

def health_panel() -> tuple[str, InlineKeyboardMarkup]:
    health = watchdog.get_health()
    lines = ["💚 *Watchdog Status*\n",
             f"{'Component':<22} {'Status':<8} Restarts"]
    for name, s in sorted(health.items()):
        icon = "✅" if s.get("alive", True) else "❌"
        restarts = s.get("restarts", 0)
        lines.append(f"{name:<22} {icon}       {restarts}")

    btns = []
    dead = [n for n, s in health.items() if not s.get("alive", True)]
    if dead:
        for d in dead[:3]:
            btns.append([IB(f"🔄 Restart {d}", callback_data=f"panic:watchdog:restart:{d}")])

    btns.append([IB("🔄 Restart All Monitors", callback_data="panic:watchdog:restart_all")])
    btns.append(_nav("panic:menu"))
    return "\n".join(lines), _mk(*btns)


# ── Triggers list ─────────────────────────────────────────────────────────────

_TRIGGER_LABELS: dict[str, str] = {
    "wifi_loss":        "📶 Wi-Fi Loss",
    "location_change":  "📍 Location",
    "usb_change":       "🔌 USB Change",
    "power_disconnect": "🔋 Power Disconnect",
    "bluetooth_loss":   "📱 Bluetooth Loss",
    "dead_man_switch":  "⏳ Dead-Man Switch",
    "failed_login":     "🔐 Failed Login",
    "lid_open":         "💻 Lid Open",
    "boot_source":      "🥾 Boot Source",
    "manual":           "⚡ Manual",
}


def triggers_menu(cfg: dict | None = None) -> tuple[str, InlineKeyboardMarkup]:
    if cfg is None:
        cfg = config_store.load()
    triggers = cfg.get("triggers", {})
    rows = []
    row = []
    for i, (name, label) in enumerate(_TRIGGER_LABELS.items()):
        t_cfg = triggers.get(name, {})
        enabled = t_cfg.get("enabled", False)
        icon = "✅" if enabled else "❌"
        btn = IB(f"{icon} {label}", callback_data=f"panic:trg:cfg:{name}")
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(_nav("panic:menu"))
    return "🎯 *Configure Triggers*\nTap a trigger to configure it.", _mk(*rows)


def trigger_cfg_menu(name: str) -> tuple[str, InlineKeyboardMarkup]:
    cfg = config_store.get_trigger(name)
    label = _TRIGGER_LABELS.get(name, name)
    enabled = cfg.get("enabled", False)
    grace = cfg.get("grace_seconds", 300)
    timeout = cfg.get("timeout_minutes", cfg.get("timeout_hours", 0))
    trusted_ssids = cfg.get("trusted_ssids", [])
    trusted_bt = cfg.get("trusted_devices", [])
    trusted_usb = cfg.get("trusted_device_ids", [])

    lines = [f"⚙️ *{label}*\n",
             f"Status: {'✅ Enabled' if enabled else '❌ Disabled'}",
             f"Grace: {grace}s"]

    if timeout:
        lines.append(f"Timeout: {timeout} {'min' if 'minutes' in str(cfg) else 'hrs'}")

    rows: list[list[IB]] = []
    tog_cb = f"panic:trg:toggle:{name}"
    rows.append([IB(f"{'🔴 Disable' if enabled else '🟢 Enable'}", callback_data=tog_cb)])

    # Grace presets
    rows.append([
        IB("Grace: 0s", callback_data=f"panic:trg:grace:{name}:0"),
        IB("30s",       callback_data=f"panic:trg:grace:{name}:30"),
        IB("1m",        callback_data=f"panic:trg:grace:{name}:60"),
        IB("5m",        callback_data=f"panic:trg:grace:{name}:300"),
        IB("10m",       callback_data=f"panic:trg:grace:{name}:600"),
    ])

    # Timeout presets (if applicable)
    if name in ("wifi_loss", "bluetooth_loss"):
        rows.append([
            IB("Timeout: 1m",  callback_data=f"panic:trg:timeout:{name}:1"),
            IB("3m",           callback_data=f"panic:trg:timeout:{name}:3"),
            IB("5m",           callback_data=f"panic:trg:timeout:{name}:5"),
            IB("15m",          callback_data=f"panic:trg:timeout:{name}:15"),
        ])

    # Trusted lists
    if name == "wifi_loss":
        lines.append(f"\nTrusted SSIDs ({len(trusted_ssids)}):")
        for i, ssid in enumerate(trusted_ssids[:5]):
            rows.append([IB(f"➖ {ssid}", callback_data=f"panic:trg:del_ssid:{i}")])
        rows.append([IB("➕ Add current SSID", callback_data="panic:trg:add_cur_ssid"),
                     IB("➕ Type SSID", callback_data="panic:trg:add_ssid")])

    if name == "bluetooth_loss":
        lines.append(f"\nTrusted BT Devices ({len(trusted_bt)}):")
        for i, dev in enumerate(trusted_bt[:5]):
            rows.append([IB(f"➖ {dev}", callback_data=f"panic:trg:del_bt:{i}")])
        rows.append([IB("➕ Add BT device", callback_data="panic:trg:add_bt")])

    if name == "usb_change":
        lines.append(f"\nTrusted USB IDs ({len(trusted_usb)}):")
        for i, uid in enumerate(trusted_usb[:5]):
            rows.append([IB(f"➖ {uid[:20]}…", callback_data=f"panic:trg:del_usb:{i}")])
        rows.append([IB("➕ Add current USBs", callback_data="panic:trg:add_usb")])

    rows.append([IB("🧪 Test trigger", callback_data=f"panic:trg:test:{name}")])
    rows.append(_nav("panic:trg:menu"))
    return "\n".join(lines), _mk(*rows)


# ── Actions list ──────────────────────────────────────────────────────────────

_ACTION_LABELS: dict[str, str] = {
    "telegram_alert":   "📨 Alert",
    "lock_workstation": "🔒 Lock",
    "forensic_snapshot":"🔬 Forensics",
    "kill_processes":   "💀 Kill Procs",
    "veracrypt_dismount":"🔐 VeraCrypt",
    "ram_clear":        "🧹 RAM Clear",
    "browser_cleanup":  "🌐 Browser",
    "delete_temp":      "🗑️ Temp Files",
    "custom_script":    "📜 Script",
    "disable_network":  "📶 Disable Net",
    "hibernate":        "😴 Hibernate",
    "shutdown":         "⏹️ Shutdown",
}

_LEVEL_ACTIONS_ORDER = {
    "level1": ["telegram_alert", "lock_workstation"],
    "level2": ["forensic_snapshot", "kill_processes", "veracrypt_dismount",
               "ram_clear", "browser_cleanup", "delete_temp", "custom_script"],
    "level3": ["disable_network", "hibernate", "shutdown"],
}


def actions_menu(level_key: str = "level1", cfg: dict | None = None) -> tuple[str, InlineKeyboardMarkup]:
    if cfg is None:
        cfg = config_store.load()
    actions = cfg.get("actions", {}).get(level_key, {})

    level_tabs = [
        IB(f"{'→' if level_key == 'level1' else ''}L1", callback_data="panic:act:lvl:level1"),
        IB(f"{'→' if level_key == 'level2' else ''}L2", callback_data="panic:act:lvl:level2"),
        IB(f"{'→' if level_key == 'level3' else ''}L3", callback_data="panic:act:lvl:level3"),
    ]

    rows: list[list[IB]] = [level_tabs]
    for action_name in _LEVEL_ACTIONS_ORDER.get(level_key, []):
        a_cfg = actions.get(action_name, {})
        enabled = a_cfg.get("enabled", False)
        label = _ACTION_LABELS.get(action_name, action_name)
        icon = "✅" if enabled else "❌"
        row = [
            IB(f"{icon} {label}", callback_data=f"panic:act:toggle:{level_key}:{action_name}"),
            IB("⚙️", callback_data=f"panic:act:cfg:{level_key}:{action_name}"),
        ]
        rows.append(row)

    rows.append(_nav("panic:menu"))
    level_num = level_key[-1]
    return f"🔒 *Level {level_num} Actions*", _mk(*rows)


# ── Escalation config ─────────────────────────────────────────────────────────

def escalation_menu() -> tuple[str, InlineKeyboardMarkup]:
    cfg = config_store.get_escalation_config()
    l12 = cfg.get("l1_to_l2_delay_seconds", 480)
    l23 = cfg.get("l2_to_l3_delay_seconds", 1320)
    notify = cfg.get("notify_on_transition", True)
    allow = cfg.get("allow_manual_escalate", True)

    text = (
        f"⬆️ *Escalation Settings*\n\n"
        f"L1 → L2 delay: *{l12 // 60}m*\n"
        f"L2 → L3 delay: *{l23 // 60}m*\n"
        f"Notify on transition: {'✅' if notify else '❌'}\n"
        f"Allow manual escalate: {'✅' if allow else '❌'}"
    )

    state = state_machine.get_state()
    manual_rows = []
    if state == PanicState.LEVEL1:
        manual_rows.append([IB("⚡ Escalate to L2 now", callback_data="panic:esc:now:2")])
    if state == PanicState.LEVEL2:
        manual_rows.append([IB("⚡ Escalate to L3 now", callback_data="panic:esc:now:3")])

    delay_rows = [
        [IB("L1→L2: 2m", callback_data="panic:esc:delay:l12:120"),
         IB("5m",         callback_data="panic:esc:delay:l12:300"),
         IB("8m",         callback_data="panic:esc:delay:l12:480"),
         IB("15m",        callback_data="panic:esc:delay:l12:900"),
         IB("30m",        callback_data="panic:esc:delay:l12:1800")],
        [IB("L2→L3: 5m", callback_data="panic:esc:delay:l23:300"),
         IB("15m",        callback_data="panic:esc:delay:l23:900"),
         IB("22m",        callback_data="panic:esc:delay:l23:1320"),
         IB("60m",        callback_data="panic:esc:delay:l23:3600")],
        [_tog(notify, "Notify transitions", "Notify transitions",
              "panic:esc:notify:on", "panic:esc:notify:off"),
         _tog(allow, "Manual escalate", "Manual escalate",
              "panic:esc:manual:on", "panic:esc:manual:off")],
    ]
    all_rows = manual_rows + delay_rows + [_nav("panic:menu")]
    return text, _mk(*all_rows)


# ── Logs panel ────────────────────────────────────────────────────────────────

def logs_panel(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    from . import logs as panic_logs
    events = panic_logs.get_events(page=page, page_size=8)
    total = panic_logs.get_event_count()
    pages = max(1, (total + 7) // 8)

    lines = [f"📋 *Panic Logs* (page {page + 1}/{pages}, {total} total)\n"]
    if not events:
        lines.append("_No events yet._")
    for e in events:
        ts = e.get("timestamp", "?")[:16].replace("T", " ")
        trigger = e.get("trigger", "?").replace("_", " ")
        levels = e.get("levels_executed", [])
        mode = "🧪" if e.get("test_mode") else ""
        lines.append(f"`{ts}` {mode} {trigger} → L{',L'.join(map(str, levels))}")

    nav_row = []
    if page > 0:
        nav_row.append(IB("◀️", callback_data=f"panic:logs:{page - 1}"))
    if page < pages - 1:
        nav_row.append(IB("▶️", callback_data=f"panic:logs:{page + 1}"))

    rows = [_nav("panic:menu")]
    if nav_row:
        rows.insert(0, nav_row)
    rows.insert(0, [IB("📊 Analytics", callback_data="panic:logs:analytics"),
                    IB("🗑️ Clear logs", callback_data="panic:logs:clear")])
    return "\n".join(lines), _mk(*rows)


def logs_clear_confirm() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "⚠️ Clear *all* panic logs?",
        _mk([IB("✅ Yes, clear", callback_data="panic:logs:clear:confirm"),
             IB("❌ Cancel", callback_data="panic:logs:0")]),
    )


# ── Forensics panel ───────────────────────────────────────────────────────────

def forensics_panel() -> tuple[str, InlineKeyboardMarkup]:
    from . import forensics
    snaps = forensics.list_snapshots()
    lines = [f"🔬 *Forensic Snapshots* ({len(snaps)} files)\n"]
    rows = []
    for i, p in enumerate(snaps[:10]):
        size_kb = p.stat().st_size // 1024
        lines.append(f"{i + 1}. `{p.name[:30]}` ({size_kb} KB)")
        rows.append([IB(f"📤 Send #{i + 1}", callback_data=f"panic:forensics:send:{i}")])
    if not snaps:
        lines.append("_No snapshots yet._")
    rows.append(_nav("panic:menu"))
    return "\n".join(lines), _mk(*rows)


# ── Hotkey config ─────────────────────────────────────────────────────────────

def hotkey_menu() -> tuple[str, InlineKeyboardMarkup]:
    cfg = config_store.load().get("hotkey", {})
    enabled = cfg.get("enabled", False)
    combo = cfg.get("combo", "<ctrl>+<alt>+<end>")
    debounce = cfg.get("debounce_seconds", 2)

    text = (
        f"⌨️ *Global Hotkey*\n\n"
        f"Status: {'✅ Enabled' if enabled else '❌ Disabled'}\n"
        f"Combo: `{combo}`\n"
        f"Debounce: {debounce}s\n\n"
        "_To set combo: type the key combo after tapping 'Set Combo'_"
    )
    return text, _mk(
        [_tog(enabled, "Enabled", "Disabled", "panic:hotkey:on", "panic:hotkey:off")],
        [IB("Set Combo", callback_data="panic:hotkey:set_combo"),
         IB("🧪 Test", callback_data="panic:hotkey:test")],
        _nav("panic:menu"),
    )


# ── Offline recovery config ───────────────────────────────────────────────────

def offline_recovery_menu() -> tuple[str, InlineKeyboardMarkup]:
    cfg = config_store.load().get("offline_recovery", {})
    from . import emergency_config
    has_pin = emergency_config.has_pin()
    locked = emergency_config.is_locked_out()

    text = (
        f"🔑 *Offline Recovery*\n\n"
        f"PIN set: {'✅' if has_pin else '❌ (not configured)'}\n"
        f"Locked out: {'🔴 YES' if locked else '🟢 No'}\n\n"
        "Drop a `panic_recovery.json` file on a USB or in the watch folder to recover offline.\n"
        "_File format:_ `{{\"pin\": \"1234\", \"timestamp\": \"ISO-8601\"}}`"
    )
    return text, _mk(
        [IB("🔑 Set Recovery PIN", callback_data="panic:offline:setpin"),
         IB("📁 Set Watch Dir", callback_data="panic:offline:setdir")],
        _nav("panic:menu"),
    )


# ── Settings ──────────────────────────────────────────────────────────────────

def settings_menu() -> tuple[str, InlineKeyboardMarkup]:
    cfg = config_store.load()
    silent = cfg.get("silent_mode", False)
    cooldown = cfg.get("cooldown_seconds", 300)
    wd_cfg = cfg.get("watchdog", {})
    wd_enabled = wd_cfg.get("enabled", True)

    text = (
        f"⚙️ *Panic Settings*\n\n"
        f"Silent mode: {'✅' if silent else '❌'}\n"
        f"Cooldown: {cooldown // 60}m\n"
        f"Watchdog: {'✅' if wd_enabled else '❌'}"
    )
    return text, _mk(
        [_tog(silent, "Silent ON", "Silent OFF", "panic:silent:on", "panic:silent:off")],
        [_tog(wd_enabled, "Watchdog ON", "Watchdog OFF",
              "panic:watchdog:on", "panic:watchdog:off")],
        [IB("Cooldown: 1m",  callback_data="panic:cooldown:60"),
         IB("5m",            callback_data="panic:cooldown:300"),
         IB("10m",           callback_data="panic:cooldown:600"),
         IB("30m",           callback_data="panic:cooldown:1800")],
        _nav("panic:menu"),
    )


# ── Grace keyboard (sent as standalone message during countdown) ──────────────

def grace_keyboard(trigger: str) -> InlineKeyboardMarkup:
    return _mk([
        IB("❌ Cancel Panic",  callback_data=f"panic:grace:cancel:{trigger}"),
        IB("⚡ Trigger Now",   callback_data=f"panic:grace:now:{trigger}"),
        IB("🙈 Ignore Once",  callback_data=f"panic:grace:ignore:{trigger}"),
    ])


# ── Recovery confirm ──────────────────────────────────────────────────────────

def recover_confirm() -> tuple[str, InlineKeyboardMarkup]:
    state = state_machine.get_state()
    return (
        f"🔓 *Recover from {state.value}?*\n\n"
        "This will re-enable network adapters and cancel any pending shutdown.",
        _mk(
            [IB("✅ Confirm Recovery", callback_data="panic:recover:confirm"),
             IB("❌ Cancel", callback_data="panic:menu")],
        ),
    )


# ── Safe mode panel ───────────────────────────────────────────────────────────

def safe_mode_panel() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "🛡️ *Safe Mode Active*\n\n"
        "Too many panic events detected.\n"
        "Only Telegram alerts are sent — no destructive actions.\n\n"
        "Tap below to exit safe mode.",
        _mk([IB("🔓 Exit Safe Mode", callback_data="panic:safe:off")],
            _nav("panic:menu")),
    )
