"""Totals / handicaps in arbitrage: only identical football lines ending in .5 are matched."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D

from app.arbitrage.finder import Finder
from app.arbitrage.optimizer import FX
from app.domain import MarketType
from app.matching.engine import group_markets
from app.providers.bookmakers.common.models import ParsedEvent, ParsedMarket, ParsedOutcome
from app.providers.bookmakers.common.normalizer import _half_line, to_domain

T = datetime(2026, 11, 21, 12, 30, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def ev(bk, home, away, markets, sport="football"):
    return ParsedEvent(bookmaker=bk, external_id=f"{bk}1", sport=sport, home=home, away=away, start_time=T,
                       markets=markets, fetched_at=NOW, url=f"https://{bk}.kz/e")


def total(line, over, under):
    return ParsedMarket(name=f"Тотал {line}", kind="total", outcomes=[
        ParsedOutcome(name="Больше", odds=D(over), param=line), ParsedOutcome(name="Меньше", odds=D(under), param=line)])


def hc(h1, o1, h2, o2):
    return ParsedMarket(name="Фора", kind="handicap", outcomes=[
        ParsedOutcome(name=f"Ф1 ({h1})", odds=D(o1), param=h1), ParsedOutcome(name=f"Ф2 ({h2})", odds=D(o2), param=h2)])


def test_half_lines_only():
    assert _half_line("2.5") == D("2.5") and _half_line("+1.5") == D("1.5") and _half_line("-0.5") == D("-0.5")
    assert _half_line("3") is None and _half_line("2.25") is None and _half_line("") is None


def test_total_and_handicap_markets_built_with_line_in_period():
    ms = to_domain([ev("fonbet", "Rostov", "Krasnodar", [total("2.5", "2.10", "1.70"), total("3", "2.6", "1.5"),
                                                         hc("-1.5", "3.1", "+1.5", "1.35"), hc("-1", "2.8", "+1", "1.4")])],
                   "fonbet")
    kinds = {(m.market_type, m.period) for m in ms}
    assert kinds == {(MarketType.TOTAL, "regular_time|total=2.5"), (MarketType.HANDICAP, "regular_time|hc1=-1.5")}
    assert all(not m.settlement.draw_possible for m in ms)  # half lines: no push


def test_not_for_other_sports():
    assert to_domain([ev("fonbet", "A", "B", [total("2.5", "2", "1.8")], sport="hockey")], "fonbet") == []


def test_cross_bookmaker_total_arbitrage_is_found_and_lines_never_mix():
    a = to_domain([ev("fonbet", "Rostov", "Krasnodar", [total("2.5", "2.10", "1.70"), total("3.5", "3.0", "1.35")])],
                  "fonbet")
    b = to_domain([ev("olimpbet", "Ростов", "Краснодар", [total("2.5", "1.75", "2.05")])], "olimpbet")
    groups = [g for g in group_markets(a + b, min_conf=0.9) if len(g.markets) > 1]
    assert len(groups) == 1 and groups[0].markets[0].period == "regular_time|total=2.5"  # 3.5 stays alone
    opps = Finder(capital_usd=D("1000"), fx=FX("USD", {"KZT": D(1) / D(520)})).from_group(groups[0])
    best = max((o for o in opps if o.result), key=lambda o: o.result.roi_pct)
    # Over 2.5 @ 2.10 (FONBET) + Under 2.5 @ 2.05 (OLIMPBET): 1/2.10 + 1/2.05 = 0.9640 < 1
    assert best.result.is_arb and {lf.leg.quote.platform for lf in best.result.legs} == {"fonbet", "olimpbet"}
    assert all(isinstance(lf.stake, D) for lf in best.result.legs)


def test_handicap_sides_must_be_the_same_line():
    a = to_domain([ev("fonbet", "Rostov", "Krasnodar", [hc("-1.5", "3.1", "+1.5", "1.35")])], "fonbet")
    b = to_domain([ev("winline", "Ростов", "Краснодар", [hc("+1.5", "1.3", "-1.5", "3.4")])], "winline")
    assert [g for g in group_markets(a + b, min_conf=0.9) if len(g.markets) > 1] == []  # home −1.5 ≠ home +1.5
