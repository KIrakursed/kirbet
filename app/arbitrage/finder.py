"""Builds candidate arbitrage combinations from markets and market groups."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.arbitrage.optimizer import FX, optimize
from app.domain import (
    ONE,
    ArbResult,
    Leg,
    Market,
    MarketType,
    OppStatus,
    PriceKind,
    Quote,
    utcnow,
)
from app.execution_models.fees import fee_for
from app.execution_models.legs import unit_cost, unit_payout
from app.matching.engine import MarketGroup, settlement_conflicts


@dataclass
class Opportunity:
    signature: str
    kind: str  # intra / cross
    title: str
    category: str
    market_type: MarketType
    period: str
    start_time: datetime | None
    markets: list[Market]
    scenarios: list[str]
    scenario_labels: dict[str, str]
    legs: list[Leg]
    result: ArbResult | None
    confidence: float
    status: OppStatus = OppStatus.NEW
    warnings: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    log: list[tuple[str, str]] = field(default_factory=list)  # (status, message)
    created_at: datetime = field(default_factory=utcnow)
    verified_at: datetime | None = None
    db_id: int | None = None

    def set(self, status: OppStatus, msg: str = "") -> None:
        self.status = status
        self.log.append((status.value, msg))

    @property
    def platforms(self) -> list[str]:
        return sorted({lg.quote.platform for lg in self.legs})

    @property
    def max_quote_age(self) -> float:
        return max((lg.quote.age_seconds() for lg in self.legs), default=0.0)


def effective_unit_cost(q: Quote) -> Decimal:
    """Approximate money needed per 1 unit of payout at best price incl. fees."""
    from app.domain import Leg as _L

    leg = _L(q, frozenset())
    up = unit_payout(leg, q.best_price)
    if up <= 0:
        return Decimal("99")
    base = unit_cost(leg, q.best_price) / up
    if q.kind == PriceKind.BINARY_CONTRACT:
        base += fee_for(q.fee_model, Decimal("100"), q.best_price, q.fee_params) / 100
    return base


def _signature(legs: list[Leg]) -> str:
    raw = "|".join(sorted(lg.quote.instrument_id for lg in legs))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def target_scenario(q: Quote, outcome_key: str, all_keys: list[str]) -> str | None:
    """Scenario in which taking quote ``q`` (quoted on ``outcome_key``) pays out.

    BACK / BUY pay when the outcome happens. A LAY on X pays when X does NOT
    happen, so in a 2-way market it belongs to the other outcome; in N-way
    markets a lay covers several scenarios and is not used in partition combos.
    """
    if q.kind != PriceKind.EXCHANGE_LAY:
        return outcome_key
    others = [k for k in all_keys if k != outcome_key]
    return others[0] if len(others) == 1 else None


class Finder:
    def __init__(self, *, capital_usd: Decimal, fx: FX, prefilter_margin: Decimal = Decimal("0")):
        self.capital = capital_usd
        self.fx = fx
        self.margin = prefilter_margin
        self.stats = {"combos_checked": 0, "prefilter_pass": 0, "candidates": 0}
        self.closest: list[tuple[Decimal, str]] = []  # (S incl. fees, description) for monitoring

    def _combos(self, per_outcome: dict[str, list[tuple[Quote, Market]]], scenarios: list[str],
                max_combos: int = 64):
        """Cheapest quotes per outcome; for 2-3 outcomes try a few alternatives each."""
        k = 3 if len(scenarios) <= 3 else 1
        ranked = {s: sorted(per_outcome[s], key=lambda qm: effective_unit_cost(qm[0]))[:k] for s in scenarios}
        n = 0
        for combo in itertools.product(*(ranked[s] for s in scenarios)):
            n += 1
            if n > max_combos:
                break
            yield list(zip(scenarios, combo))

    def evaluate(self, *, title: str, category: str, mtype: MarketType, period: str, start,
                 markets: list[Market], per_outcome: dict[str, list[tuple[Quote, Market]]],
                 scenarios: list[str], labels: dict[str, str], confidence: float, kind: str,
                 base_warnings: list[str], blocking: list[str]) -> list[Opportunity]:
        out = []
        if any(not per_outcome.get(s) for s in scenarios):
            return out
        for combo in self._combos(per_outcome, scenarios):
            self.stats["combos_checked"] += 1
            # cross kind requires ≥2 platforms, intra kind exactly one
            plats = {q.platform for _, (q, _) in combo}
            if kind == "cross" and len(plats) < 2:
                continue
            # an instrument can't be used twice (e.g. Kalshi YES A + NO B are different, fine)
            if len({q.instrument_id for _, (q, _) in combo}) != len(combo):
                continue
            total = sum((effective_unit_cost(q) for _, (q, _) in combo), Decimal(0))
            if kind == "cross":
                self.closest.append((total, f"{title[:60]} | " + " + ".join(
                    f"{q.platform}:{q.side} {q.outcome_label[:18]}@{q.best_price}" for _, (q, _) in combo)))
            if total >= ONE - self.margin:
                continue
            self.stats["prefilter_pass"] += 1
            legs = [Leg(q, frozenset({s}), m) for s, (q, m) in combo]
            # settlement rules are compared only between the platforms actually used
            leg_markets = list({id(m): m for _, (_, m) in combo}.values())
            combo_block, combo_warn = settlement_conflicts(
                [(m.platform, m.settlement) for m in leg_markets], category,
                soft_unknown=any(m.extra.get("manual") for m in leg_markets))
            try:
                res = optimize(legs, scenarios, capital=self.capital, fx=self.fx)
            except (KeyError, ValueError):
                continue
            if res is None:
                continue
            self.stats["candidates"] += 1
            opp = Opportunity(
                signature=_signature(legs), kind=kind, title=title, category=category, market_type=mtype,
                period=period, start_time=start, markets=markets, scenarios=scenarios,
                scenario_labels=labels, legs=legs, result=res, confidence=confidence,
                warnings=list(base_warnings) + combo_warn + res.warnings, blocking=list(blocking) + combo_block,
            )
            opp.set(OppStatus.NEW, f"S≈{total:.4f}")
            out.append(opp)
        return out

    # ------------------------------------------------------------------
    def from_market(self, m: Market) -> list[Opportunity]:
        """Arbitrage inside one market of one platform (complete set cheaper than 1)."""
        if len(m.outcomes) < 2:
            return []
        scen = [o.key for o in m.outcomes]
        warnings, blocking = [], []
        if not m.exhaustive:
            blocking.append("набор исходов не исчерпывающий (возможны другие исходы)")
        if m.settlement.draw_possible and not any(o.key in ("draw", "tie") for o in m.outcomes):
            blocking.append("возможна ничья, но исход 'ничья' не покрыт")
        keys = [o.key for o in m.outcomes]
        per: dict[str, list] = {k: [] for k in keys}
        for o in m.outcomes:
            for q in o.quotes:
                t = target_scenario(q, o.key, keys)
                if t is not None:
                    per[t].append((q, m))
        return self.evaluate(
            title=m.title, category=m.category, mtype=m.market_type, period=m.period, start=m.start_time,
            markets=[m], per_outcome=per, scenarios=scen, labels={o.key: o.label for o in m.outcomes},
            confidence=1.0, kind="intra", base_warnings=warnings, blocking=blocking,
        )

    def from_group(self, g: MarketGroup) -> list[Opportunity]:
        if len(g.markets) < 2:
            return []
        base = g.markets[0]
        scen = list(g.outcome_keys)
        labels = {o.key: o.label for o in base.outcomes}
        per: dict[str, list[tuple[Quote, Market]]] = {s: [] for s in scen}
        for m in g.markets:
            kmap = g.key_maps.get(m.market_id, {})
            for o in m.outcomes:
                u = kmap.get(o.key)
                if u not in per:
                    continue
                for q in o.quotes:
                    t = target_scenario(q, u, scen)
                    if t is not None:
                        per[t].append((q, m))
        blocking: list[str] = []
        warnings: list[str] = []
        if any(m.settlement.draw_possible for m in g.markets) and not any(s in ("draw", "tie") for s in scen):
            blocking.append("в одном из рынков возможна ничья (BO2/1X2) — 2 исхода не покрывают все сценарии")
        if any(not m.exhaustive for m in g.markets):
            blocking.append("рынок не исчерпывающий")
        warnings += g.reasons
        title = base.title if len(base.title) < 90 else base.title[:87] + "..."
        return self.evaluate(
            title=title, category=base.category, mtype=base.market_type, period=base.period,
            start=base.start_time, markets=g.markets, per_outcome=per, scenarios=scen, labels=labels,
            confidence=g.confidence, kind="cross", base_warnings=warnings, blocking=blocking,
        )
