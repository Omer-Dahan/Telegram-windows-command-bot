"""Network inline keyboards."""
from __future__ import annotations

from telegram import InlineKeyboardButton as IB, InlineKeyboardMarkup


def hotspot_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [IB("🔁 Toggle hotspot", callback_data="net:hotspot:toggle")],
        [IB("ℹ Status", callback_data="net:hotspot:status")],
    ])


def bluetooth_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [IB("🔁 Toggle Bluetooth", callback_data="net:bt:toggle")],
    ])


def wifi_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [IB("📶 List networks", callback_data="net:wifi:list")],
        [IB("ℹ Current", callback_data="net:wifi:current")],
        [IB("🌐 Local IP", callback_data="net:ip:local"),
         IB("🌍 Public IP", callback_data="net:ip:public")],
        [IB("⚙️ Location Settings", callback_data="net:wifi:open_location")],
    ])

