"""Parimatch (parimatch.kz) — site-specific data model and dictionaries."""

from __future__ import annotations

from pydantic import BaseModel

from app.providers.bookmakers.common.models import ParsedEvent, ParsedMarket, ParsedOutcome

__all__ = ["ParsedEvent", "ParsedMarket", "ParsedOutcome", "ParimatchConfig", "SPORT_PAGES"]

# robots.txt explicitly allows /ru/football/ and /ru/events/; the other sport
# pages are not disallowed. The client re-checks robots.txt on every run.
SPORT_PAGES = {"football": "https://parimatch.kz/ru/football/", "tennis": "https://parimatch.kz/ru/tennis/",
               "hockey": "https://parimatch.kz/ru/ice-hockey/", "basketball": "https://parimatch.kz/ru/basketball/"}


class ParimatchConfig(BaseModel):
    sports: list[str] = list(SPORT_PAGES)
