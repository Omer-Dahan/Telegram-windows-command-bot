"""Hotspot, Bluetooth, Wi-Fi, IP."""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import time

import pyautogui
from keyboard import send as kb_send

log = logging.getLogger(__name__)


def _shell(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return f"error: {e}"


def toggle_hotspot() -> str:
    try:
        kb_send("win+a")
        time.sleep(1.0)
        pyautogui.press("down")
        time.sleep(0.4)
        pyautogui.press("right")
        time.sleep(0.4)
        pyautogui.press("enter")
        time.sleep(0.3)
        kb_send("win+a")
        return "📡 Hotspot toggle sent"
    except Exception as e:
        return f"❌ {e}"


def hotspot_status() -> str:
    out = _shell([
        "powershell", "-NoProfile", "-Command",
        "Get-NetAdapter | Where-Object {$_.InterfaceDescription -like '*Wi-Fi Direct*'} | Format-Table -AutoSize"
    ])
    if not out:
        return "📡 Hotspot: OFF"
    return f"📡 Hotspot: {'ON' if 'Up' in out else 'OFF'}\n{out}"


def toggle_bluetooth() -> str:
    try:
        kb_send("win+a")
        time.sleep(1.0)
        pyautogui.press("right")
        time.sleep(0.4)
        pyautogui.press("enter")
        time.sleep(0.3)
        kb_send("win+a")
        return "🎧 Bluetooth toggle sent"
    except Exception as e:
        return f"❌ {e}"


def list_wifi() -> str:
    out = _shell(["netsh", "wlan", "show", "networks", "mode=Bssid"])
    if not out:
        return "📶 No Wi-Fi data"
    if len(out) > 3500:
        out = out[:3500] + "\n…(truncated)"
    return f"📶 Wi-Fi networks:\n```\n{out}\n```"


def wifi_current() -> str:
    out = _shell(["netsh", "wlan", "show", "interfaces"])
    return f"📶 Current Wi-Fi:\n```\n{out}\n```"


def open_location_settings() -> str:
    try:
        import os
        os.startfile("ms-settings:privacy-location")
        return "⚙️ Opened Location settings page on host PC"
    except Exception as e:
        return f"❌ Failed to open location settings: {e}"


def _local_ip_fallback() -> list[str]:
    import psutil
    lines = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        for name, addr_list in addrs.items():
            ipv4 = None
            netmask = None
            for addr in addr_list:
                if addr.family == socket.AF_INET:
                    ipv4 = addr.address
                    netmask = addr.netmask
                    break
            if not ipv4:
                continue
            is_up = stats[name].isup if name in stats else True
            status_str = "Up" if is_up else "Down"
            lines.append(f"• *{name}* (Status: {status_str}):")
            lines.append(f"  IP: `{ipv4}`")
            if netmask:
                lines.append(f"  Subnet Mask: `{netmask}`")
    except Exception as e:
        lines.append(f"  ❌ Fallback error: {e}")
    return lines


def local_ip() -> str:
    try:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            primary_ip = s.getsockname()[0]
            s.close()
        except Exception:
            primary_ip = "Unknown (No Internet connection?)"

        hostname = socket.gethostname()

        cmd = [
            "powershell", "-NoProfile", "-Command",
            "Get-NetIPConfiguration | Where-Object {$_.IPv4Address} | "
            "Select-Object InterfaceAlias, InterfaceDescription, IPv4Address, IPv4DefaultGateway, DNSServer | "
            "ConvertTo-Json -Depth 3"
        ]

        lines = []
        lines.append(f"🌐 *Primary Local IP:* `{primary_ip}`")
        lines.append(f"💻 *Hostname:* `{hostname}`\n")
        lines.append("🔌 *Network Adapters:*")

        out = _shell(cmd)
        if out and not out.startswith("error:"):
            try:
                data = json.loads(out)
                if not data:
                    data = []
                elif not isinstance(data, list):
                    data = [data]

                for item in data:
                    alias = item.get("InterfaceAlias", "Unknown")
                    desc = item.get("InterfaceDescription", "Unknown")

                    ip_obj = item.get("IPv4Address")
                    ip_str = "None"
                    if isinstance(ip_obj, dict):
                        ip_str = ip_obj.get("IPAddress", "None")
                    elif isinstance(ip_obj, str):
                        ip_str = ip_obj

                    gw_obj = item.get("IPv4DefaultGateway")
                    gw_str = "None"
                    if isinstance(gw_obj, dict):
                        gw_str = gw_obj.get("NextHop", "None")
                    elif isinstance(gw_obj, list):
                        gws = []
                        for g in gw_obj:
                            if isinstance(g, dict):
                                gws.append(g.get("NextHop", ""))
                            elif isinstance(g, str):
                                gws.append(g)
                        gw_str = ", ".join(filter(None, gws)) or "None"
                    elif isinstance(gw_obj, str):
                        gw_str = gw_obj

                    dns_obj = item.get("DNSServer")
                    dns_str = "None"
                    if isinstance(dns_obj, dict):
                        dns_str = dns_obj.get("ServerAddresses", "None")
                        if isinstance(dns_str, list):
                            dns_str = ", ".join(dns_str)
                    elif isinstance(dns_obj, list):
                        dns_str = ", ".join(str(d) for d in dns_obj)
                    elif isinstance(dns_obj, str):
                        dns_str = dns_obj

                    lines.append(f"• *{alias}* ({desc}):")
                    lines.append(f"  IP: `{ip_str}`")
                    lines.append(f"  Gateway: `{gw_str}`")
                    lines.append(f"  DNS: `{dns_str}`")
            except Exception as json_err:
                log.warning("JSON parse of PowerShell network adapter details failed: %s", json_err)
                lines.append("_PowerShell details unavailable. Showing basic adapter list._")
                lines.extend(_local_ip_fallback())
        else:
            lines.extend(_local_ip_fallback())

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error retrieving network info: {e}"


def public_ip() -> str:
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            return f"🌍 Public IP: {r.read().decode().strip()}"
    except Exception as e:
        return f"❌ {e}"

