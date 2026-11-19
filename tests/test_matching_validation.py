import asyncio
from datetime import timedelta
from decimal import Decimal as D

from app.arbitrage.finder import Finder
from app.arbitrage.optimizer import FX
from app.domain import MarketType, OppStatus, RetirementRule, SettlementProfile, VoidRule, utcnow
from app.matching.engine import group_markets, match_markets, settlement_conflicts
from app.normalization.names import parse_best_of, similarity
from app.providers.base import BaseProvider, ProviderState
from app.validation.engine import ValidationSettings, Validator
from tests.helpers import NOW, TENNIS_BK, TENNIS_K, TENNIS_PM, h2h_market


def test_name_similarity():
    assert similarity("Alexander Zverev", "A. Zverev", person=True) >= 0.95
    assert similarity("Zverev A.", "Alexander Zverev", person=True) >= 0.95
    assert similarity("Andy Murray", "Andy Murphy", person=True) < 0.85
    assert similarity("NAVI", "Natus Vincere") == 1.0
    assert similarity("Team Spirit", "Spirit") >= 0.95
    assert parse_best_of("CS2: A vs B (BO3) - Cup") == 3


def test_match_same_event_across_platforms():
    a = h2h_market("polymarket", "Alexander Zverev", "Ben Shelton", "0.6", "0.4")
    b = h2h_market("kalshi", "Ben Shelton", "Alexander Zverev", "0.4", "0.6", settlement=TENNIS_K,
                   start=NOW + timedelta(hours=11))
    r = match_markets(a, b)
    assert r is not None and r.confidence >= 0.9
    assert r.outcome_map["alexander zverev"] == "alexander zverev"


# 11. event mismatch
def test_event_mismatch():
    a = h2h_market("polymarket", "Alexander Zverev", "Ben Shelton", "0.6", "0.4")
    b = h2h_market("kalshi", "Alexander Zverev", "Taylor Fritz", "0.6", "0.4")
    assert match_markets(a, b) is None
    c = h2h_market("kalshi", "Alexander Zverev", "Ben Shelton", "0.6", "0.4", start=NOW + timedelta(days=4))
    assert match_markets(a, c) is None  # same players, different day


# 12. market mismatch: match winner vs set winner vs map winner
def test_market_mismatch():
    a = h2h_market("polymarket", "A Player", "B Player", "0.6", "0.4")
    b = h2h_market("kalshi", "A Player", "B Player", "0.6", "0.4", mtype=MarketType.SET_WINNER, period="set1")
    assert match_markets(a, b) is None
    c = h2h_market("polymarket", "NAVI", "G2", "0.5", "0.5", category="cs2", mtype=MarketType.MAP_WINNER, period="map1")
    d = h2h_market("kalshi", "NAVI", "G2", "0.5", "0.5", category="cs2")
    assert match_markets(c, d) is None
    e = h2h_market("polymarket", "NAVI", "G2", "0.5", "0.5", category="cs2", best_of=3)
    f = h2h_market("kalshi", "NAVI", "G2", "0.5", "0.5", category="cs2", best_of=5)
    assert match_markets(e, f) is None


# 13 + 14. settlement rule differences, tennis retirement
def test_settlement_retirement_mismatch_blocks():
    blocking, warns = settlement_conflicts([("polymarket", TENNIS_PM), ("bk", TENNIS_BK)], "tennis")
    assert blocking and "retirement" in blocking[0]
    blocking2, warns2 = settlement_conflicts([("polymarket", TENNIS_PM), ("kalshi", TENNIS_K)], "tennis")
    assert not blocking2 and warns2  # same retirement rule, walkover differs -> warning only


def _finder():
    return Finder(capital_usd=D("1000"), fx=FX())


