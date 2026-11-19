"""Winline → domain markets (1X2 / winner only feed the arbitrage scanner)."""

from __future__ import annotations

from app.domain import Market
from app.providers.bookmakers.common import normalizer as common
from app.providers.bookmakers.winline.models import ParsedEvent


def normalize(events: list[ParsedEvent], *, include_live: bool = False) -> list[Market]:
    return common.to_domain(events, "winline", include_live=include_live)
