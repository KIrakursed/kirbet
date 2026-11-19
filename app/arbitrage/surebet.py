"""Classic bookmaker surebet maths (decimal odds, N exclusive+exhaustive outcomes).

    S = sum(1/odds_i);  arbitrage iff S < 1
    ROI = (1/S - 1) * 100
    stake_i = B * (1/odds_i) / S
After rounding stakes, the real worst-case profit is recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.domain import ONE, ZERO


@dataclass
class SurebetResult:
    implied_sum: Decimal
    is_arb: bool
    theoretical_roi_pct: Decimal
    stakes: list[Decimal]
    payouts: list[Decimal]
    total_staked: Decimal
    profits: list[Decimal]  # net profit for each outcome winning
    min_profit: Decimal
    real_roi_pct: Decimal
    warnings: list[str]


def implied_sum(odds: list[Decimal]) -> Decimal:
    if len(odds) < 2:
        raise ValueError("need at least 2 outcomes")
    for o in odds:
        if o <= ONE:
            raise ValueError(f"decimal odds must be > 1, got {o}")
    return sum((ONE / o for o in odds), ZERO)


def calculate(
    odds: list[Decimal],
    bank: Decimal,
    *,
    rounding_step: Decimal = Decimal("1"),
    min_stakes: list[Decimal] | None = None,
    max_stakes: list[Decimal] | None = None,
) -> SurebetResult:
    odds = [Decimal(str(o)) for o in odds]
    bank = Decimal(str(bank))
    s = implied_sum(odds)
    theo_roi = (ONE / s - ONE) * 100
    warnings: list[str] = []

    raw = [bank * (ONE / o) / s for o in odds]
    scale = ONE
    if max_stakes:
        for st, mx in zip(raw, max_stakes):
            if mx is not None and st > mx:
                scale = min(scale, mx / st)
        if scale < ONE:
            warnings.append(
                f"Банк уменьшен до {(bank * scale).quantize(Decimal('1'))} из-за лимита ставки"
            )
            raw = [r * scale for r in raw]

    stakes = [(r / rounding_step).to_integral_value(ROUND_DOWN) * rounding_step for r in raw]
    if min_stakes:
        for i, (st, mn) in enumerate(zip(stakes, min_stakes)):
            if mn is not None and st < mn:
                warnings.append(f"Ставка #{i + 1} ({st}) меньше минимальной ({mn})")
    total = sum(stakes, ZERO)
    payouts = [st * o for st, o in zip(stakes, odds)]
    profits = [p - total for p in payouts]
    min_p = min(profits) if profits else ZERO
    real_roi = (min_p / total * 100) if total > 0 else ZERO
    q = Decimal("0.01")
    return SurebetResult(
        implied_sum=s,
        is_arb=s < ONE,
        theoretical_roi_pct=theo_roi.quantize(Decimal("0.0001"), ROUND_HALF_UP),
        stakes=stakes,
        payouts=[p.quantize(q, ROUND_HALF_UP) for p in payouts],
        total_staked=total,
        profits=[p.quantize(q, ROUND_HALF_UP) for p in profits],
        min_profit=min_p.quantize(q, ROUND_HALF_UP),
        real_roi_pct=real_roi.quantize(Decimal("0.0001"), ROUND_HALF_UP),
        warnings=warnings,
    )


def binary_price_to_odds(price: Decimal) -> Decimal:
    """Effective decimal odds of buying a binary share at ``price`` (no fees)."""
    if price <= 0 or price >= 1:
        raise ValueError("price must be in (0,1)")
    return ONE / price
