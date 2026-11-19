"""FONBET (fonbet.kz) — site-specific data model and dictionaries."""

from __future__ import annotations

from pydantic import BaseModel

from app.providers.bookmakers.common.models import ParsedEvent, ParsedMarket, ParsedOutcome

__all__ = ["ParsedEvent", "ParsedMarket", "ParsedOutcome", "FonbetConfig", "SPORT_PAGES", "COLUMN_GROUPS", "SECOND_TIME"]

# checked from the server on 19.11.2026: these slugs list their own sport; unknown slugs (mma,
# american-football, boxing) silently show the football list instead and are not used.
SPORT_PAGES = {"football": "football", "tennis": "tennis", "hockey": "hockey", "basketball": "basketball",
               "volleyball": "volleyball", "esports": "esports", "handball": "handball", "futsal": "futsal"}

# competition header column label → (group, outcome name). Russian and English pages
# (FONBET serves English outside Kazakhstan). English repeats "1"/"2" for the
# handicap columns, so a second "1"/"2" in one header means handicap (see parser.columns_to_groups).
COLUMN_GROUPS = {
    "1": ("main", "П1"), "х": ("main", "Х"), "x": ("main", "Х"), "2": ("main", "П2"),
    "фора 1": ("handicap", "Ф1"), "фора 2": ("handicap", "Ф2"),
    "handicap 1": ("handicap", "Ф1"), "handicap 2": ("handicap", "Ф2"),
    "тотал": ("total_line", None), "total": ("total_line", None),
    "б": ("total", "Больше"), "o": ("total", "Больше"), "м": ("total", "Меньше"), "u": ("total", "Меньше"),
}
SECOND_TIME = {"1": ("handicap", "Ф1"), "2": ("handicap", "Ф2")}


class FonbetConfig(BaseModel):
    sports: list[str] = list(SPORT_PAGES)
