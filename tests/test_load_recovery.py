"""Load (many users at once) and recovery (failing scan / source) behaviour."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.main import Service
from app.telegram.bot import ThrottleMiddleware


def bare_service(scanner):
    svc = Service.__new__(Service)
    svc.scanner, svc.providers, svc.live, svc.gone = scanner, {}, {}, []
    svc._scan_lock, svc._last_rep, svc._last_rep_at = asyncio.Lock(), None, 0.0
    svc.counters = {"scans": 0, "cache_hits": 0, "waited": 0, "errors": 0, "notified": 0}
    svc.last_success_at, svc.last_error = None, ""

    class Repo:
        async def save_scan(self, *a):
            pass

        async def save_matches(self, *a):
            pass
    svc.repo = Repo()
    svc.scanner.last_groups = []
    return svc


class Rep:
    verified, rejected = [], []


@pytest.mark.asyncio
async def test_500_concurrent_users_trigger_one_scan(monkeypatch):
    import app.main as main_mod

    async def no_fx():
        return None
    monkeypatch.setattr(main_mod, "load_fx", no_fx)
    calls = []

    class Scanner:
        async def run(self):
            calls.append(1)
            await asyncio.sleep(0.2)
            return Rep()
    svc = bare_service(Scanner())
    t0 = time.monotonic()
    res = await asyncio.gather(*(svc.scan_shared(max_age=90) for _ in range(500)))
    assert len(calls) == 1 and all(r[0] is res[0][0] for r in res)
    assert time.monotonic() - t0 < 2  # one scan + 499 waiters, not 500 scans
    assert svc.counters["scans"] == 1 and svc.counters["cache_hits"] == 499


@pytest.mark.asyncio
async def test_failed_scan_is_not_replaced_by_stale_data(monkeypatch):
    import app.main as main_mod

    async def no_fx():
        return None
    monkeypatch.setattr(main_mod, "load_fx", no_fx)
    state = {"fail": True}

    class Scanner:
        async def run(self):
            if state["fail"]:
                raise ConnectionError("network down")
            return Rep()
    svc = bare_service(Scanner())
    with pytest.raises(ConnectionError):
        await svc.scan_shared(max_age=90)
    assert svc.last_success_at is None and svc.counters["errors"] == 1
    state["fail"] = False  # network is back → normal service resumes
    rep, cached = await svc.scan_shared(max_age=90)
    assert not cached and svc.last_success_at is not None


def test_throttle_handles_many_users_fast():
    t = ThrottleMiddleware()
    t0 = time.monotonic()
    for uid in range(20_000):
        assert t.allow(uid, 1.0)
    assert time.monotonic() - t0 < 2


@pytest.mark.asyncio
async def test_dom_source_backs_off_after_block():
    from app.providers.bookmakers.common.conftest_helpers import FETCHED
    from app.providers.bookmakers.parimatch.provider import ParimatchProvider
    from app.providers.browser.zendriver_client import PageResult, ZendriverClient

    class B(ZendriverClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def fetch(self, url, **kw):
            self.calls += 1
            return PageResult(url=url, http_status=403, blocked="http_403", error="HTTP 403", fetched_at=FETCHED)
    b = B()
    p = ParimatchProvider(browser=b)
    await p.fetch_markets()
    assert b.calls == 1  # first 403 stops the pass: the other 3 pages are not opened
    await p.fetch_markets()
    assert b.calls == 1  # within the 30-min back-off nothing is requested again
    assert p.report.status == "BLOCKED"