def test_cross_finder_finds_arb_and_settlement_mismatch_flagged():
    a = h2h_market("polymarket", "Alexander Zverev", "Ben Shelton", "0.55", "0.47")
    b = h2h_market("bk", "Alexander Zverev", "Ben Shelton", "0.60", "0.40", settlement=TENNIS_BK)
    groups = [g for g in group_markets([a, b]) if len(g.markets) > 1]
    assert len(groups) == 1
    opps = _finder().from_group(groups[0])
    assert opps
    assert all(o.blocking for o in opps)  # bookmaker voids on retirement → blocked


# 15. draw in BO2
def test_bo2_draw_blocks_two_way():
    a = h2h_market("polymarket", "NAVI", "G2", "0.45", "0.45", category="cs2", best_of=2, draw=True)
    opps = _finder().from_market(a)
    assert opps and all(any("ничья" in b for b in o.blocking) for o in opps)


class FakeProvider(BaseProvider):
    name = "fake"

    def __init__(self, markets_after_refresh=None, fail=False, state=ProviderState.OK, region=""):
        super().__init__(config={})
        self.after = markets_after_refresh
        self.fail = fail
        self.status.state = state
        self.status.region_note = region
        self.calls = 0

    async def refresh_market(self, market):
        self.calls += 1
        if self.fail:
            raise RuntimeError("API 500")
        if self.after:
            for oc, prices in zip(market.outcomes, self.after):
                q = oc.quotes[0]
                q.best_price = D(prices)
                from app.domain import Level
                q.levels = [Level(D(prices), q.levels[0].size)]
                q.fetched_at = utcnow()
        return market


def _cross_opp(pa="0.45", pb="0.45", start=None):
    a = h2h_market("polymarket", "Alexander Zverev", "Ben Shelton", pa, "0.60", start=start)
    b = h2h_market("kalshi", "Alexander Zverev", "Ben Shelton", "0.60", pb, settlement=TENNIS_K, start=start)
    g = [g for g in group_markets([a, b]) if len(g.markets) > 1][0]
    opps = _finder().from_group(g)
    assert opps
    return opps[0]


def _validator(**provs):
    return Validator(provs, FX(), ValidationSettings(min_roi_pct=D("0.5"), max_quote_age_s=20))


def test_verified_after_recheck():
    o = _cross_opp()
    v = _validator(polymarket=FakeProvider(), kalshi=FakeProvider())
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.VERIFIED, o.log
    statuses = [s for s, _ in o.log]
    assert statuses[:5] == ["NEW", "MATCHING", "VALIDATING", "RECHECKING", "VERIFIED"][: len(statuses[:5])]


# 10. odds change before notification
def test_price_change_before_notify_rejects():
    o = _cross_opp()
    v = _validator(polymarket=FakeProvider(["0.55", "0.45"]), kalshi=FakeProvider(["0.6", "0.50"]))
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.REJECTED


# 9. stale odds
def test_stale_quotes():
    o = _cross_opp()
    for lg in o.legs:
        lg.quote.fetched_at = utcnow() - timedelta(minutes=5)

    class NoRefresh(FakeProvider):
        async def refresh_market(self, market):
            return market

    v = _validator(polymarket=NoRefresh(), kalshi=NoRefresh())
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.STALE


# 16. cancelled / finished event
def test_expired_event():
    o = _cross_opp(start=NOW - timedelta(hours=12))
    v = _validator(polymarket=FakeProvider(), kalshi=FakeProvider())
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.EXPIRED


def test_quote_disappeared_expires():
    o = _cross_opp()

    class Gone(FakeProvider):
        async def refresh_market(self, market):
            for oc in market.outcomes:
                oc.quotes = []
            return market

    v = _validator(polymarket=Gone(), kalshi=FakeProvider())
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.EXPIRED


# 20 + 21. source unavailable / API error
def test_source_down_and_api_error():
    o = _cross_opp()
    v = _validator(polymarket=FakeProvider(state=ProviderState.DOWN), kalshi=FakeProvider())
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.UNAVAILABLE
    o2 = _cross_opp()
    v2 = _validator(polymarket=FakeProvider(fail=True), kalshi=FakeProvider())
    asyncio.run(v2.validate(o2))
    assert o2.status == OppStatus.UNAVAILABLE


