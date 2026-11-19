"""Manual odds entry: the user types odds seen in a bookmaker app (Olimpbet,
Fonbet, ...); we find the same event among live markets and compute arbitrage.

Input examples (one line):
    Ростов — Крылья Советов 1.92 3.62 4.23      (football 1X2: home, draw, away)
    Medvedev - Zverev 1.60 2.40                  (2-way: winner)
    Спартак против ЦСКА 2,10 3,40 3,30
The user's odds cannot be re-checked by us, so every result is marked as a risk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from app.arbitrage.finder import Finder, Opportunity
from app.arbitrage.optimizer import FX
from app.domain import (
    Market,
    MarketType,
    OppStatus,
    Outcome,
    PlatformType,
    PriceKind,
    Quote,
    RetirementRule,
    SettlementProfile,
    VoidRule,
    utcnow,
)
from app.matching.engine import match_markets
from app.normalization.names import basic

MANUAL_BOOKMAKERS = {
    "olimpbet": "OLIMPBET", "fonbet": "FONBET", "parimatch": "PARIMATCH", "winline": "WINLINE",
    "tennisi": "TENNISI", "manual": "Другая контора",
}
_SEP = re.compile(r"\s+(?:—|–|-|vs\.?|v|против|x)\s+", re.I)
_ODD = re.compile(r"(?<![\w.,])(\d{1,3}[.,]\d{1,3}|\d{1,3})(?![\w.,])")
MANUAL_RISK = ("РИСК: коэффициент {bk} введён вручную — бот не может его перепроверить. "
               "Убедитесь в приложении, что коэффициент и событие те же, прежде чем ставить")


class ManualError(ValueError):
    pass


@dataclass
class ManualInput:
    team_a: str
    team_b: str
    odds: list[Decimal]  # 2 = winner A/B; 3 = 1, X, 2


@dataclass
class ManualResult:
    event_title: str
    event_start: object
    category: str
    matched_platforms: list[str]
    opportunities: list[Opportunity] = field(default_factory=list)
    best_s: Decimal | None = None
    best_desc: str = ""
    confidence: float = 0.0
    # (outcome label, your odds, odds needed for an arbitrage given the best live prices elsewhere)
    thresholds: list[tuple[str, Decimal, Decimal | None]] = field(default_factory=list)


def parse_manual(text: str) -> ManualInput:
    text = (text or "").strip().replace(" ", " ")
    m = re.search(r"(\d{1,3}[.,]\d{1,3}|\s\d{1,3})(\s+(\d{1,3}[.,]\d{1,3}|\d{1,3}))+\s*$", text)
    if not m:
        raise ManualError("не нашёл коэффициенты в конце строки (нужно 2 или 3 числа, например 1.92 3.62 4.23)")
    names_part, odds_part = text[: m.start()].strip(" :;,"), text[m.start():]
    odds = []
    for tok in _ODD.findall(odds_part):
        try:
            v = Decimal(tok.replace(",", "."))
        except InvalidOperation as e:
            raise ManualError(f"не число: {tok}") from e
        odds.append(v)
    if len(odds) not in (2, 3):
        raise ManualError(f"нужно 2 или 3 коэффициента, а найдено {len(odds)}")
    if any(o <= 1 or o > 1000 for o in odds):
        raise ManualError("коэффициенты должны быть больше 1 (например 1.85)")
    parts = _SEP.split(names_part, maxsplit=1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ManualError("не понял участников — напишите через тире: «Команда 1 — Команда 2»")
    return ManualInput(parts[0].strip(), parts[1].strip(), odds)


def manual_market(bk: str, inp: ManualInput, template: Market) -> Market:
    """Build the user's market in the shape of a live market of the same event."""
    labels = [inp.team_a, "Draw", inp.team_b] if len(inp.odds) == 3 else [inp.team_a, inp.team_b]
    now = utcnow()
    outs = []
    for lbl, odd in zip(labels, inp.odds):
        q = Quote(platform=bk, kind=PriceKind.DECIMAL_ODDS, outcome_label=lbl, instrument_id=f"{bk}:manual:{basic(lbl)}",
                  best_price=odd, levels=[], depth_known=False, fetched_at=now, fee_params={"unit_step": "1"},
                  currency="KZT", side="BACK")
        outs.append(Outcome(basic(lbl), lbl, [q]))
    return Market(
        platform=bk, platform_type=PlatformType.BOOKMAKER, market_id=f"{bk}:manual:{basic(inp.team_a)}:{basic(inp.team_b)}",
        event_id="manual", title=f"{inp.team_a} — {inp.team_b}", category=template.category,
        market_type=MarketType.MATCH_WINNER, outcomes=outs, start_time=None, participants=(inp.team_a, inp.team_b),
        settlement=SettlementProfile(VoidRule.REFUND, RetirementRule.UNKNOWN, draw_possible=len(inp.odds) == 3,
                                     notes=f"{MANUAL_BOOKMAKERS.get(bk, bk)}: правила не проверены (ввод вручную)"),
        period=template.period, extra={"manual": True},
    )


