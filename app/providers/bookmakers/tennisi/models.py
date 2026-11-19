"""TENNISI (tennisi.kz) — site-specific data model."""

from __future__ import annotations

from pydantic import BaseModel

from app.providers.bookmakers.common.models import ParsedEvent, ParsedMarket, ParsedOutcome

__all__ = ["ParsedEvent", "ParsedMarket", "ParsedOutcome", "TennisiConfig"]


class TennisiConfig(BaseModel):
    home_url: str = "https://tennisi.kz/"