# 24. platform not available in region
def test_region_blocked():
    o = _cross_opp()
    v = _validator(polymarket=FakeProvider(region="Страна по IP: XX; ТОРГОВЛЯ ЗАБЛОКИРОВАНА"), kalshi=FakeProvider())
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.UNAVAILABLE


def test_low_confidence_rejected():
    o = _cross_opp()
    o.confidence = 0.7
    v = _validator(polymarket=FakeProvider(), kalshi=FakeProvider())
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.REJECTED


def test_non_exhaustive_multi_outcome_blocked():
    from app.domain import Market, Outcome, PlatformType
    from tests.helpers import bq
    m = Market("polymarket", PlatformType.PREDICTION_MARKET, "ev1", "ev1", "Who wins?", "politics",
               MarketType.MULTI_OUTCOME, [Outcome(k, k, [bq("0.3", iid=k)]) for k in ("a", "b", "c")],
               exhaustive=False, settlement=SettlementProfile(VoidRule.FIFTY_FIFTY, RetirementRule.ADVANCING_PLAYER))
    opps = _finder().from_market(m)
    assert opps and opps[0].blocking


# regression: a LAY on X must cover "X does not win"
def test_lay_covers_complement_not_same_outcome():
    from app.domain import Market, Outcome, PlatformType, PriceKind
    from tests.helpers import oq
    back_a = oq("2.2", platform="bf", iid="backA")
    lay_b = oq("1.84", platform="bf", kind=PriceKind.EXCHANGE_LAY, commission="0.05", iid="layB")
    back_b = oq("1.9", platform="bf", iid="backB")
    m = Market("bf", PlatformType.EXCHANGE, "m", "e", "A vs B", "tennis", MarketType.MATCH_WINNER,
               [Outcome("a", "A", [back_a]), Outcome("b", "B", [back_b, lay_b])], start_time=NOW + timedelta(hours=3),
               participants=("A", "B"), settlement=TENNIS_K)
    opps = _finder().from_market(m)
    for o in opps:
        layleg = [lg for lg in o.legs if lg.quote.kind == PriceKind.EXCHANGE_LAY]
        assert all(lg.covers == frozenset({"a"}) for lg in layleg)
        # back A + lay B both win on A -> never a valid combination
        assert {lg.quote.instrument_id for lg in o.legs} != {"backA", "layB"}


def test_high_roi_flagged_and_absurd_rejected():
    o = _cross_opp(pa="0.20", pb="0.20")  # ~150%: shown, but as a risk
    asyncio.run(_validator(polymarket=FakeProvider(), kalshi=FakeProvider()).validate(o))
    assert o.status == OppStatus.VERIFIED
    assert any("необычно высокая доходность" in w for w in o.warnings)
    o2 = _cross_opp(pa="0.04", pb="0.04")  # >1000%: data error
    asyncio.run(_validator(polymarket=FakeProvider(), kalshi=FakeProvider()).validate(o2))
    assert o2.status == OppStatus.REJECTED and "аномальный" in o2.log[-1][1]


def test_live_event_with_aggregator_leg_rejected():
    o = _cross_opp(start=NOW - timedelta(minutes=30))
    o.legs[0].quote.depth_known = False
    for f in o.result.legs:
        if f.leg.quote.instrument_id == o.legs[0].quote.instrument_id:
            f.leg.quote.depth_known = False

    class Keep(FakeProvider):
        async def refresh_market(self, market):
            for oc in market.outcomes:
                for q in oc.quotes:
                    q.fetched_at = utcnow()
            return market

    v = _validator(polymarket=Keep(), kalshi=Keep())
    asyncio.run(v.validate(o))
    assert o.status == OppStatus.REJECTED and "live" in o.log[-1][1]
