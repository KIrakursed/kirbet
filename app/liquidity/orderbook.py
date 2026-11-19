"""Order-book liquidity helpers: VWAP, slippage, fillable size."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain import ZERO, Level


@dataclass
class FillEstimate:
    requested: Decimal
    filled: Decimal
    vwap: Decimal
    cost: Decimal
    worst_price: Decimal
    slippage: Decimal  # vwap - best price
    partial: bool


def vwap_buy(levels: list[Level], size: Decimal) -> FillEstimate:
    """Walk ask levels (sorted best first) buying ``size`` units."""
    if not levels or size <= 0:
        return FillEstimate(size, ZERO, ZERO, ZERO, ZERO, ZERO, True)
    left, cost, filled, worst = size, ZERO, ZERO, levels[0].price
    for lvl in levels:
        if left <= 0:
            break
        t = min(left, lvl.size)
        cost += t * lvl.price
        filled += t
        left -= t
        worst = lvl.price
    vwap = cost / filled if filled else ZERO
    return FillEstimate(size, filled, vwap, cost, worst, vwap - levels[0].price, filled < size)


def depth(levels: list[Level], max_price: Decimal | None = None) -> Decimal:
    return sum((lv.size for lv in levels if max_price is None or lv.price <= max_price), ZERO)


def depth_notional(levels: list[Level], max_price: Decimal | None = None) -> Decimal:
    return sum((lv.size * lv.price for lv in levels if max_price is None or lv.price <= max_price), ZERO)
