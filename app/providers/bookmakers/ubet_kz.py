"""Ubet (ubet.kz) — public prematch "top" line, read via a normal page view.

Research (18.11.2026):
  * ubet.kz's API host l.ubet.kz answers 404 to non-browser clients, so we do
    NOT call it directly and do NOT spoof browser headers;
  * instead a real headless Chrome (Zendriver, shared session) opens the public
    home page exactly like a visitor, and we read the JSON the page itself
    receives (``/matches/top``) from the browser's network log (CDP);
  * no login, no CAPTCHA; robots.txt of ubet.kz does not disallow the home page;
  * Ubet rules forbid *placing bets* with bots and treat arbitrage betting as
    bad faith (bets may be voided) — this is shown to the user as a risk.
Coverage of the home page feed is football 1X2 (team names in Russian only).
Refresh: at most one page view per ``min_refresh_seconds``.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain import (
    Market,
    MarketType,
    Outcome,
    PlatformType,
    PriceKind,
    Quote,
    RetirementRule,
    SettlementProfile,
    VoidRule,
    utcnow,
)
from app.normalization.names import basic
from app.providers.base import BaseProvider, ProviderState
from app.providers.browser.zendriver_client import ZendriverClient

FEED_TZ: str | None = "UTC"  # verified, see parse_match
HOME = "https://ubet.kz/"
FEED = "l.ubet.kz/matches/top"
SPORT = {1: "football", 3: "tennis", 12: "volleyball", 53: "esports", 4: "basketball"}
SETTLE = SettlementProfile(walkover=VoidRule.REFUND, retirement=RetirementRule.UNKNOWN, draw_possible=True,
                           notes="Ubet: 1X2 — основное время; отмена → возврат")


def parse_match(m: dict, fetched: datetime) -> Market | None:
    if m.get("is_live"):
        return None
    sport = SPORT.get(m.get("sport_id"))
    if sport != "football":
        return None
    teams = m.get("teams") or []
    if len(teams) != 2:
        return None
    a, b = teams[0]["name"], teams[1]["name"]
    main = {s.get("name"): s for s in (m.get("stakes") or {}).get("1", []) if s.get("is_active")}
    if not all(k in main for k in ("П1", "X", "П2")):
        return None
    start = None
    if m.get("started_at"):
        # The feed's ``started_at`` carries no zone. Established on 19.11.2026 by cross-checking three
        # matches against FONBET (e.g. Оренбург — Балтика 10:00 here = "21 ноября в 15:00" Asia/Almaty there):
        # the feed is UTC. FEED_TZ records that finding; set it to None and times become timezone_unknown.
        start = datetime.fromisoformat(m["started_at"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo(FEED_TZ)) if FEED_TZ else None
        start = start.astimezone(timezone.utc) if start else None
    url = "https://ubet.kz" + (m.get("url") or "")
    outs = []
    for key, label in (("П1", a), ("X", "Draw"), ("П2", b)):
        s = main[key]
        q = Quote(platform="ubet", kind=PriceKind.DECIMAL_ODDS, outcome_label=label,
                  instrument_id=f"ubet:{s['id']}", best_price=Decimal(str(s["factor"])), levels=[],
                  depth_known=False, fetched_at=fetched, fee_params={"unit_step": "1"}, currency="KZT",
                  side="BACK", url=url)
        outs.append(Outcome(basic(label), label, [q]))
    return Market(
        platform="ubet", platform_type=PlatformType.BOOKMAKER, market_id=f"ubet:{m['id']}", event_id=str(m["id"]),
        title=f"{a} — {b}", category="football", market_type=MarketType.MATCH_WINNER, outcomes=outs,
        start_time=start, participants=(a, b), tournament=m.get("tournament", ""), settlement=SETTLE,
        period="regular_time", url=url, extra={"match_id": m["id"], "lang": "ru"},
    )


class UbetKzProvider(BaseProvider):
    name = "ubet"
    platform_type = PlatformType.BOOKMAKER
    docs_url = "https://ubet.kz"
    default_rate = 0.2

    def default_config(self) -> dict:
        return {"min_refresh_seconds": 300, "max_hours_ahead": 336,
                "coverage": ["футбол 1X2 (топ-линия главной страницы)"],
                "region_note": "Лицензированный оператор РК; данные — публичная главная страница (браузер)"}

    _cache: list[Market] | None = None
    _cache_at: float = 0.0
    _lock: asyncio.Lock | None = None

    def __init__(self, browser: ZendriverClient | None = None, **kw):
        super().__init__(**kw)
        self.browser = browser or ZendriverClient.shared()

    async def _page_view(self) -> list[dict]:
        """One normal page view; returns the payload of the feed the page itself loaded."""
        self._lock = self._lock or asyncio.Lock()
        async with self._lock:
            res = None
            for _attempt in range(2):  # one retry: the browser client restarts Chrome if it died
                res = await self.browser.fetch(HOME, capture=[FEED], timeout=45,
                                               settle=0.5, scroll=0)
                self.status.requests += 1
                self.status.latency_ms = res.load_ms
                if res.blocked:  # robots / 403 / challenge — reported, never bypassed
                    break
                if FEED in res.captured:
                    break
                self.status.errors += 1
            if res is None or FEED not in res.captured:
                err = (res.error if res else "") or "страница не загрузила линию (возможна блокировка/изменение сайта)"
                self.status.last_error = err[:300]
                raise RuntimeError(f"HTTP {res.http_status if res else '?'}: {err}")
            try:
                data = json.loads(res.captured[FEED][-1])
            except ValueError as e:
                raise RuntimeError(f"линия не JSON: {e}") from e
            return (data or {}).get("payload") or []

    async def health_check(self):
        if self._cache_at and time.monotonic() - self._cache_at < 900 and self.status.state == ProviderState.OK:
            return self.status
        try:
            await self.browser.start()
            if self.status.state not in (ProviderState.OK, ProviderState.DEGRADED):
                self.status.state = ProviderState.OK
        except Exception as e:  # noqa: BLE001
            self.mark_down(f"Chrome (Zendriver) недоступен: {e}")
        return self.status

    def _parse(self, payload: list[dict]) -> list[Market]:
        fetched = utcnow()
        horizon = fetched + timedelta(hours=float(self.cfg["max_hours_ahead"]))
        out = []
        for m in payload:
            mk = parse_match(m, fetched)
            if mk and (mk.start_time is None or mk.start_time <= horizon):
                out.append(mk)
        return out

    async def fetch_markets(self) -> list[Market]:
        if self._cache is not None and time.monotonic() - self._cache_at < float(self.cfg["min_refresh_seconds"]):
            return self._cache
        try:
            payload = await self._page_view()
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
            return []
        markets = self._parse(payload)
        self.mark_ok(len(payload), len(markets))
        self._cache, self._cache_at = markets, time.monotonic()
        self._unsaved = True
        return markets

    _unsaved = False

    def pop_unsaved(self):
        """Latest page view as ParsedEvents for the DB (odds_snapshots), once per real refresh."""
        if not self._unsaved or self._cache is None:
            return None
        self._unsaved = False
        from app.providers.bookmakers.common.models import AdapterReport, ParsedEvent, ParsedMarket, ParsedOutcome

        events = []
        names = {0: "П1", 1: "Х", 2: "П2"}
        for m in self._cache:
            q0 = m.outcomes[0].quotes[0]
            outs = [ParsedOutcome(name=names[i], odds=o.quotes[0].best_price) for i, o in enumerate(m.outcomes)]
            events.append(ParsedEvent(
                bookmaker="ubet", external_id=m.event_id, sport=m.category, tournament=m.tournament,
                home=m.participants[0], away=m.participants[1], start_time=m.start_time, url=m.url,
                markets=[ParsedMarket(name="1X2", kind="1x2", outcomes=outs)], fetched_at=q0.fetched_at))
        rep = AdapterReport(bookmaker="UBET", status="OK", events=len(events), markets=len(events),
                            timestamp=events[0].fetched_at if events else utcnow(), load_ms=self.status.latency_ms)
        return events, rep

    async def refresh_market(self, market: Market) -> Market:
        payload = await self._page_view()
        fresh = {m.market_id: m for m in self._parse(payload)}
        self._cache, self._cache_at = list(fresh.values()), time.monotonic()
        f = fresh.get(market.market_id)
        if f:
            market.outcomes = f.outcomes
        else:
            for o in market.outcomes:
                o.quotes = []
        return market

    async def close(self) -> None:
        await super().close()  # the shared Chrome is closed by its owner (scanner / CLI)
