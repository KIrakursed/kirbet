"""Cross-platform arbitrage optimizer working on real cash flows.

Given a set of legs and the list of possible scenarios, we choose a common
payout target ``P`` (base currency) for every leg, walk each leg's order book
(VWAP + fees + limits) and compute the net result for *every* scenario:

    profit_s = sum(payout_l for legs l covering s) - sum(cost_l)

The arbitrage is valid only if min_s profit_s > 0. P is optimised over the
liquidity breakpoints of every leg and the capital constraint.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.domain import ONE, ZERO, ArbResult, Leg, LegFill
from app.execution_models.legs import breakpoints, fill_for_payout, max_payout

Q2 = Decimal("0.01")


class FX:
    """Currency → base currency multipliers (value of 1 unit in base)."""

    def __init__(self, base: str = "USD", rates: dict[str, Decimal] | None = None):
        self.base = base
        self.rates = {base: ONE, **(rates or {})}

    def to_base(self, amount: Decimal, cur: str) -> Decimal:
        if cur not in self.rates:
            raise KeyError(f"нет курса для {cur}")
        return amount * self.rates[cur]

    def from_base(self, amount: Decimal, cur: str) -> Decimal:
        return amount / self.rates[cur]


def _evaluate(
    legs: list[Leg], scenarios: list[str], payout_base: Decimal, fx: FX
) -> tuple[list[LegFill], dict[str, Decimal], Decimal] | None:
    fills: list[LegFill] = []
    for leg in legs:
        p_leg = fx.from_base(payout_base, leg.quote.currency)
        f = fill_for_payout(leg, p_leg)
        if f is None:
            return None
        fills.append(f)
    cost = sum((fx.to_base(f.stake, f.leg.quote.currency) for f in fills), ZERO)
    res: dict[str, Decimal] = {}
    for s in scenarios:
        pay = sum(
            (fx.to_base(f.payout_if_win, f.leg.quote.currency) for f in fills if s in f.leg.covers),
            ZERO,
        )
        res[s] = pay - cost
    return fills, res, cost


def check_coverage(legs: list[Leg], scenarios: list[str]) -> list[str]:
    """Scenarios not covered by any leg (must be empty for an arbitrage)."""
    return [s for s in scenarios if not any(s in leg.covers for leg in legs)]


def optimize(
    legs: list[Leg],
    scenarios: list[str],
    *,
    capital: Decimal,
    fx: FX | None = None,
    min_payout: Decimal = Decimal("1"),
) -> ArbResult | None:
    """Return the best feasible allocation, or None if nothing is fillable."""
    fx = fx or FX()
    capital = Decimal(str(capital))
    uncovered = check_coverage(legs, scenarios)
    if uncovered:
        raise ValueError(f"исходы не покрыты: {uncovered}")

    # upper bound for P from liquidity
    caps: list[Decimal] = []
    limited = ""
    for leg in legs:
        mp = max_payout(leg)
        if mp is not None:
            v = fx.to_base(mp, leg.quote.currency)
            caps.append(v)
    p_max = min(caps) if caps else capital * 1000

    # candidate payout points: liquidity breakpoints + capital bound
    cands: set[Decimal] = set()
    for leg in legs:
        for bp in breakpoints(leg):
            v = fx.to_base(bp, leg.quote.currency)
            if v <= p_max:
                cands.add(v)
    cands.add(p_max)

    # largest P whose total cost fits into capital (binary search)
    def cost_at(p: Decimal) -> Decimal | None:
        r = _evaluate(legs, scenarios, p, fx)
        return None if r is None else r[2]

    lo, hi = ZERO, p_max
    for _ in range(40):
        mid = (lo + hi) / 2
        c = cost_at(mid)
        if c is not None and c <= capital:
            lo = mid
        else:
            hi = mid
    p_cap = lo
    cands = {c for c in cands if c <= p_cap}
    cands.add(p_cap)
    # geometric grid so that small profitable sizes are never skipped
    g = max(min_payout, Decimal("1"))
    while g < p_cap:
        cands.add(g)
        g *= Decimal("1.5")
    cands = {c for c in cands if c >= min_payout}
    if not cands:
        return None

    best = None
    for p in sorted(cands):
        r = _evaluate(legs, scenarios, p, fx)
        if r is None:
            continue
        fills, res, cost = r
        if cost > capital:
            continue
        mn = min(res.values())
        if best is None or mn > best[1]:
            best = (fills, mn, res, cost, p)
    if best is None:
        return None
    fills, mn, res, cost, p = best
    if abs(p - p_max) < Decimal("0.01"):
        limited = "liquidity"
    elif abs(p - p_cap) < Decimal("0.01"):
        limited = "capital"
    roi = (mn / cost * 100) if cost > 0 else ZERO
    warnings: list[str] = []
    for f in fills:
        if not f.leg.quote.depth_known:
            warnings.append(
                f"{f.leg.quote.platform}: глубина/лимиты неизвестны — объём не подтверждён"
            )
        if f.worst_price != f.leg.quote.best_price:
            warnings.append(
                f"{f.leg.quote.platform}: проскальзывание {f.leg.quote.best_price}→{f.worst_price}"
            )
    return ArbResult(
        legs=fills,
        scenarios={k: v.quantize(Q2, ROUND_HALF_UP) for k, v in res.items()},
        total_cost=cost.quantize(Q2, ROUND_HALF_UP),
        min_profit=mn.quantize(Q2, ROUND_HALF_UP),
        roi_pct=roi.quantize(Decimal("0.0001"), ROUND_HALF_UP),
        payout_units=p,
        limited_by=limited,
        warnings=warnings,
    )


def max_profitable_size(
    legs: list[Leg], scenarios: list[str], fx: FX | None = None
) -> Decimal:
    """Largest payout P (base) at which min profit is still > 0."""
    fx = fx or FX()
    caps = [max_payout(leg) for leg in legs]
    caps_b = [fx.to_base(c, leg.quote.currency) for c, leg in zip(caps, legs) if c is not None]
    hi = min(caps_b) if caps_b else Decimal("1e9")
    pts = sorted(
        {fx.to_base(bp, leg.quote.currency) for leg in legs for bp in breakpoints(leg)} | {hi}
    )
    best = ZERO
    for p in pts:
        if p > hi:
            break
        r = _evaluate(legs, scenarios, p, fx)
        if r and min(r[1].values()) > 0:
            best = p
    return best
