"""Pre-panic forensic snapshot: screenshot, processes, network, battery → ZIP → upload."""
from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import subprocess
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from telegram.ext import Application

from ...core.config import DATA_DIR
from . import config_store

log = logging.getLogger(__name__)

FORENSICS_DIR = DATA_DIR / "forensics"
FORENSICS_DIR.mkdir(exist_ok=True)


def _ps(cmd: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            **config_store.subprocess_kwargs(),
        )
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return f"error: {e}"


def _capture_snapshot(cfg: dict) -> dict[str, bytes | str]:
    """Collect all forensic data. Returns name → bytes/str."""
    items: dict[str, bytes | str] = {}

    # Screenshot
    if cfg.get("include_screenshot", True):
        try:
            import mss
            import mss.tools
            with mss.mss() as sct:
                sct.compression_level = 6
                raw = mss.tools.to_png(sct.grab(sct.monitors[0]).rgb,
                                       sct.grab(sct.monitors[0]).size)
            items["screenshot.png"] = raw
        except Exception as e:
            items["screenshot_error.txt"] = f"screenshot failed: {e}"

    # Webcam snapshot (optional)
    if cfg.get("include_webcam", False):
        try:
            from ..webcam import service as webcam_svc
            img_path = webcam_svc.capture_photo()
            if img_path and Path(img_path).exists():
                items["webcam.jpg"] = Path(img_path).read_bytes()
        except Exception as e:
            items["webcam_error.txt"] = f"webcam failed: {e}"

    # Processes
    if cfg.get("include_processes", True):
        try:
            procs = [
                {"name": p.name(), "pid": p.pid,
                 "cpu": round(p.cpu_percent(), 1),
                 "mem_mb": round(p.memory_info().rss / 1024 / 1024, 1)}
                for p in psutil.process_iter(["name", "pid"])
                if not _safe_ignore(p)
            ]
            items["processes.json"] = json.dumps(procs, indent=2)
        except Exception as e:
            items["processes_error.txt"] = f"process list failed: {e}"

    # Network info
    try:
        from .monitors import _get_wifi_profile
        ssid = _get_wifi_profile() or "N/A"

        visible_nets = _ps("netsh wlan show networks mode=bssid", timeout=8)
        public_ip = "N/A"
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
                public_ip = resp.read().decode().strip()
        except Exception:
            pass

        net_info = {
            "ssid": ssid,
            "public_ip": public_ip,
            "visible_networks": visible_nets[:2000],
        }
        items["network.json"] = json.dumps(net_info, indent=2)
    except Exception as e:
        items["network_error.txt"] = f"network info failed: {e}"

    # USB devices
    try:
        usb_out = _ps(
            "Get-PnpDevice -Class USB -PresentOnly "
            "| Select-Object FriendlyName,InstanceId "
            "| ConvertTo-Json -Depth 1"
        )
        items["usb_devices.json"] = usb_out[:5000]
    except Exception as e:
        items["usb_error.txt"] = f"USB list failed: {e}"

    # Battery + system info
    try:
        bat = psutil.sensors_battery()
        sys_info = {
            "user": getpass.getuser(),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "battery_pct": round(bat.percent) if bat else None,
            "plugged": bat.power_plugged if bat else None,
            "uptime_seconds": int(__import__("time").time() - psutil.boot_time()),
        }
        items["system.json"] = json.dumps(sys_info, indent=2)
    except Exception as e:
        items["system_error.txt"] = f"system info failed: {e}"

    return items


def _safe_ignore(proc) -> bool:
    try:
        return proc.name().lower() in ("system idle process", "system", "registry")
    except Exception:
        return True


def _compress(items: dict[str, bytes | str], trigger: str) -> Path:
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    zip_path = FORENSICS_DIR / f"{ts}_{trigger}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, content in items.items():
            if isinstance(content, str):
                zf.writestr(name, content.encode("utf-8", errors="replace"))
            else:
                zf.writestr(name, content)
    log.info("PANIC forensics: saved %s (%d bytes)", zip_path.name, zip_path.stat().st_size)
    return zip_path


def _prune_old_snapshots(keep: int) -> None:
    zips = sorted(FORENSICS_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in zips[keep:]:
        try:
            old.unlink()
        except Exception:
            pass


async def capture_and_upload(
    app: "Application",
    chat_ids: set[int],
    trigger: str,
    test_mode: bool,
) -> Path | None:
    """Capture, compress, upload (async). Returns zip path or None on failure."""
    cfg = config_store.load().get("forensics", {})
    try:
        items = await asyncio.to_thread(_capture_snapshot, cfg)
        zip_path = await asyncio.to_thread(_compress, items, trigger)
        await asyncio.to_thread(_prune_old_snapshots, cfg.get("keep_local_copies", 20))

        if not test_mode and cfg.get("upload_to_telegram", True):
            caption = f"🔬 Forensic snapshot — {trigger} {'[TEST]' if test_mode else ''}"
            for cid in chat_ids:
                try:
                    with open(zip_path, "rb") as f:
                        await app.bot.send_document(cid, f, caption=caption)
                except Exception as e:
                    log.warning("PANIC forensics upload failed for chat %d: %s", cid, e)

        return zip_path
    except Exception:
        log.exception("PANIC forensics: capture_and_upload failed")
        return None


def list_snapshots() -> list[Path]:
    return sorted(FORENSICS_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
