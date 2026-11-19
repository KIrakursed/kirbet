"""Polymarket adapter — official public APIs, no key required for market data.

Gamma API  https://gamma-api.polymarket.com   events / markets / metadata
CLOB API   https://clob.polymarket.com        order books (POST /books batch)
Geoblock   https://polymarket.com/api/geoblock  is trading allowed from this IP
Docs       https://docs.polymarket.com/
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain import (
    Level,
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
from app.normalization.names import basic, parse_best_of
from app.providers.base import BaseProvider, ProviderState
from app.timeutil import aware_or_none

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GEOBLOCK = "https://polymarket.com/api/geoblock"

SPORT_TAGS = {"tennis": 864, "esports": 64, "volleyball": 102883}
TAG_TO_CATEGORY = {
    "tennis": "tennis", "counter-strike-2": "cs2", "dota-2": "dota2",
    "league-of-legends": "lol", "valorant": "valorant", "volleyball": "volleyball",
    "starcraft-2": "sc2", "starcraft-ii": "sc2",
}
NONSPORT_CATEGORY_TAGS = {
    "politics": "politics", "elections": "politics", "crypto": "crypto",
    "economy": "economics", "economics": "economics", "finance": "finance",
    "tech": "tech", "science": "science", "pop-culture": "culture", "culture": "culture",
    "geopolitics": "geopolitics", "weather": "weather", "mentions": "mentions",
}


def _j(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return v or []


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace(" ", "T", 1)
    if s.endswith("+00"):
        s += ":00"
    s = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return aware_or_none(d)  # a value without a time zone is not guessed (timezone_unknown)


def parse_settlement(description: str, draw_possible: bool = False) -> SettlementProfile:
    d = (description or "").lower()
    walk = VoidRule.FIFTY_FIFTY if "50-50" in d else VoidRule.UNKNOWN
    ret = RetirementRule.UNKNOWN
    if re.search(r"retire\w*.{0,120}resolve to the (player|team) who (advances|wins)", d, re.S) or (
        "not completed" in d and ("who advances" in d or "team who wins" in d)
    ):
        ret = RetirementRule.ADVANCING_PLAYER
    return SettlementProfile(walkover=walk, retirement=ret, draw_possible=draw_possible,
                             notes="Polymarket: отмена/ничья → 50-50")


def event_category(event: dict) -> str:
    slugs = [t.get("slug", "") for t in event.get("tags") or []]
    for s in slugs:
        if s in TAG_TO_CATEGORY:
            return TAG_TO_CATEGORY[s]
    for s in slugs:
        if s in NONSPORT_CATEGORY_TAGS:
            return NONSPORT_CATEGORY_TAGS[s]
    if "sports" in slugs:
        return event.get("seriesSlug") or "sports"
    return slugs[0] if slugs else "other"


def _market_kind(m: dict) -> tuple[MarketType, str] | None:
    smt = m.get("sportsMarketType")
    q = m.get("question", "") + " " + (m.get("groupItemTitle") or "")
    if smt == "moneyline":
        return MarketType.MATCH_WINNER, "match"
    if smt == "child_moneyline":
        mm = re.search(r"(map|game)\s*(\d)", q, re.I)
        return (MarketType.MAP_WINNER, f"map{mm.group(2)}") if mm else None
    if smt == "tennis_first_set_winner":
        return MarketType.SET_WINNER, "set1"
    if smt in ("tennis_set_winner", "volleyball_set_winner"):
        mm = re.search(r"set\s*(\d)", q, re.I)
        return (MarketType.SET_WINNER, f"set{mm.group(1)}") if mm else None
    return None


class PolymarketProvider(BaseProvider):
    name = "polymarket"
    platform_type = PlatformType.PREDICTION_MARKET
    docs_url = "https://docs.polymarket.com/"
    default_rate = 8.0

    def default_config(self) -> dict:
        return {
            "sports": ["tennis", "esports", "volleyball"],
            "include_nonsport": True,
            "nonsport_events": 60,
            "max_hours_ahead": 72,
            "include_live": True,
            "periods": ["match", "set1", "set2", "map1", "map2", "map3"],
            "coverage": ["tennis ATP/WTA/Challenger", "CS2", "Dota 2", "LoL", "Valorant",
                         "volleyball", "politics", "crypto", "economics", "tech", "culture"],
        }

    # ----------------------------------------------------------------
    async def health_check(self):
        try:
            geo = await self.request("GET", GEOBLOCK, retries=1)
            await self.request("GET", f"{CLOB}/time", retries=1)
            self.status.region_note = (
                f"Страна по IP: {geo.get('country')}; "
                + ("ТОРГОВЛЯ ЗАБЛОКИРОВАНА для этого региона" if geo.get("blocked") else "торговля не заблокирована")
            )
            if geo.get("blocked"):
                self.status.state = ProviderState.DEGRADED
                self.status.message = "геоблок: данные доступны, торговля из вашего региона запрещена"
            elif self.status.state != ProviderState.OK:
                self.status.state = ProviderState.OK
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
        return self.status

    async def _events(self, **params) -> list[dict]:
        out: list[dict] = []
        offset = 0
        limit = 100
        while True:
            p = {"active": "true", "closed": "false", "limit": limit, "offset": offset, **params}
            page = await self.request("GET", f"{GAMMA}/events", params=p)
            out.extend(page)
            if len(page) < limit or offset >= 900:
                break
            offset += limit
        return out

    async def books(self, token_ids: list[str]) -> dict[str, dict]:
        res: dict[str, dict] = {}
        for i in range(0, len(token_ids), 100):
            chunk = token_ids[i:i + 100]
            data = await self.request("POST", f"{CLOB}/books", json=[{"token_id": t} for t in chunk])
            for b in data or []:
                res[b["asset_id"]] = b
        return res

    # ----------------------------------------------------------------
    def _quote(self, token: str, label: str, side: str, book: dict | None, m: dict, url: str,
               fetched: datetime) -> Quote | None:
        if book is None:
            return None
        asks = sorted(
            (Level(Decimal(a["price"]), Decimal(a["size"])) for a in book.get("asks", [])),
            key=lambda lv: lv.price,
        )
        asks = [a for a in asks if Decimal("0") < a.price < Decimal("1") and a.size > 0]
        bids = [Decimal(b["price"]) for b in book.get("bids", [])]
        if not asks:
            return None
        fs = m.get("feeSchedule") or {}
        fee_params = {
            "enabled": bool(m.get("feesEnabled")),
            "rate": fs.get("rate", "0"),
            "exponent": fs.get("exponent", 1),
            "unit_step": "0.01",
        }
        return Quote(
            platform=self.name, kind=PriceKind.BINARY_CONTRACT, outcome_label=label,
            instrument_id=token, best_price=asks[0].price, levels=asks[:40],
            fetched_at=fetched, min_size=Decimal(str(book.get("min_order_size") or m.get("orderMinSize") or 5)),
            fee_model="polymarket", fee_params=fee_params, currency="USD", side=side,
            url=url, bid_price=max(bids) if bids else None,
        )

    def _build_sport_markets(self, events: list[dict]) -> list[tuple[Market, list[tuple[str, str, str]], dict]]:
        now = utcnow()
        horizon = now + timedelta(hours=float(self.cfg["max_hours_ahead"]))
        out = []
        for e in events:
            cat = event_category(e)
            title = e.get("title", "")
            bo = parse_best_of(title)
            for m in e.get("markets", []):
                if m.get("closed") or not m.get("acceptingOrders") or not m.get("enableOrderBook"):
                    continue
                kind = _market_kind(m)
                if not kind or kind[1] not in self.cfg["periods"]:
                    continue
                outcomes = _j(m.get("outcomes"))
                tokens = _j(m.get("clobTokenIds"))
                if len(outcomes) < 2 or len(outcomes) != len(tokens):
                    continue
                start = _dt(m.get("gameStartTime")) or _dt(e.get("startTime"))
                if start is None or start > horizon:
                    continue
                if start < now - timedelta(hours=6) or (start < now and not self.cfg["include_live"]):
                    continue
                draw = any(basic(o) in ("draw", "tie") for o in outcomes) or bo == 2
                players = tuple(o for o in outcomes if basic(o) not in ("draw", "tie"))
                url = f"https://polymarket.com/event/{e.get('slug', '')}"
                mk = Market(
                    platform=self.name, platform_type=self.platform_type,
                    market_id=str(m["id"]), event_id=str(e["id"]), title=m.get("question") or title,
                    category=cat, market_type=kind[0], period=kind[1],
                    outcomes=[Outcome(key=basic(o), label=o) for o in outcomes],
                    start_time=start, participants=players, tournament=title.split(":")[0],
                    exhaustive=True, settlement=parse_settlement(m.get("description", ""), draw),
                    best_of=bo, url=url, rules=m.get("description", "")[:1500],
                    extra={"neg_risk": m.get("negRisk"), "restricted": m.get("restricted")},
                )
                toks = [(tokens[i], outcomes[i], "BUY") for i in range(len(outcomes))]
                out.append((mk, toks, m))
        return out

    def _build_nonsport(self, events: list[dict]) -> list[tuple[Market, list[tuple[str, str, str]], dict]]:
        out = []
        for e in events:
            mkts = e.get("markets", [])
            cat = event_category(e)
            slugs = {t.get("slug") for t in e.get("tags") or []}
            if cat in TAG_TO_CATEGORY.values() or "sports" in slugs or "games" in slugs:
                continue  # sports are handled by the structured sports path
            if e.get("negRisk") and len(mkts) >= 2:
                # all YES tokens of a neg-risk event = mutually exclusive set
                if any(m.get("closed") and _j(m.get("outcomePrices"))[:1] == ["1"] for m in mkts):
                    continue  # already resolved
                live = [m for m in mkts if not m.get("closed") and m.get("acceptingOrders")]
                if len(live) < 2:
                    continue
                exhaustive = not e.get("negRiskAugmented")
                outcomes, toks = [], []
                for m in live:
                    tk = _j(m.get("clobTokenIds"))
                    label = m.get("groupItemTitle") or m.get("question")
                    if len(tk) != 2:
                        break
                    outcomes.append(Outcome(key=basic(label) or str(m["id"]), label=label))
                    toks.append((tk[0], label, "YES"))
                else:
                    mk = Market(
                        platform=self.name, platform_type=self.platform_type,
                        market_id=f"ev{e['id']}", event_id=str(e["id"]), title=e.get("title", ""),
                        category=cat, market_type=MarketType.MULTI_OUTCOME, outcomes=outcomes,
                        start_time=_dt(e.get("endDate")), exhaustive=exhaustive,
                        settlement=SettlementProfile(walkover=VoidRule.FIFTY_FIFTY,
                                                     retirement=RetirementRule.ADVANCING_PLAYER),
                        url=f"https://polymarket.com/event/{e.get('slug', '')}",
                        extra={"neg_risk": True, "augmented": bool(e.get("negRiskAugmented")),
                               "closed_outcomes": len(mkts) - len(live)},
                    )
                    out.append((mk, toks, live[0]))
            # every binary market: YES + NO complete set
            for m in mkts:
                if m.get("closed") or not m.get("acceptingOrders"):
                    continue
                outs, tk = _j(m.get("outcomes")), _j(m.get("clobTokenIds"))
                if len(outs) != 2 or len(tk) != 2:
                    continue
                mk = Market(
                    platform=self.name, platform_type=self.platform_type,
                    market_id=str(m["id"]), event_id=str(e["id"]), title=m.get("question", ""),
                    category=cat, market_type=MarketType.BINARY_EVENT,
                    outcomes=[Outcome(key=basic(o), label=o) for o in outs],
                    start_time=_dt(m.get("endDate")), exhaustive=True,
                    settlement=SettlementProfile(walkover=VoidRule.FIFTY_FIFTY,
                                                 retirement=RetirementRule.ADVANCING_PLAYER),
                    url=f"https://polymarket.com/event/{e.get('slug', '')}",
                    rules=(m.get("description") or "")[:1500],
                )
                out.append((mk, [(tk[0], outs[0], "BUY"), (tk[1], outs[1], "BUY")], m))
        return out

    async def _attach_books(self, built) -> list[Market]:
        tokens = sorted({t for _, toks, _ in built for t, _, _ in toks})
        books = await self.books(tokens)
        fetched = utcnow()
        markets = []
        for mk, toks, m in built:
            for (tok, label, side), oc in zip(toks, mk.outcomes):
                q = self._quote(tok, label, side, books.get(tok), m, mk.url, fetched)
                oc.quotes = [q] if q else []
            mk.extra["tokens"] = [t for t, _, _ in toks]
            mk.extra["raw_fee"] = {"feesEnabled": m.get("feesEnabled"), "feeSchedule": m.get("feeSchedule")}
            markets.append(mk)
        return markets

    async def fetch_markets(self) -> list[Market]:
        try:
            events: list[dict] = []
            seen = set()
            for sport in self.cfg["sports"]:
                tag = SPORT_TAGS.get(sport)
                if tag is None:
                    continue
                for ev in await self._events(tag_id=tag):
                    if ev["id"] not in seen:
                        seen.add(ev["id"])
                        events.append(ev)
            built = self._build_sport_markets(events)
            if self.cfg.get("include_nonsport"):
                ns = await self.request(
                    "GET", f"{GAMMA}/events",
                    params={"active": "true", "closed": "false", "limit": min(100, int(self.cfg["nonsport_events"])),
                            "order": "volume24hr", "ascending": "false"},
                )
                built += self._build_nonsport(ns)
                events += ns
            markets = await self._attach_books(built)
            self.mark_ok(len(events), len(markets))
            return markets
        except Exception as e:  # noqa: BLE001
            self.log.exception("fetch failed")
            self.mark_down(e)
            return []

    async def refresh_market(self, market: Market) -> Market:
        toks = market.extra.get("tokens", [])
        books = await self.books(toks)
        fetched = utcnow()
        raw = {**market.extra.get("raw_fee", {}), "orderMinSize": 5}
        for tok, oc in zip(toks, market.outcomes):
            old = oc.quotes[0] if oc.quotes else None
            q = self._quote(tok, oc.label, old.side if old else "BUY", books.get(tok), raw, market.url, fetched)
            oc.quotes = [q] if q else []
        return market
