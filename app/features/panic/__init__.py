"""Panic Mode / Anti-Theft Protection feature.

Exposes register(app) and match_text(text, chat_id) for the FSD feature registry.
"""
from .handlers import match_text, register

__all__ = ["register", "match_text"]
