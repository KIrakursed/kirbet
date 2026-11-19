from __future__ import annotations

from app.providers.aggregators.the_odds_api import TheOddsApiProvider
from app.providers.base import BaseProvider
from app.providers.bookmakers.fonbet.provider import FonbetProvider
from app.providers.bookmakers.kz import RESEARCH, KzBookmakerProvider
from app.providers.bookmakers.oinabet_kz import OinabetKzProvider
from app.providers.bookmakers.olimpbet.provider import OlimpbetProvider
from app.providers.bookmakers.onexbet_kz import OneXBetKzProvider
from app.providers.bookmakers.parimatch.provider import ParimatchProvider
from app.providers.bookmakers.tennisi.provider import TennisiProvider
from app.providers.bookmakers.ubet_kz import UbetKzProvider
from app.providers.bookmakers.winline.provider import WinlineProvider
from app.providers.exchanges.betfair import BetfairProvider
from app.providers.prediction_markets.kalshi import KalshiProvider
from app.providers.prediction_markets.polymarket import PolymarketProvider


def build_providers() -> dict[str, BaseProvider]:
    provs: list[BaseProvider] = [
        PolymarketProvider(),
        KalshiProvider(),
        TheOddsApiProvider(),
        BetfairProvider(),
        OneXBetKzProvider(),
        UbetKzProvider(),
        OinabetKzProvider(),
        *build_dom_bookmakers().values(),
    ]
    provs += [KzBookmakerProvider(k) for k in RESEARCH]
    return {p.name: p for p in provs}


def build_dom_bookmakers() -> dict[str, BaseProvider]:
    """Bookmakers read from the public rendered DOM via Zendriver (one shared Chrome)."""
    provs = [OlimpbetProvider(), FonbetProvider(), ParimatchProvider(), WinlineProvider(), TennisiProvider()]
    return {p.name: p for p in provs}


DOM_BOOKMAKERS = ("olimpbet", "fonbet", "parimatch", "winline", "tennisi")
DATA_PROVIDERS = ("polymarket", "kalshi", "the_odds_api", "betfair", "1xbet", "ubet", "oinabet", *DOM_BOOKMAKERS)
KZ_BOOKMAKERS = ("1xbet", "ubet", "olimpbet", "fonbet", "parimatch", "winline", "tennisi", "oinabet")
