"""Helpers for reading Russian-language bookmaker DOM text."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.timeutil import ALMATY, almaty  # noqa: E402  (Asia/Almaty)

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8,
    "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    # English pages (e.g. FONBET served in English outside Kazakhstan)
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_DMY = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")
_DMON = re.compile(r"(\d{1,2})\s+([a-zа-яё]{3,})", re.I)
_ODDS = re.compile(r"^\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*$")


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def parse_odds(text: str | None) -> Decimal | None:
    m = _ODDS.match(text or "")
    if not m:
        return None
    try:
        v = Decimal(m.group(1).replace(",", "."))
    except InvalidOperation:
        return None
    return v if v > 1 else None


def parse_ru_datetime(text: str, now: datetime | None = None) -> datetime | None:
    """'Сегодня 17:45', 'Завтра в 21:30', 'tomorrow, 03:30', '19 ноября 18:00', '21.11 19:00' → UTC."""
    t = clean(text).lower()
    tm = _TIME.search(t)
    if not tm:
        return None
    now_local = almaty(now or datetime.now(timezone.utc))
    day = None
    if "сегодня" in t or "today" in t:
        day = now_local.date()
    elif "послезавтра" in t or "day after tomorrow" in t:
        day = (now_local + timedelta(days=2)).date()
    elif "завтра" in t or "tomorrow" in t:
        day = (now_local + timedelta(days=1)).date()
    else:
        m = _DMON.search(t)
        if m and m.group(2)[:3] in MONTHS:
            d, mon = int(m.group(1)), MONTHS[m.group(2)[:3]]
            day = _with_year(now_local, d, mon)
        else:
            m = _DMY.search(t[: tm.start()] + " " + t[tm.end():])
            if m:
                d, mon = int(m.group(1)), int(m.group(2))
                if m.group(3):
                    y = int(m.group(3))
                    day = datetime(y + 2000 if y < 100 else y, mon, d).date()
                else:
                    day = _with_year(now_local, d, mon)
    if day is None:
        day = now_local.date()
    try:
        local = datetime(day.year, day.month, day.day, int(tm.group(1)), int(tm.group(2)), tzinfo=ALMATY)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def _with_year(now_local: datetime, d: int, mon: int):
    y = now_local.year
    # a date more than ~2 months in the past means the next year (Dec → Jan)
    if (mon, d) < (now_local.month, now_local.day) and now_local.month - mon > 2:
        y += 1
    try:
        return datetime(y, mon, d).date()
    except ValueError:
        return None


SPORT_WORDS = {
    "american_football": ("американский футбол", "american-football", "american football", "amfootball"),
    "aussie_rules": ("aussie-rules", "австралийский футбол"),
    "football": ("футбол", "football", "soccer", "footbal-star"),
    "tennis": ("теннис", "tennis"),
    "table_tennis": ("настольный теннис", "table_tennis", "table-tennis"),
    "hockey": ("хоккей", "hockey"),
    "basketball": ("баскетбол", "basketball"),
    "volleyball": ("волейбол", "volleyball", "volleybal"),
    "futsal": ("футзал", "futsal", "мини-футбол"),
    "handball": ("гандбол", "handball"),
    "mma": ("mma", "мма", "ufc"),
    "boxing": ("бокс", "boxing"),
    "esports": ("киберспорт", "esports", "e-sports", "cybersport"),
    "cs2": ("counter-strike", "cs2", "cs:go", "filled-cs"),
    "dota2": ("dota",),
    "lol": ("league of legends", "filled-lol", "-lol"),
    "valorant": ("valorant",),
    "sc2": ("starcraft", "filled-sc2"),
}


_CYBER_NICK = re.compile(r"\(([A-Za-z0-9_.\-]{2,24})\)\s*$")
_REAL_SUFFIX = {"люб", "u21", "u19", "u23", "res", "w", "жен", "рез", "до21", "до19", "до23"}


def cyber_sport(sport: str, home: str, away: str) -> str:
    """'Наполи (Rodja) — Рома (Bjela)' is an e-sports simulation, not real football: both sides carry a
    player nickname in Latin letters. Such events get their own sport (efootball, ebasketball, ...)
    so they can never be matched with the real teams' match."""
    hm, am = _CYBER_NICK.search(home or ""), _CYBER_NICK.search(away or "")
    if hm and am and hm.group(1).lower() not in _REAL_SUFFIX and am.group(1).lower() not in _REAL_SUFFIX:
        return sport if sport.startswith("e") and sport not in ("esports",) else f"e{sport}"
    return sport


def sport_slug(text: str) -> str:
    t = clean(text).lower()
    # longer names first so "настольный теннис" is not taken for "теннис"
    t = re.sub(r"^(st-|filled-|outline-)", "", t)
    for slug in ("table_tennis", "cs2", "dota2", "lol", "valorant", "sc2", "american_football", "aussie_rules", "futsal", "football", "tennis", "hockey", "basketball",
                 "volleyball", "handball", "mma", "boxing", "esports"):
        if any(w in t for w in SPORT_WORDS[slug]):
            return slug
    return t or "unknown"
