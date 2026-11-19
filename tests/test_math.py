from decimal import Decimal as D

import pytest

from app.arbitrage import surebet
from app.arbitrage.optimizer import FX, optimize
from app.domain import Level, PriceKind
from app.execution_models.fees import kalshi_fee, polymarket_fee
from app.liquidity.orderbook import vwap_buy
from tests.helpers import bq, leg, oq


# 1. two-way surebet
def test_two_way_surebet():
    r = surebet.calculate([D("2.20"), D("2.10")], D("10000"))
    assert r.is_arb
    assert r.implied_sum < 1
    assert r.theoretical_roi_pct == D("7.4419")
    assert r.stakes == [D("4883"), D("5116")]  # matches spec example (rounded down to 1)
    assert r.min_profit > 0
    assert all(p > 0 for p in r.profits)


# 2. three-way
def test_three_way_surebet():
    r = surebet.calculate([D("3.5"), D("3.6"), D("3.4")], D("1000"))
    assert r.is_arb
    assert len(r.stakes) == 3 and r.min_profit > 0


# 3. N-way
def test_n_way_surebet():
    odds = [D("5.5")] * 5
    r = surebet.calculate(odds, D("1000"), rounding_step=D("0.01"))
    assert r.is_arb
    assert r.real_roi_pct == D("10.0000")


# 4. no arbitrage
def test_no_arbitrage():
    r = surebet.calculate([D("1.90"), D("1.90")], D("1000"))
    assert not r.is_arb
    assert r.min_profit < 0


def test_invalid_odds():
    with pytest.raises(ValueError):
        surebet.implied_sum([D("1.0"), D("2")])


# 5. commissions turn a binary near-arb negative
def test_fees_polymarket_formula_matches_docs():
    # docs table (sports, rate 0.05): 100 shares @0.50 -> $1.25 ; @0.30 -> $1.05
    p = {"enabled": True, "rate": "0.05", "exponent": 1}
    assert polymarket_fee(D("100"), D("0.50"), p) == D("1.25")
    assert polymarket_fee(D("100"), D("0.30"), p) == D("1.05")
    assert polymarket_fee(D("100"), D("0.30"), {"enabled": False, "rate": "0.05"}) == 0


def test_kalshi_fee_rounds_up_to_cent():
    # 0.07 * 10 * 0.5 * 0.5 = 0.175 -> 0.18
    assert kalshi_fee(D("10"), D("0.50"), {"rate": "0.07"}) == D("0.18")
    assert kalshi_fee(D("1"), D("0.99"), {"rate": "0.07"}) == D("0.01")


# 23. negative profit after fees
def test_negative_after_fees():
    legs = [leg(bq("0.49", fee="pm", iid="a"), "A"), leg(bq("0.495", fee="kalshi", platform="k", iid="b"), "B")]
    # raw sum 0.985 < 1 but fees eat it
    r = optimize(legs, ["A", "B"], capital=D("500"))
    assert r is not None and r.min_profit <= 0
    legs_nofee = [leg(bq("0.49", iid="a"), "A"), leg(bq("0.495", platform="k", iid="b"), "B")]
    r2 = optimize(legs_nofee, ["A", "B"], capital=D("500"))
    assert r2.min_profit > 0


# 6. rounding of stakes recomputes real profit
def test_rounding_recomputes_profit():
    r = surebet.calculate([D("2.03"), D("2.03")], D("101"), rounding_step=D("10"))
    assert r.total_staked == D("100")
    assert r.profits == [p.quantize(D("0.01")) for p in [D("50") * D("2.03") - 100] * 2]


# 7. min / max stakes
def test_min_max_stakes():
    r = surebet.calculate([D("2.2"), D("2.1")], D("10000"), max_stakes=[D("1000"), None])
    assert r.stakes[0] <= 1000
    assert any("лимит" in w for w in r.warnings)
    r2 = surebet.calculate([D("2.2"), D("2.1")], D("100"), min_stakes=[D("100"), D("100")])
    assert any("меньше минимальной" in w for w in r2.warnings)


