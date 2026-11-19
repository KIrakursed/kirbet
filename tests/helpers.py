"""Builders for unit tests (synthetic quotes are used ONLY to test maths, never shown as real)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.domain import (
    Leg,
    Level,
    Market,
    MarketType,
    Outcome,
    PlatformType,
    PriceKind,
    Quote,
    RetirementRule,
    SettlementProfile,
    VoidRule,
)
from app.normalization.names import basic

NOW = datetime.now(timezone.utc)


def bq(price, size="1000", platform="pm", fee=None, side="YES", iid=None, levels=None, min_size="0"):
    params = {"unit_step": "0.01"}
    model = "none"
    if fee == "pm":
        model, params = "polymarket", {"enabled": True, "rate": "0.05", "exponent": 1, "unit_step": "0.01"}
    elif fee == "kalshi":
        model, params = "kalshi", {"rate": "0.07", "multiplier": "1", "unit_step": "1"}
    lv = levels or [Level(D(str(price)), D(str(size)))]
    return Quote(platform=platform, kind=PriceKind.BINARY_CONTRACT, outcome_label=iid or f"{platform}{price}",
                 instrument_id=iid or f"{platform}:{price}:{side}", best_price=lv[0].price, levels=lv,
                 fee_model=model, fee_params=params, side=side, min_size=D(min_size))


def oq(odds, platform="bk", max_stake=None, kind=PriceKind.DECIMAL_ODDS, commission=None, size="1e9", iid=None):
    params = {"unit_step": "1"}
    if commission is not None:
        params["commission"] = str(commission)
        params["unit_step"] = "0.01"
    return Quote(platform=platform, kind=kind, outcome_label=str(odds), instrument_id=iid or f"{platform}:{odds}:{kind.value}",
                 best_price=D(str(odds)), levels=[Level(D(str(odds)), D(str(size)))],
                 max_stake=D(str(max_stake)) if max_stake else None, fee_params=params)


def leg(q, *covers):
    return Leg(q, frozenset(covers))


TENNIS_PM = SettlementProfile(VoidRule.FIFTY_FIFTY, RetirementRule.ADVANCING_PLAYER)
TENNIS_K = SettlementProfile(VoidRule.FAIR_PRICE, RetirementRule.ADVANCING_PLAYER)
TENNIS_BK = SettlementProfile(VoidRule.REFUND, RetirementRule.VOID)


def h2h_market(platform, a, b, pa, pb, *, start=None, category="tennis", mtype=MarketType.MATCH_WINNER,
               period="match", settlement=TENNIS_PM, best_of=None, draw=False, fee=None):
    start = start or NOW + timedelta(hours=5)
    s = settlement if not draw else SettlementProfile(settlement.walkover, settlement.retirement, True)
    outs = [
        Outcome(basic(a), a, [bq(pa, platform=platform, iid=f"{platform}:{a}", fee=fee)]),
        Outcome(basic(b), b, [bq(pb, platform=platform, iid=f"{platform}:{b}", fee=fee)]),
    ]
    return Market(platform=platform, platform_type=PlatformType.PREDICTION_MARKET, market_id=f"{platform}-{a}-{b}-{period}",
                  event_id="e", title=f"{a} vs {b}", category=category, market_type=mtype, outcomes=outs,
                  start_time=start, participants=(a, b), settlement=s, period=period, best_of=best_of)
