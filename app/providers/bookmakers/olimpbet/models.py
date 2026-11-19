"""OLIMPBET (olimpbet.kz) — site-specific data model and dictionaries."""

from __future__ import annotations

from pydantic import BaseModel

from app.providers.bookmakers.common.models import ParsedEvent, ParsedMarket, ParsedOutcome

__all__ = ["ParsedEvent", "ParsedMarket", "ParsedOutcome", "OlimpbetConfig", "TOP_TABS"]

# sport tabs of the "Топ события" block on the home page:
# data-testid="main-top-events-sports-tab-<id>" → sport
TOP_TABS = {"100": "football", "101": "tennis", "103": "hockey", "102": "basketball", "105": "esports",
            "1001": "mma"}


class OlimpbetConfig(BaseModel):
    home_url: str = "https://olimpbet.kz/"
    tabs: dict[str, str] = TOP_TABS
