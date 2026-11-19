"""Correctness of every amount and percent the user sees."""

import asyncio
import re
from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.arbitrage.finder import Finder
from app.arbitrage.optimizer import FX
from app.database.repo import Repo, category_group, platform_group
from app.domain import (
    Level,
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
from app.matching.engine import group_markets
from app.presentation.messages import ago, details_card, outcome_text, simple_card
from app.presentation.plan import PRESETS_KZT, Color, build_plan, plain_sentence, tenge

KZT_PER_USD = D("500")
FXR = FX("USD", {"KZT": D(1) / KZT_PER_USD})
NOW = utcnow()
FOOT = SettlementProfile(VoidRule.REFUND, RetirementRule.ADVANCING_PLAYER)


def bk_market(platform, a, b, oa, ob, *, currency="KZT"):
    def q(lbl, o):
        return Quote(platform=platform, kind=PriceKind.DECIMAL_ODDS, outcome_label=lbl, instrument_id=f"{platform}:{lbl}",
                     best_price=D(o), levels=[], depth_known=False, fee_params={"unit_step": "1"}, currency=currency,
                     side="BACK", url=f"https://{platform}.kz/e")
    return Market(platform, PlatformType.BOOKMAKER, f"{platform}-m", "e", f"{a} vs {b}", "tennis",
                  MarketType.MATCH_WINNER, [Outcome(a.lower(), a, [q(a, oa)]), Outcome(b.lower(), b, [q(b, ob)])],
                  start_time=NOW + timedelta(hours=6), participants=(a, b), settlement=FOOT)


def verified_opp(m1, m2):
    g = [g for g in group_markets([m1, m2]) if len(g.markets) > 1][0]
    opps = Finder(capital_usd=D("1000"), fx=FXR).from_group(g)
    assert opps
    o = max(opps, key=lambda x: x.result.roi_pct)
    o.set(OppStatus.VERIFIED)
    o.verified_at = utcnow()
    return o


@pytest.fixture
def two_books():
    return verified_opp(bk_market("olimpbet", "Ivan Petrov", "Oleg Smirnov", "2.20", "1.90"),
                        bk_market("1xbet", "Ivan Petrov", "Oleg Smirnov", "1.80", "2.15"))


def test_plan_amounts_add_up(two_books):
    plan = build_plan(two_books, FXR, D("100000"))
    stakes = [lp.stake_kzt for lp in plan.legs]
    assert all(s == s.to_integral_value() for s in stakes)  # whole tenge
    assert sum(stakes) == plan.total_kzt <= D("100000")
    assert plan.total_kzt >= D("99990")
    for lp in plan.legs:  # payout = stake × odds (rounded to tenge)
        assert lp.payout_kzt == tenge(lp.stake_kzt * lp.price)
    for s, pay in plan.payouts_kzt.items():
        assert plan.profits_kzt[s] == pay - plan.total_kzt
    assert plan.profit_kzt == min(plan.profits_kzt.values())
    assert plan.roi_pct == plan.profit_kzt / plan.total_kzt * 100
    # classic formula: S = 1/2.2 + 1/2.15 -> ROI ≈ 8.74%
    s = 1 / D("2.20") + 1 / D("2.15")
    assert abs(plan.roi_pct - (1 / s - 1) * 100) < D("0.01")
    assert plan.color == Color.GREEN


def _kzt_numbers(text: str) -> list[D]:
    return [D(x.replace(" ", "").replace(" ", "")) for x in re.findall(r"([\d ]+) ₸", text)]


def test_card_numbers_match_plan(two_books):
    plan = build_plan(two_books, FXR, D("100000"))
    text = simple_card(two_books, plan)
    for lp in plan.legs:
        assert f"Ставка: <b>{format(int(lp.stake_kzt), ',').replace(',', ' ')} ₸</b>" in text
    assert f"Общая сумма ставок: {format(int(plan.total_kzt), ',').replace(',', ' ')} ₸" in text
    profit_line = re.search(r"ЧИСТАЯ ПРИБЫЛЬ: \+([\d ]+) ₸", text).group(1)
    assert D(profit_line.replace(" ", "")) == plan.profit_kzt
    pct = D(re.search(r"ПРИБЫЛЬ В ПРОЦЕНТАХ: \+([\d.]+)%", text).group(1))
    assert pct == plan.roi_pct.quantize(D("0.01"))
    # what returns − what was staked == profit shown
    ret = D(re.search(r"Сколько вернётся: (?:от )?([\d ]+) ₸", text).group(1).replace(" ", ""))
    assert ret - plan.total_kzt == plan.profit_kzt
    assert "ТВОЙ КАПИТАЛ: <b>100 000 ₸</b>" in text
    assert "OLIMPBET" in text and "1XBET" in text and "Победа Ivan Petrov" in text
    assert "Это расчётная возможность" in text
    sentence = plain_sentence(plan)
    assert sentence.startswith("Вложить ") and "на OLIMPBET" in sentence and "на 1XBET" in sentence


@pytest.mark.parametrize("amount", PRESETS_KZT)
def test_presets_recalculate(two_books, amount):
    plan = build_plan(two_books, FXR, D(amount))
    assert plan.total_kzt <= amount
    assert plan.total_kzt >= amount - 5  # rounding to whole tenge loses at most a few tenge
    assert plan.profit_kzt > 0
    assert abs(plan.roi_pct - D("8.74")) < D("0.2")  # same opportunity → same percent at any size


def test_liquidity_limit_reported():
    pm_levels = [Level(D("0.40"), D("50"))]  # only 50 contracts at 0.40 = $20
    pm = Market("polymarket", PlatformType.PREDICTION_MARKET, "pm", "e", "Ivan Petrov vs Oleg Smirnov", "tennis",
                MarketType.MATCH_WINNER, [
                    Outcome("ivan petrov", "Ivan Petrov", [Quote("polymarket", PriceKind.BINARY_CONTRACT, "Ivan Petrov", "pmA",
                                                           D("0.40"), pm_levels, fee_params={"unit_step": "0.01"},
                                                           url="https://polymarket.com/event/x")]),
                    Outcome("oleg smirnov", "Oleg Smirnov", [Quote("polymarket", PriceKind.BINARY_CONTRACT, "Oleg Smirnov", "pmB",
                                                           D("0.70"), [Level(D("0.70"), D("1000"))],
                                                           fee_params={"unit_step": "0.01"})]),
                ], start_time=NOW + timedelta(hours=6), participants=("Ivan Petrov", "Oleg Smirnov"),
                settlement=SettlementProfile(VoidRule.FIFTY_FIFTY, RetirementRule.ADVANCING_PLAYER))
    bk = bk_market("1xbet", "Ivan Petrov", "Oleg Smirnov", "1.50", "1.85")
    o = verified_opp(pm, bk)
    plan = build_plan(o, FXR, D("250000"))
    assert plan.total_kzt < D("250000")
    assert "Доступно только" in plan.limited_note
    usd_leg = next(lp for lp in plan.legs if lp.currency == "USD")
    assert usd_leg.units <= D("50")
    assert abs(usd_leg.stake_kzt - usd_leg.stake_native * KZT_PER_USD) <= 1  # conversion shown correctly
    assert plan.fx_used and plan.kzt_per_usd == D("500.00")
    text = simple_card(o, plan)
    assert "Доступно только" in text and "контр." in text
    det = details_card(o, plan)
    det = details_card(o, build_plan(o, FXR, D("250000"), with_max_volume=True))
    for s in ("Теоретическая", "После комиссий", "С учётом доступного объёма", "В худшем сценарии",
              "Курс: 1 $ = 500.00 ₸", "Максимальный объём"):
        assert s in det


def test_colors(two_books):
    assert build_plan(two_books, FXR, D(10000)).color == Color.GREEN
    two_books.warnings.append("РИСК: тест")
    assert build_plan(two_books, FXR, D(10000)).color == Color.YELLOW
    two_books.set(OppStatus.STALE)
    assert build_plan(two_books, FXR, D(10000)).color == Color.GREY
    two_books.set(OppStatus.EXPIRED)
    assert build_plan(two_books, FXR, D(10000)).color == Color.RED


def test_negative_profit_is_red(two_books):
    # verified earlier, then a coefficient dropped: the card must turn red, not show a fake profit
    lg = two_books.legs[0]
    lg.quote.best_price = D("1.50")
    lg.quote.levels = []
    plan = build_plan(two_books, FXR, D("10000"))
    assert plan.profit_kzt <= 0 and plan.color == Color.RED
    assert "ВИЛКА ИСЧЕЗЛА" in simple_card(two_books, plan)


def test_wording_helpers():
    assert ago(2) == "2 секунды назад"
    assert ago(1) == "1 секунду назад"
    assert ago(11) == "11 секунд назад"
    assert ago(125) == "2 минуты назад"
    assert outcome_text("Draw", PriceKind.DECIMAL_ODDS, "match_winner") == "Ничья"
    assert outcome_text("Ann (NO на «Bea»)", PriceKind.BINARY_CONTRACT, "match_winner").startswith("Победа Ann")
    assert "LAY" in outcome_text("Ann", PriceKind.EXCHANGE_LAY, "match_winner")


def test_groups():
    assert platform_group("1xbet") == "kz" and platform_group("ubet") == "kz"
    assert platform_group("polymarket") == "polymarket" and platform_group("kalshi") == "other_pm"
    assert platform_group("betfair_ex_eu") == "exchanges" and platform_group("pinnacle") == "intl"
    assert category_group("cs2") == "esports" and category_group("finance") == "economics"
    assert category_group("weather") == "other"


def test_paper_trading_kept_separate(tmp_path, two_books):
    async def run():
        repo = Repo(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
        await repo.init()
        u = await repo.get_or_create_user(7)
        plan = build_plan(two_books, FXR, D("100000"))
        t1 = await repo.paper_add(u.id, two_books, plan, None)
        await repo.paper_add(u.id, two_books, build_plan(two_books, FXR, D("10000")), None)
        s = await repo.paper_summary(u.id)
        assert s["count"] == 2 and s["confirmed"] == 0 and s["confirmed_profit"] == 0
        assert s["simulated"] > 0 and s["theoretical"] >= s["simulated"] - 2
        await repo.paper_set(u.id, t1, "confirmed")
        s = await repo.paper_summary(u.id)
        assert s["confirmed"] == 1 and s["confirmed_profit"] == plan.profit_kzt
        assert await repo.paper_set(999, t1, "confirmed") is None  # other users can't touch it
        await repo.close()
    asyncio.run(run())


def test_filter_for_user(two_books):
    from types import SimpleNamespace

    from app.main import Service

    svc = Service.__new__(Service)
    svc.fx = FXR
    st = SimpleNamespace(platform_groups=["kz"], categories=["tennis"], min_roi_pct=D("1"), min_liquidity_kzt=D(0),
                         max_quote_age_s=20, capital_kzt=D("100000"))
    assert svc.filter_for_user([two_books], st) == [two_books]
    st.min_roi_pct = D("10")
    assert svc.filter_for_user([two_books], st) == []
    st.min_roi_pct, st.categories = D("0"), ["football"]
    assert svc.filter_for_user([two_books], st) == []
    st.categories, st.platform_groups = ["tennis"], ["polymarket"]
    assert svc.filter_for_user([two_books], st) == []
