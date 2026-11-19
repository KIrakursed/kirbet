"""ParsedEvent (site DOM) → domain ``Market`` used by matching / arbitrage."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.domain import (
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
from app.providers.bookmakers.common.models import ParsedEvent, ParsedMarket

DRAW_NAMES = {"х", "x", "ничья", "draw"}
HOME_NAMES = {"п1", "1", "w1"}
AWAY_NAMES = {"п2", "2", "w2"}
CATEGORY = {"cs2": "cs2", "dota2": "dota2"}


def default_settlement(sport: str, draw: bool, site: str) -> SettlementProfile:
    if sport == "tennis":
        return SettlementProfile(walkover=VoidRule.REFUND, retirement=RetirementRule.UNKNOWN, draw_possible=False,
                                 notes=f"{site}: правила снятия игрока не прочитаны автоматически — проверьте")
    return SettlementProfile(walkover=VoidRule.REFUND, retirement=RetirementRule.UNKNOWN, draw_possible=draw,
                             notes=f"{site}: исход основного времени; отмена → возврат")


def winner_market(ev: ParsedEvent, pm: ParsedMarket, platform: str) -> Market | None:
    """Convert a 1X2 / 2-way winner market. Other market kinds are kept only in snapshots."""
    if pm.kind not in ("1x2", "winner"):
        return None
    labels: list[tuple[str, str]] = []  # (canonical label, raw outcome name)
    for o in pm.outcomes:
        n = o.name.strip().lower()
        if n in HOME_NAMES:
            labels.append((ev.home, o.name))
        elif n in AWAY_NAMES:
            labels.append((ev.away, o.name))
        elif n in DRAW_NAMES:
            labels.append(("Draw", o.name))
        else:
            return None
    by_name = {o.name: o for o in pm.outcomes}
    if len({lab for lab, _ in labels}) != len(labels):
        return None
    draw = any(lab == "Draw" for lab, _ in labels)
    if pm.kind == "1x2" and not draw:
        return None
    outs = []
    for lab, raw in labels:
        o = by_name[raw]
        q = Quote(platform=platform, kind=PriceKind.DECIMAL_ODDS, outcome_label=lab,
                  instrument_id=f"{platform}:{ev.external_id}:{pm.name}:{raw}", best_price=o.odds, levels=[],
                  depth_known=False, fetched_at=ev.fetched_at, fee_params={"unit_step": "1"}, currency="KZT",
                  side="BACK", url=ev.url)
        outs.append(Outcome(basic(lab), lab, [q]))
    cat = CATEGORY.get(ev.sport, ev.sport)
    return Market(
        platform=platform, platform_type=PlatformType.BOOKMAKER, market_id=f"{platform}:{ev.external_id}",
        event_id=ev.external_id, title=ev.name, category=cat, market_type=MarketType.MATCH_WINNER,
        outcomes=outs, start_time=ev.start_time, participants=(ev.home, ev.away), tournament=ev.tournament,
        settlement=default_settlement(ev.sport, draw, platform), period="regular_time" if draw else "match",
        url=ev.url, extra={"lang": "ru", "source": "dom", "live": ev.is_live, "market_name": pm.name,
                           "timezone_unknown": ev.timezone_unknown, "start_text": ev.start_text},
    )


LINE_SPORTS = {"football"}  # totals/handicaps take part in arbitrage only here (regular time, see below)


def _half_line(v: str | None) -> Decimal | None:
    """'2.5' / '-1.5' / '+0.5' → Decimal; whole and quarter lines (push / split stakes) → None."""
    try:
        d = Decimal(str(v).replace(",", ".").replace("+", "")) if v not in (None, "") else None
    except InvalidOperation:
        return None
    if d is None or (abs(d) * 2) % 2 != 1:
        return None
    return d


def line_market(ev: ParsedEvent, pm: ParsedMarket, platform: str) -> Market | None:
    """Total / handicap market for arbitrage.

    Only football, only lines ending in .5 (no push, no split stake), only full-time regular-time
    markets (FONBET / OLIMPBET / WINLINE list pages show regular-time lines for football). The line is
    part of ``period`` so the matching engine compares a total 2.5 only with a total 2.5, and a
    handicap −1.5 on the home team only with a handicap −1.5 on the home team.
    """
    if ev.sport not in LINE_SPORTS or pm.kind not in ("total", "handicap") or len(pm.outcomes) != 2:
        return None
    o1, o2 = pm.outcomes
    if pm.kind == "total":
        line = _half_line(o1.param)
        if line is None or _half_line(o2.param) != line or {o1.name, o2.name} != {"Больше", "Меньше"}:
            return None
        period = f"regular_time|total={line}"
        spec = [("over", "Over", next(o for o in pm.outcomes if o.name == "Больше")),
                ("under", "Under", next(o for o in pm.outcomes if o.name == "Меньше"))]
        mtype = MarketType.TOTAL
    else:
        h1, h2 = _half_line(o1.param), _half_line(o2.param)
        if h1 is None or h2 is None or h1 != -h2 or not o1.name.startswith("Ф1") or not o2.name.startswith("Ф2"):
            return None
        period = f"regular_time|hc1={h1:+}"
        spec = [(basic(ev.home), ev.home, o1), (basic(ev.away), ev.away, o2)]
        mtype = MarketType.HANDICAP
    outs = []
    for key, label, o in spec:
        q = Quote(platform=platform, kind=PriceKind.DECIMAL_ODDS, outcome_label=label,
                  instrument_id=f"{platform}:{ev.external_id}:{period}:{key}", best_price=o.odds, levels=[],
                  depth_known=False, fetched_at=ev.fetched_at, fee_params={"unit_step": "1"}, currency="KZT",
                  side="BACK", url=ev.url)
        outs.append(Outcome(key, label, [q]))
    return Market(
        platform=platform, platform_type=PlatformType.BOOKMAKER,
        market_id=f"{platform}:{ev.external_id}:{period}", event_id=ev.external_id, title=ev.name,
        category=ev.sport, market_type=mtype, outcomes=outs, start_time=ev.start_time,
        participants=(ev.home, ev.away), tournament=ev.tournament,
        settlement=SettlementProfile(walkover=VoidRule.REFUND, retirement=RetirementRule.UNKNOWN,
                                     draw_possible=False,
                                     notes=f"{platform}: {pm.name}, основное время, линия x.5 — без возврата"),
        period=period, url=ev.url,
        extra={"lang": "ru", "source": "dom", "live": ev.is_live, "market_name": pm.name,
               "timezone_unknown": ev.timezone_unknown, "start_text": ev.start_text},
    )


def to_domain(events: list[ParsedEvent], platform: str, *, include_live: bool = False) -> list[Market]:
    out: list[Market] = []
    for ev in events:
        if ev.is_live and not include_live:
            continue
        for pm in ev.markets:
            mk = winner_market(ev, pm, platform)
            if mk:
                out.append(mk)
                break
        seen: set[str] = set()
        for pm in ev.markets:
            lm = line_market(ev, pm, platform)
            if lm and lm.market_id not in seen:
                seen.add(lm.market_id)
                out.append(lm)
    return out
