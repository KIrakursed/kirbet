"""Execution models: convert a desired payout into real cash flows per leg.

Every model answers one question: *to receive payout P (leg currency) if this
leg wins, how much money must I commit right now, walking the real order
book / stake limits, including fees?*  The answer is a LegFill or None if
the available volume is insufficient.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from app.domain import ONE, ZERO, Leg, LegFill, Level, PriceKind
from app.execution_models.fees import fee_for


def _step(leg: Leg) -> Decimal:
    return Decimal(str(leg.quote.fee_params.get("unit_step", "0.01")))


def round_units(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _commission(leg: Leg) -> Decimal:
    return Decimal(str(leg.quote.fee_params.get("commission", "0")))


def unit_payout(leg: Leg, price: Decimal) -> Decimal:
    """Payout per 1 unit of *stake* (odds kinds) or per share (binary)."""
    k = leg.quote.kind
    c = _commission(leg)
    if k == PriceKind.BINARY_CONTRACT:
        return ONE
    if k == PriceKind.DECIMAL_ODDS:
        return price
    if k == PriceKind.EXCHANGE_BACK:
        return ONE + (price - ONE) * (ONE - c)
    if k == PriceKind.EXCHANGE_LAY:
        # stake S = backer's stake we accept. liability S*(L-1) is our cost,
        # if the selection loses we get liability back + S*(1-c)
        return price - c
    raise ValueError(k)


def unit_cost(leg: Leg, price: Decimal) -> Decimal:
    k = leg.quote.kind
    if k == PriceKind.BINARY_CONTRACT:
        return price
    if k == PriceKind.EXCHANGE_LAY:
        return price - ONE
    return ONE  # back stakes


def max_payout(leg: Leg) -> Decimal | None:
    """Maximum payout obtainable from visible depth / stake limits."""
    q = leg.quote
    if not q.levels:
        return None if not q.depth_known else ZERO
    total = sum((lvl.size * unit_payout(leg, lvl.price) for lvl in q.levels), ZERO)
    if q.max_stake is not None and q.kind != PriceKind.BINARY_CONTRACT:
        total = min(total, q.max_stake * unit_payout(leg, q.best_price))
    return total


def breakpoints(leg: Leg) -> list[Decimal]:
    """Payout values where the marginal price changes (end of each level)."""
    out, acc = [], ZERO
    for lvl in leg.quote.levels:
        acc += lvl.size * unit_payout(leg, lvl.price)
        out.append(acc)
    return out


def fill_for_payout(leg: Leg, payout: Decimal) -> LegFill | None:
    """Walk the book to obtain ``payout``. Returns None if not enough depth,
    below platform minimum or above a known stake limit."""
    q = leg.quote
    step = _step(leg)
    levels = list(q.levels)
    if not levels:
        if q.depth_known:
            return None
        # depth unknown (bookmaker without limit info): best price only,
        # caller must surface a warning about unverified limits
        levels = [Level(q.best_price, Decimal("1e12"))]

    binary = q.kind == PriceKind.BINARY_CONTRACT
    if binary:
        target_units = round_units(payout, step)
    else:
        # stake needed if everything filled at the best level; refined below
        target_units = None
    if binary and target_units <= 0:
        return None

    units = ZERO
    cost = ZERO
    fee = ZERO
    pay = ZERO
    worst = levels[0].price
    remaining_pay = payout
    for lvl in levels:
        up = unit_payout(leg, lvl.price)
        if up <= 0:
            continue
        if binary:
            want = target_units - units
        else:
            want = round_units(remaining_pay / up, step)
            if want <= 0 and remaining_pay > 0:
                want = step
        if want <= 0:
            break
        take = min(want, lvl.size)
        if take <= 0:
            continue
        units += take
        cost += take * unit_cost(leg, lvl.price)
        fee += fee_for(q.fee_model, take, lvl.price, q.fee_params)
        pay += take * up
        remaining_pay = payout - pay
        worst = lvl.price
        if binary and units >= target_units:
            break
        if not binary and remaining_pay <= 0:
            break

    if binary and units < target_units:
        return None
    if not binary and remaining_pay > payout * Decimal("0.001") + step * levels[0].price:
        return None
    if units < q.min_size:
        return None
    if q.max_stake is not None and not binary and units > q.max_stake:
        return None
    avg = (cost / units) if binary else (pay / units)
    return LegFill(
        leg=leg,
        units=units,
        stake=cost + fee,
        fee=fee,
        avg_price=avg,
        payout_if_win=pay,
        worst_price=worst,
    )
