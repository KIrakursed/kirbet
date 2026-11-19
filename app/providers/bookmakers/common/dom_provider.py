"""Base class for bookmaker adapters that read odds from the public, rendered DOM.

Flow per refresh: for each configured public page → robots.txt check → normal
page view in Chrome (Zendriver) → BeautifulSoup/lxml parser → Pydantic
validation → normalizer to domain ``Market``. A connection counts as OK only if
at least one event with name, participants, sport, market, outcome and a real
odds value was read. Everything else is reported with the precise reason.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from app.domain import Market, PlatformType, utcnow
from app.providers.base import BaseProvider, ProviderState
from app.providers.bookmakers.common import normalizer
from app.providers.bookmakers.common.models import AdapterReport, ParsedEvent
from app.providers.bookmakers.common.textutil import cyber_sport
from app.providers.browser.zendriver_client import PageResult, ZendriverClient
from app.timeutil import TZ_NAME

ODDS_READY_JS = r"/\b\d{1,2}[.,]\d{2}\b/.test(document.body && document.body.innerText || '')"


@dataclass
class PageSpec:
    url: str
    ready_js: str = ODDS_READY_JS
    scroll: int = 1
    settle: float = 2.0
    timeout: float = 40.0
    actions: list[tuple[str, str]] = field(default_factory=list)  # (label, js) → extra snapshots
    label: str = "main"  # passed to the parser with the page DOM (e.g. the sport of the page)


Parser = Callable[[str, str, datetime], list[ParsedEvent]]  # (html, page_label, fetched_at)


def event_metrics(events: list[ParsedEvent]) -> dict:
    """Counts for one refresh. "Unique" collapses the same match listed twice (e.g. on two pages)."""
    from app.normalization.names import latin_keys

    def key(e: ParsedEvent):
        names = tuple(sorted(min(latin_keys(n)) for n in (e.home, e.away)))
        return (e.sport, names, e.start_time.date() if e.start_time else e.start_text)

    return {
        "events": len(events),
        "unique_events": len({key(e) for e in events}),
        "prematch": sum(not e.is_live for e in events),
        "live": sum(e.is_live for e in events),
        "markets": sum(len(e.markets) for e in events),
        "odds": sum(len(m.outcomes) for e in events for m in e.markets),
        "markets_per_event": round(sum(len(e.markets) for e in events) / len(events), 2) if events else 0,
        "timezone_unknown": sum(e.timezone_unknown for e in events),
        "sports": dict(sorted(Counter(e.sport for e in events).items(), key=lambda kv: -kv[1])),
    }


class DomBookmakerProvider(BaseProvider):
    platform_type = PlatformType.BOOKMAKER
    default_rate = 0.1
    site: str = ""
    pages: list[PageSpec] = []
    parser: Parser
    include_live_in_scanner = False
    page_retries = 1  # a page that loaded but rendered no odds yet is viewed once more

    def __init__(self, browser: ZendriverClient | None = None, **kw):
        super().__init__(config=kw.pop("config", {}), **kw)
        self.browser = browser or ZendriverClient.shared()
        self.docs_url = self.status.docs = self.site
        self._events: list[ParsedEvent] = []
        self._cache_at = 0.0
        self._lock = asyncio.Lock()
        self.report: AdapterReport | None = None
        self._unsaved = False
        self.metrics_total = {"page_requests": 0, "page_ok": 0, "load_ms_sum": 0.0}
        self.metrics_last: dict = {}

    def default_config(self) -> dict:
        return {"min_refresh_seconds": 300, "blocked_backoff_seconds": 1800, "region_note": "Лицензированный оператор РК; публичная страница (DOM)"}

    # ---- collection -------------------------------------------------------------
    def parse_page(self, res: PageResult, spec: PageSpec) -> list[ParsedEvent]:
        out: list[ParsedEvent] = []
        docs = [(spec.label, res.html)] + list(res.snapshots.items())
        for label, html in docs:
            if html:
                out += self.parser(html, label, res.fetched_at)
        out = [e if (s := cyber_sport(e.sport, e.home, e.away)) == e.sport else e.model_copy(update={"sport": s})
               for e in out]
        if res.page_timezone != TZ_NAME:
            # parsers read "Сегодня 17:45" as Asia/Almaty, which holds only if the page rendered in it
            self.log.warning("%s rendered in time zone %r, not %s: start times marked timezone_unknown",
                             res.url, res.page_timezone, TZ_NAME)
            out = [e.model_copy(update={"start_time": None, "timezone_unknown": True}) for e in out]
        return out

    async def collect(self) -> tuple[list[ParsedEvent], AdapterReport]:
        results: list[PageResult] = []
        events: dict[str, ParsedEvent] = {}
        parse_errors: list[str] = []
        found: list[ParsedEvent] = []
        for spec in self.pages:
            for attempt in range(1 + self.page_retries):
                res = await self.browser.fetch(spec.url, ready_js=spec.ready_js, timeout=spec.timeout,
                                               settle=spec.settle, scroll=spec.scroll, actions=spec.actions)
                self.status.requests += 1
                self.metrics_total["page_requests"] += 1
                self.metrics_total["load_ms_sum"] += res.load_ms or 0
                if not res.error:
                    self.metrics_total["page_ok"] += 1
                if res.error:
                    self.status.errors += 1
                    break  # blocked (robots / 403 / challenge) or timed out: never retried right away
                try:
                    # HTML parsing is CPU work: run it in a worker thread so the Telegram bot keeps
                    # answering during a scan (it used to block the event loop for up to ~40 s)
                    parsed = await asyncio.to_thread(self.parse_page, res, spec)
                    found = [ev for ev in parsed if ev.is_complete()]
                except (ValidationError, ValueError) as e:
                    parse_errors.append(str(e)[:200])
                    found = []
                if found or attempt == self.page_retries:
                    break
                self.log.info("%s: no odds rendered yet, one more page view", spec.url)
            results.append(res)
            if res.blocked in ("http_403", "geo", "captcha", "cloudflare", "antibot", "auth"):
                # the site refused us (possibly for request rate): record it and stop — the other pages of the
                # same site are not opened in this pass; what was already read is kept
                break
            for ev in found if not res.error else []:
                prev = events.get(ev.external_id)
                if prev is None or len(ev.markets) > len(prev.markets):
                    events[ev.external_id] = ev
        return list(events.values()), self._report(results, list(events.values()), parse_errors)

    def _report(self, results: list[PageResult], events: list[ParsedEvent], parse_errors: list[str]) -> AdapterReport:
        first = next((r for r in results if r.http_status is not None), results[0] if results else None)
        rep = AdapterReport(
            bookmaker=self.name.upper(), status="ERROR", timestamp=utcnow(),
            http_status=first.http_status if first else None, urls=[r.url for r in results],
            load_ms=round(sum(r.load_ms or 0 for r in results)), events=len(events),
            markets=sum(len(e.markets) for e in events),
        )
        if events:
            ev = next((e for e in events if not e.is_live), events[0])
            m = ev.markets[0]
            o = m.outcomes[0]
            rep.status = "OK"
            rep.timestamp = ev.fetched_at
            rep.sample = f"{ev.name} | {ev.sport} | {m.name} | {o.name} = {o.odds}"
            bad = [r for r in results if r.error]
            if bad:
                rep.reason = "часть страниц недоступна: " + "; ".join(f"{r.url}: {r.error}" for r in bad)
            return rep
        blocked = [r for r in results if r.blocked]
        if results and len(blocked) == len(results):
            rep.status = "BLOCKED"
            rep.reason = "; ".join(f"{r.url}: {r.error}" for r in blocked)
        elif any(r.error for r in results):
            rep.status = "ERROR"
            rep.reason = "; ".join(f"{r.url}: {r.error}" for r in results if r.error)
        else:
            rep.status = "NO_DATA"
            rep.reason = ("страница открылась (HTTP {}), но в публичном DOM не найдено ни одного события с "
                          "коэффициентом".format(first.http_status if first else "?"))
            if parse_errors:
                rep.reason += f"; ошибки разбора: {parse_errors[0]}"
        return rep

    async def refresh(self, force: bool = False) -> list[ParsedEvent]:
        async with self._lock:
            ttl = float(self.cfg["min_refresh_seconds"])
            if self.report is not None and self.report.status in ("BLOCKED", "ERROR"):
                ttl = max(ttl, float(self.cfg["blocked_backoff_seconds"]))  # no hammering a refusing site
            fresh = time.monotonic() - self._cache_at < ttl
            if self.report is not None and fresh and not force:
                return self._events
            t0 = time.monotonic()
            try:
                events, rep = await self.collect()
            except Exception as e:  # noqa: BLE001
                events = []
                rep = AdapterReport(bookmaker=self.name.upper(), status="ERROR", timestamp=utcnow(),
                                    reason=f"{type(e).__name__}: {e}"[:300])
            self.status.latency_ms = (time.monotonic() - t0) * 1000
            self._events, self.report, self._cache_at = events, rep, time.monotonic()
            self.metrics_last = event_metrics(events)
            self._unsaved = True
            if rep.status == "OK":
                self.mark_ok(rep.events, rep.markets)
            else:
                self.mark_down(rep.reason)
                if rep.status == "BLOCKED":
                    self.status.state = ProviderState.UNAVAILABLE
                    from app.proxy_manager import shared as proxy_manager

                    proxy_manager().mark_blocked(self.name, rep.reason)  # blocks are not routed around
            return events

    def metrics(self, max_age_s: float = 600) -> dict:
        """Monitoring: events vs unique events vs markets vs odds, success rate, latency, staleness."""
        t = self.metrics_total
        m = dict(self.metrics_last)
        m["success_rate"] = round(t["page_ok"] / t["page_requests"], 3) if t["page_requests"] else None
        m["avg_load_ms"] = round(t["load_ms_sum"] / t["page_requests"]) if t["page_requests"] else None
        m["last_update"] = self.status.last_success
        m["status"] = self.report.status if self.report else "NOT_RUN"
        m["reason"] = (self.report.reason if self.report else "")[:300]
        now = utcnow()
        odds_ts = [e.fetched_at for e in self._events for mk in e.markets for _ in mk.outcomes]
        m["stale_share"] = round(sum((now - ts).total_seconds() > max_age_s for ts in odds_ts) / len(odds_ts), 3)             if odds_ts else None
        return m

    def pop_unsaved(self) -> tuple[list[ParsedEvent], AdapterReport] | None:
        """Events of the latest refresh not yet written to the DB (each refresh is saved once)."""
        if not self._unsaved or self.report is None:
            return None
        self._unsaved = False
        return self._events, self.report

    # ---- BaseProvider interface ----------------------------------------------
    async def health_check(self):
        # a BLOCKED verdict is re-checked only after the back-off (sites change their policy)
        expired = time.monotonic() - self._cache_at >= float(self.cfg["blocked_backoff_seconds"])
        if expired and self.status.state == ProviderState.UNAVAILABLE:
            self.status.state = ProviderState.DOWN
        return self.status

    async def fetch_markets(self) -> list[Market]:
        events = await self.refresh()
        return normalizer.to_domain(events, self.name, include_live=self.include_live_in_scanner)

    async def refresh_market(self, market: Market) -> Market:
        events = await self.refresh(force=True)
        fresh = {m.market_id: m for m in normalizer.to_domain(events, self.name)}
        f = fresh.get(market.market_id)
        if f:
            market.outcomes = f.outcomes
        else:
            for o in market.outcomes:
                o.quotes = []
        return market

    async def close(self) -> None:
        await super().close()  # the shared browser is closed by its owner (scanner / CLI)
