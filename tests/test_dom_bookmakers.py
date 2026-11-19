"""Browser provider + DOM adapter plumbing (no network: the browser is faked)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.providers.base import ProviderState
from app.providers.bookmakers.common.conftest_helpers import FETCHED, fixture
from app.providers.bookmakers.common.textutil import parse_odds, parse_ru_datetime, sport_slug
from app.providers.bookmakers.fonbet.provider import FonbetProvider
from app.providers.bookmakers.tennisi.provider import TennisiProvider
from app.providers.bookmakers.winline.provider import WinlineProvider
from app.providers.browser.zendriver_client import PageResult, RobotsRules, ZendriverClient

TENNISI_ROBOTS = "User-agent: Yandex\nDisallow: *?*\nAllow: /mtg2/cgi/!FREE.Welcome\n\nUser-agent: *\nDisallow: *?*\nAllow: /*.css\n"
WINLINE_ROBOTS = "User-Agent: *\nDisallow: /?\nDisallow: /*?\nDisallow: */stats/*\n"
PARIMATCH_ROBOTS = "User-agent: *\nDisallow: /en/\nDisallow: */direct-feed/api/eventcontent/\nAllow: /ru/football/\n"


def test_robots_wildcards():
    r = RobotsRules(TENNISI_ROBOTS)
    assert r.allowed("https://tennisi.kz/")[0]
    ok, rule = r.allowed("https://tennisi.kz/mtg2/cgi/free.Welcome?lang=rus")
    assert not ok and rule == "Disallow: *?*"
    w = RobotsRules(WINLINE_ROBOTS)
    assert w.allowed("https://winline.kz/")[0] and not w.allowed("https://winline.kz/sb/api/x?a=1")[0]
    p = RobotsRules(PARIMATCH_ROBOTS)
    assert p.allowed("https://parimatch.kz/ru/football/")[0]
    assert not p.allowed("https://parimatch.kz/ws/direct-feed/api/eventcontent/1")[0]


def test_text_helpers():
    assert parse_odds("1,85") == parse_odds("1.85") and parse_odds("1") is None and parse_odds("—") is None
    assert parse_ru_datetime("Сегодня 17:45", FETCHED).isoformat() == "2026-11-19T12:45:00+00:00"
    assert parse_ru_datetime("21 ноября в 15:00", FETCHED).isoformat() == "2026-11-21T10:00:00+00:00"
    assert parse_ru_datetime("tomorrow, 03:30", FETCHED).isoformat() == "2026-11-19T22:30:00+00:00"
    assert parse_ru_datetime("5 января 12:00", FETCHED).year == 2027
    assert sport_slug("Настольный теннис") == "table_tennis" and sport_slug("ice-hockey") == "hockey"


class FakeBrowser(ZendriverClient):
    def __init__(self, pages: dict[str, PageResult]):
        super().__init__()
        self.pages = pages

    async def fetch(self, url, **kw):
        return self.pages[url]


@pytest.mark.asyncio
async def test_ok_only_with_real_odds():
    res = PageResult(url="https://winline.kz/", final_url="https://winline.kz/", http_status=200,
                     html=fixture("winline"), fetched_at=FETCHED)
    empty = PageResult(url="https://winline.kz/sport/all", http_status=200, html="<html></html>", fetched_at=FETCHED)
    p = WinlineProvider(browser=FakeBrowser({"https://winline.kz/": res, "https://winline.kz/sport/all": empty}))
    markets = await p.fetch_markets()
    r = p.report
    assert r.status == "OK" and r.http_status == 200 and r.events == 5 and r.markets == 5
    assert "П1 = " in r.sample and r.timestamp == FETCHED
    assert p.status.state == ProviderState.OK and markets  # prematch winner markets go to the scanner


@pytest.mark.asyncio
async def test_page_without_odds_is_not_success():
    pages = {s.url: PageResult(url=s.url, http_status=200, html="<html><body>Линия</body></html>",
                               fetched_at=FETCHED) for s in FonbetProvider.pages}
    p = FonbetProvider(browser=FakeBrowser(pages))
    assert await p.fetch_markets() == []
    assert p.report.status == "NO_DATA" and "не найдено" in p.report.reason


@pytest.mark.asyncio
async def test_http_403_reported_as_blocked():
    pages = {s.url: PageResult(url=s.url, http_status=403, html="Forbidden", blocked="http_403",
                               error="HTTP 403 (доступ запрещён / антибот)", fetched_at=FETCHED)
             for s in FonbetProvider.pages}
    p = FonbetProvider(browser=FakeBrowser(pages))
    await p.fetch_markets()
    assert p.report.status == "BLOCKED" and p.report.http_status == 403 and "403" in p.report.reason
    assert p.status.state == ProviderState.UNAVAILABLE


@pytest.mark.asyncio
async def test_robots_redirect_reported():
    res = PageResult(url="https://tennisi.kz/", final_url="https://tennisi.kz/mtg2/cgi/free.Welcome?lang=rus",
                     http_status=200, blocked="robots", error="robots.txt запрещает (Disallow: *?*)",
                     fetched_at=datetime(2026, 11, 19, tzinfo=timezone.utc))
    p = TennisiProvider(browser=FakeBrowser({"https://tennisi.kz/": res}))
    await p.fetch_markets()
    assert p.report.status == "BLOCKED" and "*?*" in p.report.reason


def test_classify_challenge_page():
    res = PageResult(url="u", http_status=403, html="<div>If you are not a bot, please copy the report</div>")
    ZendriverClient._classify(res)
    assert res.blocked == "http_403" and "403" in res.error


def test_ubet_start_time_is_utc():
    from app.providers.bookmakers.ubet_kz import parse_match

    m = {"id": 1, "sport_id": 1, "teams": [{"name": "Оренбург"}, {"name": "Балтика"}],
         "started_at": "2026-11-21T10:00:00", "url": "/x",
         "stakes": {"1": [{"id": i, "name": n, "factor": f, "is_active": True}
                          for i, (n, f) in enumerate((("П1", 2.9), ("X", 3.2), ("П2", 2.5)))]}}
    mk = parse_match(m, FETCHED)
    assert mk.start_time.isoformat() == "2026-11-21T10:00:00+00:00"


@pytest.mark.asyncio
async def test_ubet_reads_feed_captured_by_browser():
    import json

    from app.providers.bookmakers.ubet_kz import FEED, HOME, UbetKzProvider

    payload = {"payload": [{"id": 7, "sport_id": 1, "teams": [{"name": "A"}, {"name": "B"}], "url": "/m/7",
                            "stakes": {"1": [{"id": i, "name": n, "factor": 2.0 + i, "is_active": True}
                                             for i, n in enumerate(("П1", "X", "П2"))]}}]}
    res = PageResult(url=HOME, http_status=200, captured={FEED: [json.dumps(payload)]})
    p = UbetKzProvider(browser=FakeBrowser({HOME: res}))
    ms = await p.fetch_markets()
    assert len(ms) == 1 and p.status.state == ProviderState.OK
    blocked = PageResult(url=HOME, http_status=403, blocked="http_403", error="HTTP 403")
    p2 = UbetKzProvider(browser=FakeBrowser({HOME: blocked}))
    assert await p2.fetch_markets() == [] and "403" in p2.status.message


@pytest.mark.asyncio
async def test_public_bot_shares_recent_scan():
    import asyncio

    from app.main import Service

    class Rep:
        verified, rejected = [], []

    calls = []

    class FakeScanner:
        async def run(self):
            calls.append(1)
            await asyncio.sleep(0.05)
            return Rep()

    class FakeRepo:
        async def save_scan(self, *a):
            pass

    svc = Service.__new__(Service)
    svc.__init__ = None
    svc.scanner, svc.repo, svc.providers, svc.live, svc.gone = FakeScanner(), FakeRepo(), {}, {}, []
    svc._scan_lock, svc._last_rep, svc._last_rep_at = asyncio.Lock(), None, 0.0
    svc.counters = {"scans": 0, "cache_hits": 0, "waited": 0, "errors": 0, "notified": 0}
    svc.last_success_at, svc.last_error = None, ""

    async def no_fx():
        return None

    import app.main as main_mod
    main_mod.load_fx, orig = no_fx, main_mod.load_fx
    try:
        r = await asyncio.gather(*(svc.scan_now(max_age=90) for _ in range(5)))
        assert len(calls) == 1 and all(x is r[0] for x in r)  # five users, one scan
        await svc.scan_now()  # the periodic loop always scans fresh
        assert len(calls) == 2
        assert svc.counters["scans"] == 2 and svc.counters["cache_hits"] == 4
    finally:
        main_mod.load_fx = orig


@pytest.mark.asyncio
async def test_ubet_snapshots_are_persisted_once_per_refresh(tmp_path):
    import json

    from app.database.repo import Repo
    from app.providers.bookmakers.ubet_kz import FEED, HOME, UbetKzProvider

    payload = {"payload": [{"id": 9, "sport_id": 1, "teams": [{"name": "Астана"}, {"name": "Кайрат"}],
                            "url": "/m/9", "started_at": "2026-11-21T10:00:00",
                            "stakes": {"1": [{"id": i, "name": n, "factor": f, "is_active": True}
                                             for i, (n, f) in enumerate((("П1", 2.1), ("X", 3.3), ("П2", 3.4)))]}}]}
    res = PageResult(url=HOME, http_status=200, captured={FEED: [json.dumps(payload)]})
    p = UbetKzProvider(browser=FakeBrowser({HOME: res}))
    await p.fetch_markets()
    events, rep = p.pop_unsaved()
    assert p.pop_unsaved() is None  # saved once per real page view
    assert events[0].home == "Астана" and str(events[0].markets[0].outcomes[1].odds) == "3.3"
    repo = Repo(f"sqlite+aiosqlite:///{(tmp_path / 'u.db').as_posix()}")
    await repo.init()
    assert await repo.save_bookmaker_events(events, rep) == 3
    await repo.close()
