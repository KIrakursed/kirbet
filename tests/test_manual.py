import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pytest

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
from app.manual.entry import ManualError, evaluate_manual, find_event, parse_manual
from app.normalization.names import basic
from app.presentation.plan import Color, build_plan
from app.telegram.bot import manual_no_arb_text

FXR = FX("USD", {"KZT": D(1) / D(500)})


def live_1x2(platform, a, b, odds, hours=30):
    def q(lbl, o):
        return Quote(platform=platform, kind=PriceKind.DECIMAL_ODDS, outcome_label=lbl,
                     instrument_id=f"{platform}:{a}:{lbl}", best_price=D(o), levels=[], depth_known=False,
                     fee_params={"unit_step": "1"}, currency="KZT", side="BACK", url=f"https://{platform}.kz/")
    labels = [a, "Draw", b]
    return Market(platform, PlatformType.BOOKMAKER, f"{platform}:{a}:{hours}", "e", f"{a} — {b}", "football",
                  MarketType.MATCH_WINNER, [Outcome(basic(lbl), lbl, [q(lbl, o)]) for lbl, o in zip(labels, odds)],
                  start_time=utcnow() + timedelta(hours=hours), participants=(a, b),
                  settlement=SettlementProfile(VoidRule.REFUND, RetirementRule.ADVANCING_PLAYER, True),
                  period="regular_time")


class NoopProvider:
    async def refresh_market(self, m):
        return m


PROVS = {"1xbet": NoopProvider(), "oinabet": NoopProvider(), "ubet": NoopProvider()}


def test_parse_variants():
    p = parse_manual("Ростов — Крылья Советов 1.92 3.62 4.23")
    assert (p.team_a, p.team_b, p.odds) == ("Ростов", "Крылья Советов", [D("1.92"), D("3.62"), D("4.23")])
    p = parse_manual("Медведев - Зверев 1,60 2,40")
    assert p.odds == [D("1.60"), D("2.40")] and p.team_b == "Зверев"
    p = parse_manual("Real Madrid vs Barcelona 2.1 3.5 3.2")
    assert p.team_a == "Real Madrid"
    p = parse_manual("Спартак против ЦСКА 2 3.40 3.30")
    assert p.odds[0] == D("2")
    for bad in ("Ростов Крылья 1.9 3.6 4.2", "Ростов — Крылья", "Ростов — Крылья 1.9", "Ростов — Крылья 0.9 3 4"):
        with pytest.raises(ManualError):
            parse_manual(bad)


def test_manual_arb_found_and_card_is_yellow():
    markets = [live_1x2("1xbet", "Ростов", "Крылья Советов", ["1.98", "3.67", "4.205"]),
               live_1x2("oinabet", "Ростов", "Крылья Советов", ["1.95", "3.60", "4.10"])]
    inp = parse_manual("Ростов — Крылья Советов 2.30 3.50 4.00")  # generous home odds at "Olimpbet"
    res = asyncio.run(evaluate_manual(providers=PROVS, markets=markets, bk="olimpbet", inp=inp, fx=FXR,
                                      capital_usd=D(200)))
    assert res is not None and set(res.matched_platforms) == {"1xbet", "oinabet"}
    good = [o for o in res.opportunities if o.status == OppStatus.VERIFIED]
    assert good
    o = good[0]
    assert any(lg.quote.platform == "olimpbet" for lg in o.legs)
    plan = build_plan(o, FXR, D(100000))
    assert plan.profit_kzt > 0 and plan.color == Color.YELLOW  # manual odds are always a risk
    assert sum(lp.stake_kzt for lp in plan.legs) == plan.total_kzt
    assert any("введён вручную" in r for r in plan.risks)


def test_manual_no_arb_gives_thresholds():
    markets = [live_1x2("1xbet", "Ростов", "Крылья Советов", ["1.98", "3.67", "4.205"])]
    inp = parse_manual("Ростов — Крылья Советов 1.90 3.50 4.00")
    res = asyncio.run(evaluate_manual(providers=PROVS, markets=markets, bk="olimpbet", inp=inp, fx=FXR,
                                      capital_usd=D(200)))
    assert res is not None
    assert not [o for o in res.opportunities if o.status == OppStatus.VERIFIED]
    th = {lbl: nd for lbl, _cur, nd in res.thresholds}
    # home win needed: 1 / (1 - 1/3.67 - 1/4.205)
    expected = (D(1) / (D(1) - D(1) / D("3.67") - D(1) / D("4.205"))).quantize(D("0.01"))
    assert th["Ростов"] == expected
    # check: with exactly that home odd the implied sum is ~1, slightly above it there is an arb
    text = manual_no_arb_text(res, "OLIMPBET", "head")
    assert "Вилки нет" in text and f"выше <b>{expected}</b>" in text


def test_manual_not_found_and_women_not_confused():
    markets = [live_1x2("1xbet", "Динамо Краснодар (ж)", "Минчанка (ж)", ["1.5", "4", "6"])]
    assert find_event(markets, "olimpbet", parse_manual("Динамо Краснодар — Минчанка 1.5 4 6"))[0] == []
    assert find_event(markets, "olimpbet", parse_manual("Спартак — ЦСКА 2 3 4"))[0] == []
    # 2 odds must not be matched to a 3-way football market
    assert find_event(markets, "olimpbet", parse_manual("Динамо Краснодар (ж) — Минчанка (ж) 1.5 2.5"))[0] == []


def test_manual_picks_nearest_of_two_meetings():
    far = live_1x2("1xbet", "Ростов", "Крылья Советов", ["2", "3.5", "4"], hours=200)
    near = live_1x2("oinabet", "Ростов", "Крылья Советов", ["2", "3.5", "4"], hours=20)
    found, _ = find_event([far, near], "olimpbet", parse_manual("Ростов — Крылья Советов 2 3.5 4"))
    assert near in found and far not in found
