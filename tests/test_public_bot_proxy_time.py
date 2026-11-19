"""Public bot protections, proxy manager policy, Almaty time, Odds API credit guard, access gate."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.providers.bookmakers.common.conftest_helpers import FETCHED
from app.providers.browser.zendriver_client import PageResult, ZendriverClient
from app.proxy_manager.manager import ProxyManager
from app.proxy_manager.models import ProxyCandidate, ProxyStats
from app.proxy_manager.sources import ProxyToolsSource, SourceUnavailable, StaticSource, parse_api
from app.telegram.bot import ThrottleMiddleware
from app.timeutil import almaty, fmt_almaty

FIX = Path(__file__).resolve().parents[1] / "app/providers/bookmakers/fonbet/tests/fixtures/fonbet_en_full.html"


# ---------------------------------------------------------------- throttling
def test_throttle_burst_and_minute_limits():
    t = ThrottleMiddleware(burst=3, burst_window=3, per_minute=5)
    assert [t.allow(1, 0.0 + i * 0.1) for i in range(4)] == [True, True, True, False]  # burst
    assert t.allow(2, 0.0)  # other users are independent
    assert t.allow(1, 10) and t.allow(1, 20)  # 5 within a minute
    assert not t.allow(1, 30)  # 6th in the same minute
    assert t.allow(1, 71)  # window slid


# ---------------------------------------------------------------- time
def test_almaty_time_independent_of_server_tz():
    naive_utc_from_db = datetime(2026, 11, 19, 20, 30)  # SQLite returns naive UTC
    assert almaty(naive_utc_from_db).strftime("%d.%m %H:%M") == "20.11 01:30"  # date rolls over
    assert fmt_almaty(datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc), "%H:%M") == "17:00"
    assert fmt_almaty(None) == "—"


# ---------------------------------------------------------------- proxy manager
@pytest.mark.asyncio
async def test_proxy_tools_without_pro_key_is_not_used():
    with pytest.raises(SourceUnavailable, match="Pro Status"):
        await ProxyToolsSource("").fetch()


def test_parse_api_keeps_only_socks5_https():
    data = {"data": [{"ip": "1.2.3.4", "port": 1080, "type": "SOCKS5", "stability": "95%"},
                     {"ip": "5.6.7.8", "port": 8080, "type": "HTTP"},
                     {"ip": "9.9.9.9", "port": "x", "type": "HTTPS"},
                     {"ip": "2.2.2.2", "port": 443, "type": "HTTPS", "response": "0.4"}]}
    got = [(c.host, c.protocol, c.source_stability) for c in parse_api(data, "socks5")]
    assert got == [("1.2.3.4", "socks5", 95.0), ("2.2.2.2", "https", None)]


@pytest.mark.asyncio
async def test_static_source_and_policy(tmp_path):
    src = StaticSource("socks5://10.0.0.1:1080, https://10.0.0.2:3128 bogus")
    cands = await src.fetch()
    assert [c.key for c in cands] == ["socks5://10.0.0.1:1080", "https://10.0.0.2:3128"]

    pm = ProxyManager(enabled=True, api_key="", static="x", allowed={"winline", "telegram", "the_odds_api"},
                      stats_file=tmp_path / "s.json")
    assert pm.allowed == {"winline"}  # Telegram / API-key providers are never proxied
    fast = ProxyCandidate(host="10.0.0.2", port=3128, protocol="https")
    slow = ProxyCandidate(host="10.0.0.1", port=1080, protocol="socks5")
    pm.candidates = {fast.key: fast, slow.key: slow}
    pm.stats = {fast.key: ProxyStats(key=fast.key, ok=10, last_latency_ms=120, active=True, anonymity="anonymous"),
                slow.key: ProxyStats(key=slow.key, ok=10, last_latency_ms=400, active=True, anonymity="anonymous")}
    assert pm.proxy_for("winline") == "socks5://10.0.0.1:1080"  # SOCKS5 preferred
    assert pm.proxy_for("fonbet") is None  # not in the allow-list
    assert pm.proxy_for("telegram") is None
    pm.mark_blocked("winline", "HTTP 403")
    assert pm.proxy_for("winline") is None  # a block is never routed around
    assert not ProxyManager(enabled=False, allowed={"winline"}, stats_file=tmp_path / "t.json").proxy_for("winline")


def test_proxy_stability_threshold(tmp_path):
    pm = ProxyManager(enabled=True, static="x", stats_file=tmp_path / "s.json")
    assert pm._qualifies(ProxyStats(key="k", ok=9, fail=1, last_latency_ms=300, anonymity="anonymous"))
    assert not pm._qualifies(ProxyStats(key="k", ok=9, fail=1, last_latency_ms=300, anonymity="transparent"))
    assert not pm._qualifies(ProxyStats(key="k", ok=7, fail=3, last_latency_ms=300))  # 70% < 80%
    assert not pm._qualifies(ProxyStats(key="k", ok=10, last_latency_ms=1500))  # slower than 1 s


# ---------------------------------------------------------------- fonbet served in English (server outside KZ)
def test_fonbet_english_page():
    from app.providers.bookmakers.fonbet.parser import columns_to_groups, parse

    assert columns_to_groups(["1", "x", "2", "1", "2", "total", "o", "u"])[3] == ("handicap", "Ф1")
    evs = parse(FIX.read_text(encoding="utf-8"), "main", FETCHED)
    assert len(evs) >= 10
    e = evs[0]
    assert e.home == "Rostov" and e.start_time.isoformat() == "2026-11-20T16:30:00+00:00"  # "Tomorrow at 21:30"
    assert {m.kind for m in e.markets} == {"1x2", "handicap", "total"}


# ---------------------------------------------------------------- odds api credit guard
def test_odds_api_daily_cap(tmp_path, monkeypatch):
    from app.providers.aggregators.the_odds_api import TheOddsApiProvider

    monkeypatch.setattr(TheOddsApiProvider, "LEDGER", tmp_path / "credits.json")
    p = TheOddsApiProvider()
    p.last_headers = {"x-requests-remaining": "408", "x-requests-last": "2"}
    assert p._track_credits() == 408
    d = p.credits()
    assert d["used_today"] == 2 and d["remaining"] == 408 and d["daily_cap"] >= 1
    p._track_credits()  # same response is not counted twice
    assert p.credits()["used_today"] == 2
    led = p._ledger()
    led["used_today"] = d["daily_cap"]
    p._save_ledger(led)
    assert not p._budget_ok()  # daily budget spent → cache only
    p.last_headers = {"x-requests-remaining": "40", "x-requests-last": "1"}
    p._track_credits()
    assert not p._budget_ok() and "мало кредитов" in p.status.message  # below reserve


# ---------------------------------------------------------------- access gate
class _FakeBrowser(ZendriverClient):
    def __init__(self, res):
        super().__init__()
        self.res, self.calls = res, 0

    async def fetch(self, url, **kw):
        self.calls += 1
        return self.res


@pytest.mark.asyncio
async def test_access_gate_detects_geo_block_and_stops():
    from app.providers.bookmakers.common.access_gate import AccessGate

    b = _FakeBrowser(PageResult(url="https://1xbet.kz/ru/line/football", final_url="https://1xbet.kz/ru/block",
                                http_status=200, html="<html>blocked</html>"))
    g = AccessGate("1xbet", "https://1xbet.kz/ru/line/football", browser=b)
    v = await g.check()
    assert not v.ok and "блокировки" in v.reason
    await g.check()
    assert b.calls == 1  # verdict cached: no hammering, no retries
