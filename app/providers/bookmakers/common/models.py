"""Pydantic models shared by the browser (DOM) bookmaker adapters."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ODDS_MIN = Decimal("1.001")
ODDS_MAX = Decimal("1000")


class ParsedOutcome(BaseModel):
    name: str = Field(min_length=1)  # П1 / Х / П2 / Больше / Ф1 (-1) ...
    odds: Decimal
    param: str | None = None  # line value for totals / handicaps

    @field_validator("odds")
    @classmethod
    def _real_odds(cls, v: Decimal) -> Decimal:
        if not (ODDS_MIN <= v <= ODDS_MAX):
            raise ValueError(f"коэффициент вне диапазона: {v}")
        return v


class ParsedMarket(BaseModel):
    name: str = Field(min_length=1)  # "1X2", "Победитель", "Тотал 2.5", "Фора"
    kind: Literal["1x2", "winner", "total", "handicap", "other"] = "other"
    outcomes: list[ParsedOutcome] = Field(min_length=2)


class ParsedEvent(BaseModel):
    bookmaker: str
    external_id: str
    sport: str = Field(min_length=1)
    tournament: str = ""
    home: str = Field(min_length=1)
    away: str = Field(min_length=1)
    start_text: str = ""
    start_time: datetime | None = None  # UTC; None when the source's zone could not be established
    timezone_unknown: bool = False  # the page's local time zone was not Asia/Almaty → time not guessed
    is_live: bool = False
    url: str = ""
    markets: list[ParsedMarket] = Field(default_factory=list)
    fetched_at: datetime

    @property
    def name(self) -> str:
        return f"{self.home} — {self.away}"

    def is_complete(self) -> bool:
        """Has every field the task requires for a *real* data point."""
        return bool(self.name and self.home and self.away and self.sport and self.fetched_at and any(
            m.name and m.outcomes and all(o.name and o.odds for o in m.outcomes) for m in self.markets))


Status = Literal["OK", "NO_DATA", "BLOCKED", "ERROR"]


class AdapterReport(BaseModel):
    bookmaker: str
    status: Status
    http_status: int | None = None
    events: int = 0
    markets: int = 0
    sample: str = ""  # "Шелтон Бен — Ходар Рафаэль | теннис | Победитель | П1 = 1.35"
    timestamp: datetime
    reason: str = ""
    urls: list[str] = Field(default_factory=list)
    load_ms: float | None = None
