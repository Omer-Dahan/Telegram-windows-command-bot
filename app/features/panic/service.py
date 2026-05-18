"""Individual defensive action implementations.

Pure logic — no Telegram imports. All subprocess calls respect silent_mode.
Actions are grouped by escalation level but live in this single module for simplicity.
"""
from __future__ import annotations

import asyncio
import ctypes
import getpass
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import psutil

from ...core.config import CONFIG, DATA_DIR
from ..system import service as sys_service
from . import config_store, logs, state_machine
from .state_machine import PanicState

log = logging.getLogger(__name__)

_SHUTDOWN_CANCEL: threading.Event | None = None
_SHUTDOWN_LOCK = threading.Lock()


# ── Subprocess helper ────────────────────────────────────────────────────────

def _ps(cmd: str, timeout: int = 15) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            **config_store.subprocess_kwargs(),
        )
        return (r.stdout or r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return f"error: {e}"


def _run(args: list[str], timeout: int = 15) -> int:
    try:
        r = subprocess.run(
            args, capture_output=True, timeout=timeout,
            **config_store.subprocess_kwargs(),
        )
        return r.returncode
    except Exception:
        return -1


# ── Snapshot helper (used by alert and forensics) ────────────────────────────

def get_system_snapshot() -> dict:
    """Lightweight system state snapshot. Always safe to call."""
    snap: dict = {"timestamp": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
    try:
        snap["user"] = getpass.getuser()
    except Exception:
        snap["user"] = "unknown"

    try:
        bat = psutil.sensors_battery()
        snap["battery"] = round(bat.percent) if bat else None
        snap["plugged"] = bat.power_plugged if bat else None
    except Exception:
        snap["battery"] = snap["plugged"] = None

    try:
        from .monitors import _get_wifi_profile
        snap["ssid"] = _get_wifi_profile() or "N/A"
    except Exception:
        snap["ssid"] = "N/A"

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            snap["public_ip"] = resp.read().decode().strip()
    except Exception:
        snap["public_ip"] = "N/A"

    return snap


# ── Level 1 actions ──────────────────────────────────────────────────────────

async def telegram_alert(
    bot,
    chat_ids: set[int],
    trigger: str,
    snapshot: dict,
    test_mode: bool,
    level: int = 1,
) -> str:
    """Send alert to all owners. Always runs even in test mode (labelled [TEST])."""
    label = "🧪 *TEST MODE* — " if test_mode else ""
    bat = f"{snapshot.get('battery', '?')}%{'⚡' if snapshot.get('plugged') else '🔋'}"
    text = (
        f"{label}🚨 *Panic Level {level} — {trigger.replace('_', ' ').title()}*\n\n"
        f"🕐 {snapshot.get('timestamp', 'N/A')}\n"
        f"👤 {snapshot.get('user', 'N/A')}\n"
        f"📶 SSID: `{snapshot.get('ssid', 'N/A')}`\n"
        f"🌐 IP: `{snapshot.get('public_ip', 'N/A')}`\n"
        f"🔋 Battery: {bat}\n"
    )
    errors = []
    for cid in chat_ids:
        try:
            await bot.send_message(cid, text, parse_mode="Markdown")
        except Exception as e:
            errors.append(str(e))
    return "ok" if not errors else f"partial: {errors[0]}"


def lock_workstation() -> str:
    try:
        ctypes.windll.user32.LockWorkStation()
        return "ok"
    except Exception as e:
        return f"error: {e}"


# ── Level 2 actions ──────────────────────────────────────────────────────────

def kill_processes(process_list: list[str]) -> str:
    killed, failed = [], []
    lower_list = [p.lower() for p in process_list]
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"].lower() in lower_list:
                proc.kill()
                killed.append(proc.info["name"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            failed.append(proc.info.get("name", "?"))
    return f"killed={killed} failed={failed}" if killed or failed else "none matched"


def veracrypt_dismount() -> str:
    vc_paths = [
        shutil.which("VeraCrypt"),
        r"C:\Program Files\VeraCrypt\VeraCrypt.exe",
        r"C:\Program Files (x86)\VeraCrypt\VeraCrypt.exe",
    ]
    vc = next((p for p in vc_paths if p and Path(p).exists()), None)
    if not vc:
        return "skipped: VeraCrypt not found"
    try:
        _run([vc, "/dismount", "/force", "/quit", "/silent"])
        _run([vc, "/wipecache", "/quit", "/silent"])
        return "ok"
    except Exception as e:
        return f"error: {e}"


def clear_ram() -> str:
    results = []
    # Clear Windows Credential Manager entries
    try:
        out = _ps("cmdkey /list")
        targets = [
            line.split("Target:")[-1].strip()
            for line in out.splitlines()
            if "Target:" in line
        ]
        for t in targets:
            _ps(f'cmdkey /delete:"{t}"')
        results.append(f"credentials cleared: {len(targets)}")
    except Exception as e:
        results.append(f"credentials error: {e}")

    # Trigger Windows memory idle tasks
    try:
        ctypes.windll.advapi32.ProcessIdleTasks()
        results.append("idle tasks triggered")
    except Exception as e:
        results.append(f"idle tasks error: {e}")

    return "; ".join(results)


def browser_cleanup(browsers: list[str]) -> str:
    """Delete browser history/cookies/sessions/cache. Browsers must already be killed."""
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    results = []

    profiles: dict[str, list[str]] = {
        "chrome": [
            os.path.join(local, r"Google\Chrome\User Data\Default\History"),
            os.path.join(local, r"Google\Chrome\User Data\Default\Cookies"),
            os.path.join(local, r"Google\Chrome\User Data\Default\Sessions"),
            os.path.join(local, r"Google\Chrome\User Data\Default\Cache"),
        ],
        "edge": [
            os.path.join(local, r"Microsoft\Edge\User Data\Default\History"),
            os.path.join(local, r"Microsoft\Edge\User Data\Default\Cookies"),
            os.path.join(local, r"Microsoft\Edge\User Data\Default\Sessions"),
            os.path.join(local, r"Microsoft\Edge\User Data\Default\Cache"),
        ],
        "firefox": [
            os.path.join(roaming, r"Mozilla\Firefox\Profiles"),
        ],
    }

    for browser in browsers:
        paths = profiles.get(browser.lower(), [])
        removed = 0
        for p in paths:
            path = Path(p)
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
                elif path.is_file():
                    path.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                pass
        results.append(f"{browser}: {removed} items")
    return "; ".join(results) if results else "nothing removed"


def delete_temp_files() -> str:
    removed = 0
    for path_str in [os.environ.get("TEMP", ""), os.environ.get("TMP", ""),
                     r"C:\Windows\Temp"]:
        if not path_str:
            continue
        target = Path(path_str)
        if not target.exists():
            continue
        for item in target.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
    return f"removed ~{removed} temp items"


def run_custom_script(script_path: str | None) -> str:
    if not script_path:
        return "skipped: no script configured"
    p = Path(script_path)
    if not p.exists():
        return f"skipped: script not found ({p.name})"
    ext = p.suffix.lower()
    try:
        if ext == ".ps1":
            r = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                capture_output=True, text=True, timeout=60,
                **config_store.subprocess_kwargs(),
            )
        else:
            r = subprocess.run(
                str(p), shell=True, capture_output=True, text=True, timeout=60,
                **config_store.subprocess_kwargs(),
            )
        return f"exit={r.returncode}"
    except subprocess.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return f"error: {e}"


# ── Level 3 actions ──────────────────────────────────────────────────────────

def disable_network_adapters() -> str:
    """Disable all active adapters. Saves names to logs for recovery."""
    out = _ps(
        "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} "
        "| Select-Object -ExpandProperty Name"
    )
    adapters = [a.strip() for a in out.splitlines() if a.strip()]
    if not adapters:
        return "no active adapters found"

    logs.save_disabled_adapters(adapters)
    state_machine.save_disabled_adapters(adapters)

    failed = []
    for adapter in adapters:
        rc = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Disable-NetAdapter -Name '{adapter}' -Confirm:$false"]
        )
        if rc != 0:
            failed.append(adapter)

    return f"disabled {len(adapters) - len(failed)}/{len(adapters)}"


async def shutdown_with_countdown(bot, chat_ids: set[int], delay_s: int) -> str:
    """Async countdown before shutdown. Cancellable via get_shutdown_cancel()."""
    global _SHUTDOWN_CANCEL
    with _SHUTDOWN_LOCK:
        cancel_ev = threading.Event()
        _SHUTDOWN_CANCEL = cancel_ev

    for remaining in range(delay_s, 0, -10):
        if cancel_ev.is_set():
            for cid in chat_ids:
                try:
                    await bot.send_message(cid, "✅ Shutdown cancelled.")
                except Exception:
                    pass
            with _SHUTDOWN_LOCK:
                _SHUTDOWN_CANCEL = None
            return "cancelled"
        msg = f"⚠️ Shutdown in {remaining}s…"
        for cid in chat_ids:
            try:
                await bot.send_message(cid, msg)
            except Exception:
                pass
        await asyncio.sleep(min(10, remaining))

    if not cancel_ev.is_set():
        sys_service.shutdown_pc(delay_seconds=0)
        return "shutdown initiated"
    return "cancelled"


def get_shutdown_cancel() -> threading.Event | None:
    with _SHUTDOWN_LOCK:
        return _SHUTDOWN_CANCEL


def cancel_shutdown() -> bool:
    """Cancel a pending shutdown countdown. Returns True if one was running."""
    with _SHUTDOWN_LOCK:
        ev = _SHUTDOWN_CANCEL
    if ev:
        ev.set()
        # Also abort any Windows shutdown command
        subprocess.run(["shutdown", "/a"], capture_output=True,
                       **config_store.subprocess_kwargs())
        return True
    subprocess.run(["shutdown", "/a"], capture_output=True,
                   **config_store.subprocess_kwargs())
    return False


def re_enable_adapters(adapters: list[str]) -> list[str]:
    """Re-enable named adapters. Returns list of result strings."""
    results = []
    for adapter in adapters:
        rc = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Enable-NetAdapter -Name '{adapter}' -Confirm:$false"]
        )
        icon = "✅" if rc == 0 else "❌"
        results.append(f"{icon} {adapter}")
    return results
