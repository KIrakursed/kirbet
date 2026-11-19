"""Turns an Opportunity into a plain-language betting plan in tenge.

All displayed amounts are rounded to whole tenge FIRST and every derived number
(total, profit, percent) is computed from the rounded values, so what the user
sees always adds up exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from app.arbitrage.finder import Opportunity, effective_unit_cost
from app.arbitrage.optimizer import FX, optimize
from app.domain import ONE, OppStatus, PriceKind, utcnow
from app.execution_models.legs import unit_payout

TENGE = Decimal("1")
PRESETS_KZT = [5_000, 10_000, 25_000, 50_000, 100_000, 250_000]

PLATFORM_TITLE = {
    "1xbet": "1XBET", "ubet": "UBET", "olimpbet": "OLIMPBET", "fonbet": "FONBET", "parimatch": "PARIMATCH",
    "winline": "WINLINE", "tennisi": "TENNISI", "oinabet": "OINABET", "polymarket": "Polymarket",
    "kalshi": "Kalshi", "betfair": "Betfair", "betfair_ex_eu": "Betfair", "pinnacle": "Pinnacle",
    "onexbet": "1xBet (межд.)", "marathonbet": "Marathonbet",
}


class Color(str, Enum):
    GREEN = "🟢"
    YELLOW = "🟡"
    RED = "🔴"
    GREY = "⚪"


COLOR_TEXT = {
    Color.GREEN: "цены перепроверены, расчётная прибыль положительная",
    Color.YELLOW: "есть ограничения или повышенный риск — прочитайте риски",
    Color.RED: "возможность исчезла или прибыль отрицательная",
    Color.GREY: "недостаточно данных для проверки",
}


def ptitle(p: str) -> str:
    return PLATFORM_TITLE.get(p, p.replace("_", " ").title())


def tenge(v: Decimal) -> Decimal:
    return Decimal(v).quantize(TENGE, ROUND_HALF_UP)


def fmt_kzt(v: Decimal, sign: bool = False) -> str:
    v = tenge(v)
    s = f"{abs(v):,.0f}".replace(",", " ")
    if sign:
        return ("+" if v >= 0 else "−") + s + " ₸"
    return ("−" if v < 0 else "") + s + " ₸"


def fmt_pct(v: Decimal, sign: bool = True) -> str:
    v = Decimal(v).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return (("+" if v >= 0 else "") if sign else "") + f"{v}%"


@dataclass
class LegPlan:
    platform: str
    title: str
    outcome: str
    kind: PriceKind
    price: Decimal
    stake_native: Decimal
    currency: str
    stake_kzt: Decimal
    payout_kzt: Decimal
    fee_kzt: Decimal
    covers: frozenset
    url: str
    side: str
    available_kzt: Decimal | None  # money that can actually be placed at visible prices (None = unknown)
    avg_price: Decimal
    units: Decimal = Decimal(0)


@dataclass
class Plan:
    legs: list[LegPlan]
    total_kzt: Decimal
    payouts_kzt: dict[str, Decimal]
    profits_kzt: dict[str, Decimal]
    profit_kzt: Decimal  # worst case
    roi_pct: Decimal
    requested_kzt: Decimal
    limited_note: str
    color: Color
    theoretical_roi_pct: Decimal
    after_fees_roi_pct: Decimal
    liquidity_roi_pct: Decimal
    age_s: float
    risks: list[str] = field(default_factory=list)
    kzt_per_usd: Decimal | None = None
    fx_used: bool = False
    max_volume_kzt: Decimal | None = None  # largest total stake that still shows a profit

    @property
    def payout_min(self) -> Decimal:
        return min(self.payouts_kzt.values())

    @property
    def payout_max(self) -> Decimal:
        return max(self.payouts_kzt.values())


def _simplify_risk(w: str) -> str | None:
    w_low = w.lower()
    if "↔" in w and "участники" in w_low:
        return None  # matching diagnostics are for the details card only
    if "глубина/лимиты неизвестны" in w_low:
        return f"{ptitle(w.split(':')[0])}: лимит ставки неизвестен — букмекер может принять меньше"
    if w.startswith("РИСК: "):
        rest = w[len("РИСК: "):]
        return rest[:1].upper() + rest[1:]
    if "проскальзывание" in w_low:
        return "Часть объёма по худшей цене (учтено в расчёте)"
    if "walkover" in w_low or "отмене" in w_low:
        return "Если матч отменят до начала, площадки рассчитают его по-разному"
    return w


def risk_color(opp: Opportunity) -> Color:
    if opp.status in (OppStatus.REJECTED, OppStatus.EXPIRED, OppStatus.INSUFFICIENT_LIQUIDITY,
                      OppStatus.SETTLEMENT_MISMATCH):
        return Color.RED
    if opp.status not in (OppStatus.VERIFIED, OppStatus.NOTIFIED):
        return Color.GREY
    if opp.result is None or opp.result.min_profit <= 0:
        return Color.RED
    serious = [w for w in opp.warnings if ("РИСК" in w or "walkover" in w or "Аномальн" in w)]
    live = opp.start_time is not None and opp.start_time < utcnow()
    if serious or live or opp.result.limited_by == "liquidity":
        return Color.YELLOW
    return Color.GREEN


def _roi_from_s(s: Decimal) -> Decimal:
    return ((ONE / s) - ONE) * 100 if s > 0 else Decimal(0)


def theoretical_rois(opp: Opportunity) -> tuple[Decimal, Decimal]:
    """(best prices without fees, best prices with fees) — both before liquidity limits."""
    s_raw = Decimal(0)
    for lg in opp.legs:
        q = lg.quote
        up = unit_payout(lg, q.best_price)
        from app.execution_models.legs import unit_cost

        s_raw += unit_cost(lg, q.best_price) / up if up > 0 else Decimal(99)
    s_fee = sum((effective_unit_cost(lg.quote) for lg in opp.legs), Decimal(0))
    return _roi_from_s(s_raw), _roi_from_s(s_fee)


def build_plan(opp: Opportunity, fx: FX, capital_kzt: Decimal, *, with_max_volume: bool = False) -> Plan | None:
    capital_kzt = Decimal(capital_kzt)
    kzt_rate = fx.rates.get("KZT")  # USD per 1 KZT
    if kzt_rate is None:
        raise KeyError("нет курса KZT")
    capital_usd = capital_kzt * kzt_rate
    res = optimize(opp.legs, opp.scenarios, capital=capital_usd, fx=fx)
    if res is None:
        return None
    usd_to_kzt = ONE / kzt_rate

    def to_kzt(amount: Decimal, cur: str) -> Decimal:
        return amount if cur == "KZT" else fx.to_base(amount, cur) * usd_to_kzt

    legs: list[LegPlan] = []
    fx_used = False
    for f in res.legs:
        q = f.leg.quote
        if q.currency != "KZT":
            fx_used = True
        avail = None
        if q.levels and q.depth_known:
            # money that fits at the BEST price only (deeper levels are worse prices)
            lv0 = q.levels[0]
            cost0 = lv0.size * (lv0.price if q.kind == PriceKind.BINARY_CONTRACT else Decimal(1))
            avail = tenge(to_kzt(cost0, q.currency))
        legs.append(LegPlan(
            platform=q.platform, title=ptitle(q.platform), outcome=q.outcome_label, kind=q.kind,
            price=q.best_price, stake_native=f.stake, currency=q.currency,
            stake_kzt=tenge(to_kzt(f.stake, q.currency)), payout_kzt=tenge(to_kzt(f.payout_if_win, q.currency)),
            fee_kzt=tenge(to_kzt(f.fee, q.currency)), covers=f.leg.covers, url=q.url, side=q.side,
            available_kzt=avail, avg_price=f.avg_price, units=f.units,
        ))
    total = sum((lp.stake_kzt for lp in legs), Decimal(0))
    payouts = {s: sum((lp.payout_kzt for lp in legs if s in lp.covers), Decimal(0)) for s in opp.scenarios}
    profits = {s: payouts[s] - total for s in opp.scenarios}
    worst = min(profits.values())
    roi = (worst / total * 100) if total > 0 else Decimal(0)
    theo, after_fees = theoretical_rois(opp)
    note = ""
    if total < capital_kzt * Decimal("0.98"):
        if res.limited_by == "liquidity":
            note = (f"Доступно только {fmt_kzt(total)} из {fmt_kzt(capital_kzt)} — "
                    "больше по этим ценам не купить, расчёт сделан на доступный объём")
        else:
            note = f"Использовано {fmt_kzt(total)} из {fmt_kzt(capital_kzt)} (округление и минимальные размеры)"
    risks: list[str] = []
    for w in dict.fromkeys(opp.warnings):
        r = _simplify_risk(w)
        if r and r not in risks:
            risks.append(r)
    if any(lp.platform in ("ubet", "parimatch", "1xbet", "winline") for lp in legs):
        risks.append("Букмекер может ограничить или аннулировать ставки, если сочтёт их «вилочными»")
    if fx_used:
        risks.append("Часть суммы в долларах (USDC) — учтите комиссии пополнения и обмена валюты")
    color = risk_color(opp)
    if worst <= 0 and color in (Color.GREEN, Color.YELLOW):
        color = Color.RED
    max_vol = None
    if with_max_volume:
        big = build_plan(opp, fx, Decimal("1000000000"))
        max_vol = big.total_kzt if big and big.profit_kzt > 0 else Decimal(0)
    return Plan(
        max_volume_kzt=max_vol,
        legs=legs, total_kzt=total, payouts_kzt=payouts, profits_kzt=profits, profit_kzt=worst,
        roi_pct=roi, requested_kzt=capital_kzt, limited_note=note, color=color,
        theoretical_roi_pct=theo, after_fees_roi_pct=after_fees, liquidity_roi_pct=res.roi_pct,
        age_s=opp.max_quote_age, risks=risks, kzt_per_usd=usd_to_kzt.quantize(Decimal("0.01")), fx_used=fx_used,
    )


def plain_sentence(plan: Plan) -> str:
    parts = [f"{fmt_kzt(lp.stake_kzt)} на {lp.title}" for lp in plan.legs]
    if len(parts) > 1:
        where = ", ".join(parts[:-1]) + " и " + parts[-1]
    else:
        where = parts[0]
    return (f"Вложить {fmt_kzt(plan.total_kzt)}. Разместить {where}. Расчётная чистая прибыль — "
            f"{fmt_kzt(plan.profit_kzt)}, или {fmt_pct(plan.roi_pct, sign=False)}, до возможных дополнительных расходов.")
