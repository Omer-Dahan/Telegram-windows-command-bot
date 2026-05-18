"""Confidence scoring engine for trigger fusion.

Score is in-memory only (resets on restart by design — avoids stale state).
Each trigger contributes its weight. Score decays at decay_per_minute rate.
Decay is applied lazily on every read/write call.
"""
from __future__ import annotations

import logging
import threading
import time

from . import config_store

log = logging.getLogger(__name__)

_LOCK = threading.RLock()

# trigger_name → (score_contribution, monotonic_timestamp)
_scores: dict[str, tuple[float, float]] = {}
_last_decay_time: float = time.monotonic()


def _apply_decay() -> None:
    """Subtract decay from each score entry. Remove entries that reach 0. NOT thread-safe alone."""
    global _last_decay_time
    now = time.monotonic()
    elapsed_minutes = (now - _last_decay_time) / 60.0
    _last_decay_time = now
    if elapsed_minutes <= 0:
        return
    cfg = config_store.get_score_config()
    decay_rate = cfg.get("decay_per_minute", 5)
    decay_amount = elapsed_minutes * decay_rate
    to_remove = []
    for trigger, (score, ts) in list(_scores.items()):
        new_score = max(0.0, score - decay_amount)
        if new_score <= 0:
            to_remove.append(trigger)
        else:
            _scores[trigger] = (new_score, ts)
    for t in to_remove:
        del _scores[t]


def _total_raw() -> float:
    return sum(s for s, _ in _scores.values())


def add_trigger_score(trigger: str) -> float:
    """Add this trigger's configured weight to the score. Returns new total."""
    with _LOCK:
        _apply_decay()
        cfg = config_store.get_score_config()
        weight = cfg.get("trigger_weights", {}).get(trigger, 10)
        existing = _scores.get(trigger, (0.0, time.monotonic()))[0]
        new_contribution = min(existing + weight, 100.0)
        _scores[trigger] = (new_contribution, time.monotonic())
        total = min(_total_raw(), 100.0)
        log.debug("PANIC scoring: +%s for %r → total=%.1f", weight, trigger, total)
        return total


def remove_trigger_score(trigger: str) -> float:
    """Remove this trigger's score contribution (condition resolved). Returns new total."""
    with _LOCK:
        _apply_decay()
        _scores.pop(trigger, None)
        total = _total_raw()
        log.debug("PANIC scoring: removed %r → total=%.1f", trigger, total)
        return total


def get_total() -> float:
    with _LOCK:
        _apply_decay()
        return min(_total_raw(), 100.0)


def get_trigger_score(trigger: str) -> float:
    with _LOCK:
        _apply_decay()
        return _scores.get(trigger, (0.0, 0.0))[0]


def get_threshold_status() -> str:
    """Return the threshold bucket name for the current score."""
    cfg = config_store.get_score_config()
    total = get_total()
    if total >= cfg.get("threshold_level3", 100):
        return "LEVEL3"
    if total >= cfg.get("threshold_level2", 80):
        return "LEVEL2"
    if total >= cfg.get("threshold_level1", 60):
        return "LEVEL1"
    if total >= cfg.get("threshold_warning", 30):
        return "WARNING"
    return "NORMAL"


def get_snapshot() -> dict:
    """Snapshot for the UI score panel."""
    with _LOCK:
        _apply_decay()
        cfg = config_store.get_score_config()
        total = min(_total_raw(), 100.0)
        breakdown = {t: round(s, 1) for t, (s, _) in _scores.items()}
        return {
            "total": round(total, 1),
            "breakdown": breakdown,
            "decay_per_min": cfg.get("decay_per_minute", 5),
            "thresholds": {
                "warning": cfg.get("threshold_warning", 30),
                "level1":  cfg.get("threshold_level1", 60),
                "level2":  cfg.get("threshold_level2", 80),
                "level3":  cfg.get("threshold_level3", 100),
            },
            "status": get_threshold_status(),
        }


def reset() -> None:
    """Clear all scores (used by recovery and safe-mode exit)."""
    global _last_decay_time
    with _LOCK:
        _scores.clear()
        _last_decay_time = time.monotonic()
        log.info("PANIC: Score reset to 0")
