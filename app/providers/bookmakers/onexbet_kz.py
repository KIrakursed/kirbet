"""1xBet Kazakhstan (1xbet.kz) — public prematch line, read-only.

Research (18.11.2026, see data/research_kz.json):
  * the site's own pages load odds from ``/service-api/main-line-feed/v3/...``
    as plain JSON, without login, cookies, CAPTCHA or anti-bot challenge;
  * robots.txt (User-agent: *) does not disallow ``/service-api/``;
  * terms of use found no clause forbidding reading public odds (they forbid
    nothing on this topic); betting with bots is not done by this project.
We identify ourselves honestly (no browser spoofing) and poll slowly.

Sport ids: 4 tennis, 6 volleyball, 40 esports. ``selectedMs=1.S.L,2.S.L,10.S.L``
selects sport S / league L. The server rejects percent-encoded commas, so the
query string is built manually.

Settlement (rules 9.23 "Теннис"): walkover before start → refund; retirement
after the 1st set is completed → match bets stand (advancing player wins);
retirement during the 1st set → refund.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
from app.providers.base import BaseProvider, ProviderError, ProviderState
from app.providers.bookmakers.common.access_gate import AccessGate

BASE = "https://1xbet.kz/service-api/main-line-feed/v3"
COMMON = "fcountry=82&gr=664&lng=ru&ref=78"
SPORTS = {1: "football", 4: "tennis", 6: "volleyball", 40: "esports"}
ESPORT_CAT = [
    (re.compile(r"counter.?strike|cs ?2|cs:go", re.I), "cs2"),
    (re.compile(r"dota", re.I), "dota2"),
    (re.compile(r"league of legends|\blol\b", re.I), "lol"),
    (re.compile(r"valorant", re.I), "valorant"),
    (re.compile(r"starcraft", re.I), "sc2"),
]
TENNIS_SETTLEMENT = SettlementProfile(
    walkover=VoidRule.REFUND, retirement=RetirementRule.ADVANCING_AFTER_FIRST_SET,
    notes="1xBet п.9.23: отказ после 1-го сета — победа прошедшему; в 1-м сете — возврат")
OTHER_SETTLEMENT = SettlementProfile(walkover=VoidRule.REFUND, retirement=RetirementRule.ADVANCING_PLAYER,
                                     notes="1xBet: отмена/перенос → возврат (коэффициент 1)")


def league_category(sport_id: int, league_name_en: str) -> str | None:
    if sport_id == 1:
        return "football"
    if sport_id == 4:
        return "tennis"
    if sport_id == 6:
        return "volleyball"
    for rx, cat in ESPORT_CAT:
        if rx.search(league_name_en or ""):
            return cat
    return None


def parse_game(g: dict, fetched: datetime, league_id: int) -> Market | None:
    """Convert one games1x2 item into a Market with 1xBet decimal-odds quotes."""
    sport_id = g.get("sport", {}).get("id")
    liga = g.get("liga", {})
    cat = league_category(sport_id, liga.get("nameEng", ""))
    if cat is None:
        return None
    o1, o2 = g.get("opponent1", {}), g.get("opponent2", {})
    if cat == "football":  # KZ bookmakers publish football teams in Russian
        a, b = o1.get("fullName") or o1.get("fullNameEng"), o2.get("fullName") or o2.get("fullNameEng")
    else:
        a = o1.get("fullNameEng") or o1.get("fullName")
        b = o2.get("fullNameEng") or o2.get("fullName")
    if not a or not b:
        return None
    grp = next((eg for eg in g.get("eventGroups", []) if eg.get("groupId") == 1), None)
    if grp is None:
        return None
    by_type = {}
    for col in grp.get("events", []):
        for ev in col:
            if ev.get("cf") and "parameter" not in ev:
                by_type[ev["type"]] = Decimal(str(ev["cf"]))
    if 1 not in by_type or 3 not in by_type:
        return None
    start = datetime.fromtimestamp(int(g["startTs"]), tz=timezone.utc) if g.get("startTs") else None
    url = f"https://1xbet.kz/ru/line/{SPORTS.get(sport_id, 'sport')}/{liga.get('id')}/{g.get('id')}"
    labels = [(1, a), (3, b)]
    draw = 2 in by_type
    if draw:
        labels.insert(1, (2, "Draw"))

    def q(t: int, label: str) -> Quote:
        return Quote(
            platform="1xbet", kind=PriceKind.DECIMAL_ODDS, outcome_label=label,
            instrument_id=f"1xbet:{g['id']}:{t}", best_price=by_type[t], levels=[], depth_known=False,
            fetched_at=fetched, fee_model="none", fee_params={"unit_step": "1"}, currency="KZT",
            side="BACK", url=url,
        )

    outcomes = [Outcome(basic(lbl), lbl, [q(t, lbl)]) for t, lbl in labels]
    return Market(
        platform="1xbet", platform_type=PlatformType.BOOKMAKER, market_id=f"1xbet:{g['id']}",
        event_id=str(g["id"]), title=f"{a} vs {b}", category=cat, market_type=MarketType.MATCH_WINNER,
        outcomes=outcomes, start_time=start, participants=(a, b), tournament=liga.get("nameEng", ""),
        settlement=(TENNIS_SETTLEMENT if cat == "tennis" else
                    SettlementProfile(OTHER_SETTLEMENT.walkover, OTHER_SETTLEMENT.retirement, draw)),
        period="regular_time" if cat == "football" else "match",
        url=url, extra={"league_id": league_id, "sport_id": sport_id, "game_id": g["id"],
                        "names_ru": (o1.get("fullName"), o2.get("fullName"))},
    )


class OneXBetKzProvider(BaseProvider):
    name = "1xbet"
    platform_type = PlatformType.BOOKMAKER
    docs_url = "https://1xbet.kz"
    default_rate = 0.7  # ~1 request / 1.4 s

    def default_config(self) -> dict:
        return {
            "sports": [1, 4, 6, 40],
            "tennis_league_include": ["ATP", "WTA", "Challenger"],
            "league_exclude": ["Doubles", "Winner", "Outright", "Special"],
            "max_leagues_per_sport": 30,
            "max_hours_ahead": 336,
            "min_refresh_seconds": 300,
            "coverage": ["футбол 1X2", "теннис ATP/WTA/Challenger", "волейбол", "CS2", "Dota 2", "LoL", "Valorant"],
            "region_note": "Лицензированный оператор РК; данные — публичная линия сайта (без входа)",
        }

    _cache: list[Market] | None = None
    _cache_at: float = 0.0

    async def _get(self, path: str, query: str):
        return await self.request("GET", f"{BASE}/{path}?{query}")

    _gate: AccessGate | None = None

    async def _page_gate(self) -> bool:
        """Open the public page through Zendriver first; a refused site is not contacted further."""
        self._gate = self._gate or AccessGate("1xbet", "https://1xbet.kz/ru/line/football")
        v = await self._gate.check()
        if not v.ok:
            self.status.state = ProviderState.UNAVAILABLE
            self.status.message = f"публичная страница недоступна — {v.reason}. Не обходим; повтор через 30 мин"
            self.status.last_error = v.reason
        return v.ok

    async def health_check(self):
        if not await self._page_gate():
            return self.status
        try:
            await self._get("topChamps", COMMON)
            if self.status.state not in (ProviderState.OK, ProviderState.DEGRADED):
                self.status.state = ProviderState.OK
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
        return self.status

    def _wanted_league(self, sport_id: int, name: str) -> bool:
        if any(x.lower() in name.lower() for x in self.cfg["league_exclude"]):
            return False
        if sport_id == 4:
            return any(name.startswith(p) for p in self.cfg["tennis_league_include"])
        if sport_id == 40:
            return any(rx.search(name) for rx, _ in ESPORT_CAT)
        return True

    async def leagues(self, sport_id: int) -> list[tuple[int, str, int]]:
        sm = f"1.{sport_id},2.{sport_id},10.{sport_id}"
        data = await self._get("leftMenuSports", f"{COMMON}&selectedMs={sm}")
        out: dict[int, tuple[int, str, int]] = {}
        for sec in data or []:
            for s in sec.get("sports", []):
                if s.get("id") != sport_id:
                    continue
                for lg in s.get("ligas", []) or []:
                    for sub in lg.get("subLigas") or [lg]:
                        if sub.get("id") and self._wanted_league(sport_id, sub.get("nameEng", "")):
                            out[sub["id"]] = (sub["id"], sub.get("nameEng", ""), sub.get("gamesCount", 0))
        return sorted(out.values(), key=lambda x: -x[2])[: int(self.cfg["max_leagues_per_sport"])]

    async def league_games(self, sport_id: int, league_id: int) -> list[dict]:
        sm = f"1.{sport_id}.{league_id},2.{sport_id}.{league_id},10.{sport_id}.{league_id}"
        # parameter order matters for this endpoint (same order as the site uses)
        q = f"cfView=3&count=40&countryFirst=true&fcountry=82&gr=664&grMode=4&lng=ru&ref=78&selectedMs={sm}"
        data = await self._get("games1x2", q)
        if not isinstance(data, list):
            raise ProviderError(f"неожиданный ответ: {str(data)[:120]}")
        return data

    async def fetch_markets(self) -> list[Market]:
        import time as _t

        if self._cache is not None and _t.monotonic() - self._cache_at < float(self.cfg["min_refresh_seconds"]):
            return self._cache
        markets: list[Market] = []
        n_events = 0
        errors = 0
        horizon = utcnow() + timedelta(hours=float(self.cfg["max_hours_ahead"]))
        for sport_id in self.cfg["sports"]:
            try:
                lgs = await self.leagues(sport_id)
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.log.warning("leagues %s failed: %s", sport_id, e)
                continue
            for lid, _name, _cnt in lgs:
                try:
                    games = await self.league_games(sport_id, lid)
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    self.log.warning("league %s failed: %s", lid, e)
                    continue
                fetched = utcnow()
                for g in games:
                    n_events += 1
                    m = parse_game(g, fetched, lid)
                    if m and (m.start_time is None or m.start_time <= horizon):
                        markets.append(m)
        if errors and not markets:
            self.mark_down(self.status.last_error or "все запросы завершились ошибкой")
            return []
        self.mark_ok(n_events, len(markets))
        if errors:
            self.status.state = ProviderState.DEGRADED
            self.status.message = f"{errors} запросов с ошибкой"
        self._cache, self._cache_at = markets, _t.monotonic()
        return markets

    async def refresh_market(self, market: Market) -> Market:
        games = await self.league_games(market.extra["sport_id"], market.extra["league_id"])
        fetched = utcnow()
        for g in games:
            if g.get("id") == market.extra["game_id"]:
                fresh = parse_game(g, fetched, market.extra["league_id"])
                if fresh:
                    market.outcomes = fresh.outcomes
                    return market
        for o in market.outcomes:  # event removed from the line
            o.quotes = []
        return market
