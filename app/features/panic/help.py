"""Panic Mode help guide — creates a Telegraph article (one-time) and caches the URL.

Content is bilingual: Hebrew + English.
Telegraph API is anonymous (no login needed).
URL is stored in panic_config.json under _help_url.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from . import config_store

log = logging.getLogger(__name__)

# Fallback URL if Telegraph is unavailable (empty = show inline)
_FALLBACK_URL = ""

_HELP_NODES: list[dict] = [
    {"tag": "h3", "children": ["🚨 Panic Mode — מדריך שימוש / User Guide"]},
    {"tag": "p", "children": [
        {"tag": "b", "children": ["שפה: עברית + English"]},
    ]},
    {"tag": "hr"},

    # Hebrew section
    {"tag": "h4", "children": ["🇮🇱 עברית"]},
    {"tag": "h4", "children": ["מה זה Panic Mode?"]},
    {"tag": "p", "children": [
        "Panic Mode הוא מודול הגנה לגיטימי שמנטר את המחשב שלך ומגיב אוטומטית כשמתגלה סכנה. "
        "המערכת מיועדת אך ורק להגנה על המכשיר שלך — ללא הצפנת קבצים, ללא נזק, ללא יצירת מניפולציות."
    ]},

    {"tag": "h4", "children": ["חימוש / פריקה"]},
    {"tag": "p", "children": [
        "לחץ על כפתור 'DISARMED' במסך הראשי כדי לחמש את המערכת. "
        "לאחר החימוש, המוניטורים יתחילו לעבוד ברקע."
    ]},

    {"tag": "h4", "children": ["🔁 מצבי המערכת (State Machine)"]},
    {"tag": "p", "children": [
        "המערכת עובדת עם מצבים מוגדרים:"
    ]},
    {"tag": "ul", "children": [
        {"tag": "li", "children": [{"tag": "b", "children": ["NORMAL"]}, " — ניטור פעיל, אין איום"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["WARNING"]}, " — ניקוד מעל סף האזהרה, תקופת חסד פועלת"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["LEVEL1"]}, " — התראה + נעילת מסך"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["LEVEL2"]}, " — סגירת תהליכים, ניקוי דפדפן, VeraCrypt"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["LEVEL3"]}, " — ניתוק רשת, שינה/כיבוי"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["LOCKDOWN"]}, " — נעילה מלאה, שורדת ריסטארט"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["SAFE_MODE"]}, " — הגנה מפני לולאת trigger שגויה"]},
    ]},

    {"tag": "h4", "children": ["📊 ניקוד (Confidence Scoring)"]},
    {"tag": "p", "children": [
        "כל טריגר תורם נקודות לציון הכולל. הציון דועך עם הזמן (5 נק' לדקה כברירת מחדל). "
        "כאשר מספר טריגרים מופעלים יחד, הציון עולה ומגיע לסף גבוה יותר."
    ]},
    {"tag": "ul", "children": [
        {"tag": "li", "children": ["30 נק' = אזהרה (WARNING)"]},
        {"tag": "li", "children": ["60 נק' = רמה 1"]},
        {"tag": "li", "children": ["80 נק' = רמה 2"]},
        {"tag": "li", "children": ["100 נק' = רמה 3"]},
    ]},

    {"tag": "h4", "children": ["⏳ תקופת חסד (Grace Period)"]},
    {"tag": "p", "children": [
        "לפני שמתחיל ביצוע, נשלחת הודעת אזהרה עם 3 כפתורים:"
    ]},
    {"tag": "ul", "children": [
        {"tag": "li", "children": [{"tag": "b", "children": ["❌ בטל"]}, " — מבטל את ה-trigger"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["⚡ הפעל עכשיו"]}, " — מדלג על ספירת לאחור"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["🙈 התעלם פעם אחת"]}, " — מתעלם לשעה"]},
    ]},

    {"tag": "h4", "children": ["🎯 טריגרים"]},
    {"tag": "ul", "children": [
        {"tag": "li", "children": [{"tag": "b", "children": ["Wi-Fi Loss"]}, " — התנתק מ-Wi-Fi מהימן יותר מ-X דקות"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["Location Change"]}, " — שינוי גדול ברשתות הנראות / IP ציבורי"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["USB Change"]}, " — חיבור התקן USB לא מוכר / הסרת התקן מהימן"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["Power Disconnect"]}, " — ניתוק ממטען"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["Bluetooth Loss"]}, " — מכשיר BT מהימן נעלם מהטווח"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["Dead-Man Switch"]}, " — לא הייתה אינטראקציה עם הבוט מעל X שעות"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["Failed Login"]}, " — יותר מ-X ניסיונות כניסה שגויים"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["Lid Open/Close"]}, " — פתיחה/סגירה של מכסה הלפטופ"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["Boot Source"]}, " — אתחול חריג (USB boot, network boot)"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["Manual (/panic)"]}, " — הפעלה ידנית"]},
    ]},

    {"tag": "h4", "children": ["🔒 פעולות הגנה"]},
    {"tag": "ul", "children": [
        {"tag": "li", "children": [{"tag": "b", "children": ["L1:"]}, " התראה לטלגרם + נעילת מסך"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["L2:"]}, " צילום forensic + סגירת תהליכים + ניקוי דפדפן + VeraCrypt + RAM + קובץ temp + סקריפט מותאם"]},
        {"tag": "li", "children": [{"tag": "b", "children": ["L3:"]}, " ניתוק רשת + שינה/כיבוי"]},
    ]},

    {"tag": "h4", "children": ["🔓 שחזור (Recovery)"]},
    {"tag": "p", "children": [
        "שלח /recover בטלגרם. הבוט יחזיר את המתאמים הרשתיים ויאפס את המצב."
    ]},
    {"tag": "p", "children": [
        "שחזור offline: שמור קובץ panic_recovery.json ב-USB עם {\"pin\": \"<PIN שלך>\", \"timestamp\": \"<ISO8601>\"}"
    ]},

    {"tag": "h4", "children": ["🔑 PIN שחזור"]},
    {"tag": "p", "children": [
        "הגדר PIN דרך: פאניק מוד ← שחזור ← הגדר PIN. "
        "ה-PIN מאוחסן כ-hash בלבד, לעולם לא בטקסט ברור."
    ]},

    {"tag": "h4", "children": ["⌨️ קיצור מקלדת"]},
    {"tag": "p", "children": [
        "הגדר קיצור מקלדת גלובלי (כגון Ctrl+Alt+End) לפעלה ידנית. "
        "הקיצור פועל גם כשהחלון מוקטן."
    ]},

    {"tag": "h4", "children": ["🧪 מצב בדיקה"]},
    {"tag": "p", "children": [
        "הפעל Test Mode לפני שמגדיר פעולות אגרסיביות. "
        "במצב בדיקה נשלחת התראה לטלגרם אך לא מתבצעות פעולות הרסניות."
    ]},

    {"tag": "hr"},

    # English section
    {"tag": "h4", "children": ["🇺🇸 English"]},
    {"tag": "h4", "children": ["What is Panic Mode?"]},
    {"tag": "p", "children": [
        "Panic Mode is a legitimate defensive security module that monitors your PC and responds "
        "automatically when a threat is detected. It is designed exclusively for personal device "
        "protection — no file encryption, no damage, no malware behavior."
    ]},

    {"tag": "h4", "children": ["Arming / Disarming"]},
    {"tag": "p", "children": [
        "Tap the 'DISARMED' button on the main screen to arm the system. "
        "Once armed, monitors will run in the background."
    ]},

    {"tag": "h4", "children": ["📊 Confidence Scoring"]},
    {"tag": "p", "children": [
        "Each trigger contributes a weighted score. The score decays over time (5 pts/min by default). "
        "Multiple simultaneous triggers increase the score toward higher action thresholds."
    ]},

    {"tag": "h4", "children": ["⏳ Grace Period"]},
    {"tag": "p", "children": [
        "Before executing actions, a warning message is sent with 3 buttons: "
        "Cancel (abort), Trigger Now (skip countdown), Ignore Once (suppress for 1 hour). "
        "The grace period auto-cancels if the threat resolves."
    ]},

    {"tag": "h4", "children": ["🔓 Recovery"]},
    {"tag": "p", "children": [
        "Send /recover in Telegram. The bot will re-enable network adapters and reset the state."
    ]},
    {"tag": "p", "children": [
        "Offline recovery: drop panic_recovery.json on a USB drive with "
        "{\"pin\": \"<your-pin>\", \"timestamp\": \"<ISO8601>\"}."
    ]},

    {"tag": "h4", "children": ["💡 Tips"]},
    {"tag": "ul", "children": [
        {"tag": "li", "children": ["Always enable Test Mode first to verify your configuration."]},
        {"tag": "li", "children": ["Set a recovery PIN before enabling L3 actions (network disable/shutdown)."]},
        {"tag": "li", "children": ["Add your home Wi-Fi as a trusted SSID before enabling the Wi-Fi Loss trigger."]},
        {"tag": "li", "children": ["The Dead-Man Switch resets on every message you send to the bot."]},
        {"tag": "li", "children": ["Safe Mode auto-activates after 5 panics in 1 hour to prevent false-positive loops."]},
    ]},

    {"tag": "p", "children": [
        {"tag": "b", "children": ["⚠️ Important: "]},
        "This module performs only local defensive actions on your own device. "
        "It does NOT encrypt files, does NOT spread to other systems, and does NOT modify Windows system files."
    ]},
]


def _telegraph_post(endpoint: str, data: dict) -> dict:
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegra.ph/{endpoint}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def create_help_article() -> str:
    """Create a Telegraph article with the help guide. Returns the URL."""
    try:
        acc = _telegraph_post("createAccount", {
            "short_name": "PanicModeBot",
            "author_name": "Panic Mode",
        })
        token = acc["result"]["access_token"]

        page = _telegraph_post("createPage", {
            "access_token": token,
            "title": "Panic Mode — Guide מדריך",
            "author_name": "Panic Mode Bot",
            "content": _HELP_NODES,
        })
        url = page["result"]["url"]
        log.info("PANIC help: Telegraph article created: %s", url)
        return url
    except Exception as e:
        log.warning("PANIC help: Telegraph creation failed: %s", e)
        return ""


def get_help_url() -> str:
    """Return cached help URL, or create it on first call."""
    cfg = config_store.load()
    cached = cfg.get("_help_url", "")
    if cached:
        return cached
    url = create_help_article()
    if url:
        cfg["_help_url"] = url
        config_store.save(cfg)
    return url
