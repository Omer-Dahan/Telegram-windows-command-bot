"""Feature registry — order matters for text matching (first match wins).

Each module exposes:
    register(app)            — adds command/callback/specialized message handlers
    match_text(text, chat_id) → TextResult | None — for the chain-of-responsibility router
"""
from __future__ import annotations

# Order: more specific button matches first; verb-based features after.
from . import (
    start_help,           # /start, /help, /about, /release_keys
    keys,                 # ⌨️ Builder, ⌨️ Keys, k <combo>, type <text>
    macros,               # 📝 Macros, /macro, /save_macro, ...
    recorder,             # 📼 Recorder — record & replay mouse/keyboard macros
    switcher,             # 🔀 Switcher (interactive Alt+Tab)
    system,               # 📊 System info, 🔋 Power, lock/sleep/etc
    audio,                # 🔊 Volume
    brightness,           # 💡 Brightness
    screen,               # 📸 Screenshot, 🎥 Record screen
    webcam,               # 📷 Webcam
    network,              # 📡 Hotspot, 🎧 Bluetooth, 📶 Wi-Fi, ip
    media,                # 🎦 VLC, 🎬 Netflix
    mouse,                # mouse <move|click|scroll|pos>
    windows_proc,         # 🪟 Windows, 📄 Processes, focus/close/kill/ps
    files,                # 📂 Files, ls/cd/pwd/download
    clipboard,            # ✂ Clipboard, copy/paste
    shell,                # cmd/ps1/launch/url
    panic,                # 🚨 Panic Mode / Anti-Theft Protection
)

ALL_FEATURES = [
    start_help, keys, macros, recorder, switcher, system, audio, brightness,
    screen, webcam, network, media, mouse, windows_proc, files,
    clipboard, shell, panic,
]
