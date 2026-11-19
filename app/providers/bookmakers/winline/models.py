"""Winline (winline.kz) — site-specific data model and dictionaries."""

from __future__ import annotations

from pydantic import BaseModel

from app.providers.bookmakers.common.models import ParsedEvent, ParsedMarket, ParsedOutcome

__all__ = ["ParsedEvent", "ParsedMarket", "ParsedOutcome", "WinlineConfig", "SPORT_IDS", "MARKET_IDS"]

# data-sport-id on the event container
SPORT_IDS = {
    "149": "football", "167": "tennis", "102": "hockey", "10": "basketball", "92": "futsal",
    "100248": "esports", "66": "volleyball", "58": "table_tennis", "79": "handball",
}
# data-market-id on outcome buttons → (market name, kind)
MARKET_IDS = {
    "1": ("1X2", "1x2"),
    "186": ("Победитель", "winner"),
    "219": ("Победитель (с ОТ)", "winner"),
    "406": ("Победитель (с ОТ и буллитами)", "winner"),
    "210": ("Победитель текущего сета", "other"),
    "202": ("Рынок #202 (теннис, второй блок)", "other"),
}


class WinlineConfig(BaseModel):
    home_url: str = "https://winline.kz/"