def _candidate_ok(m: Market, n_odds: int) -> bool:
    if m.market_type != MarketType.MATCH_WINNER or len(m.participants) != 2:
        return False
    if n_odds == 3:
        return len(m.outcomes) == 3 and m.period == "regular_time"
    return len(m.outcomes) == 2 and m.period == "match"


def find_event(markets: list[Market], bk: str, inp: ManualInput) -> tuple[list[Market], float]:
    """Live markets of the same event (one per platform), best confidence first."""
    now = utcnow()
    best: dict[str, tuple[float, Market]] = {}
    for m in markets:
        if m.platform == bk or not _candidate_ok(m, len(inp.odds)):
            continue
        if m.start_time and m.start_time < now - timedelta(hours=3):
            continue
        r = match_markets(manual_market(bk, inp, m), m)
        if r is None or r.confidence < 0.85:
            continue
        key = m.platform
        # nearest upcoming start breaks ties (same teams can meet twice in the line)
        score = r.confidence - (abs((m.start_time - now).total_seconds()) / 1e9 if m.start_time else 0)
        if key not in best or score > best[key][0]:
            best[key] = (score, m)
    if not best:
        return [], 0.0
    # all chosen markets must be the same real event: keep those close in time to the best one
    ranked = sorted(best.values(), key=lambda x: -x[0])
    anchor = ranked[0][1]
    same = [m for _, m in ranked if not (m.start_time and anchor.start_time) or
            abs((m.start_time - anchor.start_time).total_seconds()) <= 6 * 3600]
    return same, ranked[0][0]


def needed_odds(group, mine: Market) -> list[tuple[str, Decimal, Decimal | None]]:
    """For each of the user's outcomes: odds above which an arbitrage appears (fees included)."""
    from app.arbitrage.finder import effective_unit_cost, target_scenario

    keys = list(group.outcome_keys)
    best_live: dict[str, Decimal] = {}
    for m in group.markets:
        if m is mine:
            continue
        kmap = group.key_maps.get(m.market_id, {})
        for o in m.outcomes:
            u = kmap.get(o.key)
            for q in o.quotes:
                t = target_scenario(q, u, keys) if u else None
                if t is not None:
                    c = effective_unit_cost(q)
                    best_live[t] = min(best_live.get(t, c), c)
    mk = group.key_maps.get(mine.market_id, {})
    out = []
    for o in mine.outcomes:
        k = mk.get(o.key)
        others = [best_live.get(x) for x in keys if x != k]
        if k is None or any(v is None for v in others):
            out.append((o.label, o.quotes[0].best_price, None))
            continue
        rest = sum(others, Decimal(0))
        need = (Decimal(1) / (Decimal(1) - rest)).quantize(Decimal("0.01")) if rest < 1 else None
        out.append((o.label, o.quotes[0].best_price, need))
    return out


async def evaluate_manual(*, providers: dict, markets: list[Market], bk: str, inp: ManualInput, fx: FX,
                          capital_usd: Decimal) -> ManualResult | None:
    found, conf = find_event(markets, bk, inp)
    if not found:
        return None
    # fresh prices for the live side before computing
    for m in found:
        prov = providers.get(m.platform) or providers.get("the_odds_api")
        if prov is not None:
            try:
                await prov.refresh_market(m)
            except Exception:  # noqa: BLE001
                for o in m.outcomes:
                    o.quotes = []
    found = [m for m in found if all(o.quotes for o in m.outcomes)]
    if not found:
        return None
    anchor = found[0]
    mine = manual_market(bk, inp, anchor)
    from app.matching.engine import group_markets

    groups = [g for g in group_markets([mine, *found], min_conf=0.85) if len(g.markets) > 1 and mine in g.markets]
    res = ManualResult(event_title=anchor.title, event_start=anchor.start_time, category=anchor.category,
                       matched_platforms=sorted({m.platform for m in found}), confidence=conf)
    if not groups:
        return res
    res.thresholds = needed_odds(groups[0], mine)
    finder = Finder(capital_usd=capital_usd, fx=fx, prefilter_margin=Decimal("-0.08"))  # also show near misses
    opps = finder.from_group(groups[0])
    bk_title = MANUAL_BOOKMAKERS.get(bk, bk)
    for o in opps:
        o.start_time = anchor.start_time
        o.title = anchor.title
        o.warnings.insert(0, MANUAL_RISK.format(bk=bk_title))
        uses_manual = any(lg.quote.platform == bk for lg in o.legs)
        if o.blocking:
            o.set(OppStatus.SETTLEMENT_MISMATCH, "; ".join(o.blocking))
        elif o.result and o.result.min_profit > 0 and uses_manual:
            o.verified_at = utcnow()
            o.set(OppStatus.VERIFIED, "цены других площадок перепроверены; коэффициент введён вручную")
        else:
            o.set(OppStatus.REJECTED, "вилки нет при этих коэффициентах")
    if finder.closest:
        s_val, desc = min(finder.closest)
        res.best_s, res.best_desc = s_val, desc
    res.opportunities = sorted([o for o in opps if any(lg.quote.platform == bk for lg in o.legs)],
                               key=lambda o: o.result.min_profit if o.result else Decimal(-10**9), reverse=True)
    return res
