"""Main reply keyboard — single source of truth for menu button labels."""
from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup

# Each label is a public constant, so feature handlers can match on it without
# duplicating the string.
SYSTEM_INFO = "📊 System info"
POWER = "🔋 Power"
HOTSPOT = "📡 Hotspot"
BLUETOOTH = "🎧 Bluetooth"
WIFI = "📶 Wi-Fi"
VOLUME = "🔊 Volume"
BRIGHTNESS = "💡 Brightness"
SCREENSHOT = "📸 Screenshot"
RECORD = "🎥 Record screen"
WEBCAM = "📷 Webcam"
KEYS = "⌨️ Keys"
BUILDER = "⌨️ Builder"
MACROS = "📝 Macros"
RECORDER = "📼 Recorder"
SWITCHER = "🔀 Switcher"
WINDOWS = "🪟 Windows"
PROCESSES = "📄 Processes"
VLC = "🎦 VLC"
NETFLIX = "🎬 Netflix"
FILES = "📂 Files"
CLIPBOARD = "✂ Clipboard"
HELP = "💡 Help"
PANIC = "🚨 Panic"

LAYOUT = [
    [SYSTEM_INFO, POWER],
    [HOTSPOT, BLUETOOTH, WIFI],
    [VOLUME, BRIGHTNESS],
    [SCREENSHOT, RECORD, WEBCAM],
    [KEYS, BUILDER, MACROS],
    [RECORDER],
    [SWITCHER, WINDOWS, PROCESSES],
    [VLC, NETFLIX],
    [FILES, CLIPBOARD],
    [HELP],
    [PANIC],
]


def main_menu() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(b) for b in row] for row in LAYOUT]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)