def test_bookmaker_max_stake_in_optimizer():
    legs = [leg(oq("2.2", max_stake="100", iid="a"), "A"), leg(oq("2.1", iid="b"), "B")]
    r = optimize(legs, ["A", "B"], capital=D("10000"))
    a = next(f for f in r.legs if f.leg.quote.instrument_id == "a")
    assert a.units <= 100
    assert r.limited_by == "liquidity"
    assert r.min_profit > 0


# 8. insufficient liquidity: only 5 contracts at 0.30
def test_insufficient_liquidity_uses_vwap():
    a_levels = [Level(D("0.30"), D("5")), Level(D("0.60"), D("1000"))]
    legs = [leg(bq("0.30", levels=a_levels, iid="a"), "A"), leg(bq("0.60", iid="b"), "B")]
    r = optimize(legs, ["A", "B"], capital=D("1000"))
    # profitable only for the first 5 contracts; optimizer must not scale to 1000
    assert r.payout_units <= D("5.01")
    fe = vwap_buy(a_levels, D("100"))
    assert fe.vwap > D("0.30") and fe.slippage > 0


# 17. partial execution
def test_partial_fill_estimate():
    fe = vwap_buy([Level(D("0.4"), D("10"))], D("50"))
    assert fe.partial and fe.filled == D("10")


def test_optimizer_returns_none_when_depth_missing():
    legs = [leg(bq("0.3", size="0", iid="a"), "A"), leg(bq("0.6", iid="b"), "B")]
    r = optimize(legs, ["A", "B"], capital=D("100"))
    assert r is None


# 18. back / lay commission
def test_back_lay_with_commission():
    back = oq("2.20", platform="bk", iid="back")  # bookmaker back on A
    lay = oq("2.00", platform="bf", kind=PriceKind.EXCHANGE_LAY, commission="0.05", iid="lay")
    legs = [leg(back, "A"), leg(lay, "notA")]
    r = optimize(legs, ["A", "notA"], capital=D("1000"))
    assert r.min_profit > 0
    # with 25% commission the lay side no longer works
    lay_hi = oq("2.00", platform="bf", kind=PriceKind.EXCHANGE_LAY, commission="0.5", iid="lay2")
    r2 = optimize([leg(back, "A"), leg(lay_hi, "notA")], ["A", "notA"], capital=D("1000"))
    assert r2.min_profit < 0


def test_lay_is_not_back():
    from app.execution_models.legs import unit_cost, unit_payout
    lay = oq("3.0", kind=PriceKind.EXCHANGE_LAY, commission="0.05")
    lg = leg(lay, "x")
    assert unit_cost(lg, D("3.0")) == D("2.0")  # liability per 1 unit of backer stake
    assert unit_payout(lg, D("3.0")) == D("2.95")


# 19. YES/NO contracts: buy YES on one platform and NO on the other
def test_yes_no_contracts():
    yes = bq("0.40", platform="pm", side="YES", iid="yes", fee="pm")
    no = bq("0.55", platform="k", side="NO", iid="no", fee="kalshi")
    r = optimize([leg(yes, "Y"), leg(no, "N")], ["Y", "N"], capital=D("100"))
    assert r.is_arb
    assert set(r.scenarios) == {"Y", "N"}
    assert r.min_profit == min(r.scenarios.values())


def test_uncovered_scenario_raises():
    with pytest.raises(ValueError):
        optimize([leg(bq("0.4", iid="a"), "A")], ["A", "B"], capital=D("100"))


def test_currency_conversion():
    fx = FX("USD", {"KZT": D("1") / D("500")})
    kzt = oq("2.2", iid="kzt")
    kzt.currency = "KZT"
    usd = bq("0.40", iid="usd")
    r = optimize([leg(kzt, "A"), leg(usd, "B")], ["A", "B"], capital=D("100"), fx=fx)
    f = next(x for x in r.legs if x.leg.quote.currency == "KZT")
    assert f.units > 1000  # stake in tenge
    assert r.min_profit > 0
