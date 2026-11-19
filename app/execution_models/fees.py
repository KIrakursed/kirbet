"""Platform fee formulas (verified against official docs, Nov 2026).

Polymarket  (docs.polymarket.com/trading/fees):
    fee = shares * rate * (p * (1 - p)) ** exponent   (taker only)
    rate/exponent come from the market's ``feeSchedule``.
Kalshi      (kalshi.com fee schedule, series ``fee_type``/``fee_multiplier``):
    taker fee = ceil_to_cent(0.07 * multiplier * C * P * (1 - P))
Exchange    (Betfair-style): commission on net winnings.
Bookmaker   : no explicit fee, margin is inside the odds.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_UP, Decimal

from app.domain import ONE, ZERO

CENT = Decimal("0.01")
FEE_Q = Decimal("0.0001")


def polymarket_fee(shares: Decimal, price: Decimal, params: dict) -> Decimal:
    if not params.get("enabled", True):
        return ZERO
    rate = Decimal(str(params.get("rate", "0")))
    exponent = int(params.get("exponent", 1))
    if rate <= 0 or shares <= 0:
        return ZERO
    base = price * (ONE - price)
    fee = shares * rate * (base**exponent)
    return fee.quantize(FEE_Q, rounding=ROUND_UP)


def kalshi_fee(contracts: Decimal, price: Decimal, params: dict) -> Decimal:
    rate = Decimal(str(params.get("rate", "0.07")))
    mult = Decimal(str(params.get("multiplier", "1")))
    if contracts <= 0:
        return ZERO
    fee = rate * mult * contracts * price * (ONE - price)
    return fee.quantize(CENT, rounding=ROUND_CEILING)


def fee_for(model: str, units: Decimal, price: Decimal, params: dict) -> Decimal:
    if model == "polymarket":
        return polymarket_fee(units, price, params)
    if model == "kalshi":
        return kalshi_fee(units, price, params)
    if model == "flat_pct_of_cost":
        return (units * price * Decimal(str(params.get("pct", "0")))).quantize(
            FEE_Q, rounding=ROUND_UP
        )
    return ZERO
