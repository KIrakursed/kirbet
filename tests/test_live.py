"""Integration tests against REAL public APIs (no keys). Run: pytest -m live"""

import asyncio
from decimal import Decimal

import pytest

from app.domain import PriceKind
from app.providers.base import ProviderState
from app.providers.prediction_markets.kalshi import KalshiProvider
from app.providers.prediction_markets.polymarket import PolymarketProvider

pytestmark = pytest.mark.live


def test_polymarket_live():
    async def run():
        p = PolymarketProvider(config={"include_nonsport": True, "nonsport_events": 20})
        st = await p.health_check()
        assert st.state in (ProviderState.OK, ProviderState.DEGRADED)
        ms = await p.fetch_markets()
        assert ms, p.status.last_error
        q = next(q for m in ms for o in m.outcomes for q in o.quotes)
        assert q.kind == PriceKind.BINARY_CONTRACT and Decimal(0) < q.best_price < 1
        assert q.levels == sorted(q.levels, key=lambda lv: lv.price)
        m = next(m for m in ms if m.market_type.value == "match_winner")
        await p.refresh_market(m)
        assert any(o.quotes for o in m.outcomes)
        await p.close()
    asyncio.run(run())


def test_kalshi_live():
    async def run():
        k = KalshiProvider()
        st = await k.health_check()
        assert st.state != ProviderState.DOWN, st.last_error
        ms = await k.fetch_markets()
        assert k.status.events > 0
        if ms:
            m = ms[0]
            await k.refresh_market(m)
            for o in m.outcomes:
                for q in o.quotes:
                    assert Decimal(0) < q.best_price < 1
                    assert q.fee_model == "kalshi"
        await k.close()
    asyncio.run(run())


def test_1xbet_kz_live():
    """Success = a concrete event + market + fresh coefficient, not an HTML page."""
    from app.providers.bookmakers.onexbet_kz import OneXBetKzProvider

    async def run():
        p = OneXBetKzProvider(config={"sports": [4], "max_leagues_per_sport": 2})
        ms = await p.fetch_markets()
        assert ms, p.status.last_error
        m = ms[0]
        assert m.participants[0] and m.participants[1] and m.start_time
        q = m.outcomes[0].quotes[0]
        assert q.kind == PriceKind.DECIMAL_ODDS and q.best_price > 1 and q.currency == "KZT"
        assert q.age_seconds() < 60
        await p.refresh_market(m)
        await p.close()
    asyncio.run(run())


def test_ubet_kz_live():
    from app.providers.bookmakers.ubet_kz import UbetKzProvider

    async def run():
        p = UbetKzProvider()
        ms = await p.fetch_markets()
        assert ms, p.status.last_error
        m = ms[0]
        assert len(m.outcomes) == 3 and all(o.quotes[0].best_price > 1 for o in m.outcomes)
        await p.close()
    asyncio.run(run())


def test_oinabet_kz_live():
    """Real SignalR feed: concrete event + market + coefficient, and reconnect works."""
    from app.providers.bookmakers.oinabet_kz import OinabetKzProvider

    async def run():
        p = OinabetKzProvider(config={"sports": [1, 2]})
        ms = await p.fetch_markets()
        assert ms, p.status.last_error
        m = next(x for x in ms if x.category == "football")
        assert len(m.outcomes) == 3 and all(o.quotes[0].best_price > 1 for o in m.outcomes)
        await p.stream.close()  # simulate a dropped connection
        ms2 = await p.fetch_markets()
        assert ms2 and p.reconnects == 1
        await p.close()
    asyncio.run(run())
