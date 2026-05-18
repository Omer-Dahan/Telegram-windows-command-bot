"""Protected emergency configuration: recovery PIN, signing key, recovery token.

Created on first run. Never transmitted over Telegram.
PIN is stored as PBKDF2-SHA256 (200k iterations). Config signing key protects panic_config.json.
"""
from __future__ import annotations

import gc
import hashlib
import hmac
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ...core.config import CONFIG, DATA_DIR
from ...shared.atomic_json import read_json, write_json

log = logging.getLogger(__name__)

_PATH = DATA_DIR / "panic_emergency.json"
_BAK  = DATA_DIR / "panic_emergency.json.bak"
_LOCK = threading.RLock()

_DEFAULT: dict = {
    "owner_chat_ids_backup": [],
    "recovery_token_hash": "",
    "recovery_pin_hash": "",
    "recovery_pin_salt": "",
    "failed_pin_attempts": 0,
    "lockout_until": None,
    "config_signing_key": "",
    "created_at": "",
}


# ── Startup ────────────────────────────────────────────────────────────────

def initialize_if_missing() -> None:
    """Generate keys on first run. Called from handlers.register()."""
    with _LOCK:
        data = _load_raw()
        changed = False
        if not data.get("config_signing_key"):
            data["config_signing_key"] = os.urandom(32).hex()
            changed = True
        if not data.get("recovery_token_hash"):
            token = os.urandom(32).hex()
            data["recovery_token_hash"] = _sha256(token)
            # Log token once so owner can note it (won't appear in logs again)
            log.warning("PANIC: Generated recovery token. Save this securely: %s", token)
            changed = True
        if not data.get("owner_chat_ids_backup"):
            data["owner_chat_ids_backup"] = list(CONFIG.all_owner_chat_ids)
            changed = True
        if not data.get("created_at"):
            data["created_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
        if changed:
            _save_raw(data)


def load() -> dict:
    """Load with fallback to .bak. Returns merged over _DEFAULT."""
    with _LOCK:
        data = _load_raw()
        merged = dict(_DEFAULT)
        merged.update(data)
        return merged


def get_signing_key() -> str:
    return load().get("config_signing_key", "")


# ── PIN management ──────────────────────────────────────────────────────────

def set_pin(pin: str) -> None:
    """Hash pin with PBKDF2-SHA256 and persist. Clears pin from memory."""
    salt = os.urandom(16)
    digest = _pbkdf2(pin, salt)
    try:
        with _LOCK:
            data = _load_raw()
            data["recovery_pin_hash"] = digest.hex()
            data["recovery_pin_salt"] = salt.hex()
            data["failed_pin_attempts"] = 0
            data["lockout_until"] = None
            _save_raw(data)
    finally:
        del pin
        gc.collect()


def verify_pin(pin: str) -> bool:
    """Verify pin. Increments failed counter; enforces lockout. Returns True on match."""
    try:
        with _LOCK:
            data = _load_raw()
            if _is_locked_out(data):
                log.warning("PANIC: PIN attempt while locked out")
                return False
            stored_hash = data.get("recovery_pin_hash", "")
            stored_salt = data.get("recovery_pin_salt", "")
            if not stored_hash or not stored_salt:
                log.warning("PANIC: No PIN set — verify_pin returning False")
                return False
            salt = bytes.fromhex(stored_salt)
            candidate = _pbkdf2(pin, salt)
            ok = hmac.compare_digest(candidate.hex(), stored_hash)
            if ok:
                data["failed_pin_attempts"] = 0
                data["lockout_until"] = None
                log.info("PANIC: PIN verification success")
            else:
                data["failed_pin_attempts"] = data.get("failed_pin_attempts", 0) + 1
                log.warning("PANIC: PIN verification failed (attempt %d)",
                            data["failed_pin_attempts"])
                # Lock out after 5 failures
                if data["failed_pin_attempts"] >= 5:
                    until = datetime.now() + timedelta(minutes=30)
                    data["lockout_until"] = until.isoformat(timespec="seconds")
                    log.warning("PANIC: PIN locked out until %s", data["lockout_until"])
            _save_raw(data)
            return ok
    finally:
        del pin
        gc.collect()


def is_locked_out() -> bool:
    with _LOCK:
        return _is_locked_out(_load_raw())


def has_pin() -> bool:
    return bool(load().get("recovery_pin_hash"))


def verify_recovery_token(token: str) -> bool:
    data = load()
    stored = data.get("recovery_token_hash", "")
    if not stored:
        return False
    candidate = _sha256(token)
    return hmac.compare_digest(candidate, stored)


# ── Internal ────────────────────────────────────────────────────────────────

def _load_raw() -> dict:
    data = read_json(_PATH, {})
    if not data:
        data = read_json(_BAK, {})
        if data:
            log.warning("PANIC: Loaded emergency config from backup")
    return data


def _save_raw(data: dict) -> None:
    if _PATH.exists():
        import shutil
        shutil.copy2(_PATH, _BAK)
    write_json(_PATH, data)


def _is_locked_out(data: dict) -> bool:
    until_str = data.get("lockout_until")
    if not until_str:
        return False
    try:
        until = datetime.fromisoformat(until_str)
        return datetime.now() < until
    except ValueError:
        return False


def _pbkdf2(pin: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 200_000)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
