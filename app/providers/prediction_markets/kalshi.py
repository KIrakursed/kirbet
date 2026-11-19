"""Kalshi adapter — official public Trade API v2 (market data needs no key).

Docs: https://docs.kalshi.com/  Base: https://api.elections.kalshi.com/trade-api/v2
Order book returns only BIDS for YES and NO.  Ask prices are derived:
    YES ask at (1 - q)  ⇐  NO bid at q        NO ask at (1 - q)  ⇐  YES bid at q
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

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
from app.normalization.names import basic
from app.providers.base import BaseProvider, ProviderState
from app.timeutil import aware_or_none

API = "https://api.elections.kalshi.com/trade-api/v2"

SERIES = {
    # ticker: (category, market_type, period)
    "KXATPMATCH": ("tennis", MarketType.MATCH_WINNER, "match"),
    "KXWTAMATCH": ("tennis", MarketType.MATCH_WINNER, "match"),
    "KXATPCHALLENGERMATCH": ("tennis", MarketType.MATCH_WINNER, "match"),
    "KXCHALLENGERMATCH": ("tennis", MarketType.MATCH_WINNER, "match"),
    "KXWTACHALLENGERMATCH": ("tennis", MarketType.MATCH_WINNER, "match"),
    "KXCS2GAME": ("cs2", MarketType.MATCH_WINNER, "match"),
    "KXDOTA2GAME": ("dota2", MarketType.MATCH_WINNER, "match"),
    "KXLOLGAME": ("lol", MarketType.MATCH_WINNER, "match"),
    "KXVALORANTGAME": ("valorant", MarketType.MATCH_WINNER, "match"),
    "KXVOLLEYBALLMATCH": ("volleyball", MarketType.MATCH_WINNER, "match"),
    "KXNCAAWVMATCH": ("volleyball", MarketType.MATCH_WINNER, "match"),
}

# time-zone words in Kalshi rules → IANA zone (zoneinfo applies DST); unknown words are not guessed
_TZ = {"ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
       "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
       "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
       "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
       "UTC": "UTC", "GMT": "UTC"}
_SCHED_RE = re.compile(
    r"scheduled for ([A-Z][a-z]{2,8}) (\d{1,2}), (\d{4}) at (\d{1,2}):(\d{2}) ?([AP]M) ?([A-Z]{2,3})"
)


def parse_scheduled(rules: str) -> datetime | None:
    m = _SCHED_RE.search(rules or "")
    if not m:
        return None
    mon, day, year, hh, mm, ampm, tz = m.groups()
    try:
        base = datetime.strptime(f"{mon[:3]} {day} {year}", "%b %d %Y")
    except ValueError:
        return None
    h = int(hh) % 12 + (12 if ampm == "PM" else 0)
    zone = _TZ.get(tz)
    if zone is None:
        return None  # timezone_unknown: do not guess
    local = (base + timedelta(hours=h, minutes=int(mm))).replace(tzinfo=ZoneInfo(zone))
    return local.astimezone(timezone.utc)


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return aware_or_none(datetime.fromisoformat(s.replace("Z", "+00:00")))  # naive → unknown, not guessed
    except ValueError:
        return None


def _d(v, default="0") -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else default))
    except Exception:  # noqa: BLE001
        return Decimal(default)


def kalshi_settlement(category: str, rules: str) -> SettlementProfile:
    r = (rules or "").lower()
    walk = VoidRule.FAIR_PRICE if "fair" in r else VoidRule.UNKNOWN
    ret = RetirementRule.UNKNOWN
    if category == "tennis" and "after a ball has been played" in r:
        ret = RetirementRule.ADVANCING_PLAYER
    elif category != "tennis":
        ret = RetirementRule.ADVANCING_PLAYER
    return SettlementProfile(walkover=walk, retirement=ret,
                             notes="Kalshi: отмена до начала → fair price")


class KalshiProvider(BaseProvider):
    name = "kalshi"
    platform_type = PlatformType.PREDICTION_MARKET
    docs_url = "https://docs.kalshi.com/"
    default_rate = 8.0

    def default_config(self) -> dict:
        return {
            "series": list(SERIES),
            "max_hours_ahead": 72,
            "coverage": ["tennis ATP/WTA/Challenger", "CS2", "Dota 2", "LoL", "Valorant", "volleyball"],
            "region_note": "Kalshi — регулируемая CFTC биржа (США). Доступность для резидентов KZ "
                           "определяется KYC Kalshi — проверьте сами перед использованием.",
        }

    async def health_check(self):
        try:
            st = await self.request("GET", f"{API}/exchange/status", retries=1)
            if not st.get("trading_active", True):
                self.status.state = ProviderState.DEGRADED
                self.status.message = "биржа сейчас не торгует"
            elif self.status.state != ProviderState.OK:
                self.status.state = ProviderState.OK
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
        return self.status

    async def _series_fees(self, ticker: str) -> dict:
        key = f"series:{ticker}"
        hit = self.cache.get(key)
        if hit:
            return hit
        try:
            s = (await self.request("GET", f"{API}/series/{ticker}"))["series"]
            fee = {"fee_type": s.get("fee_type"), "multiplier": str(s.get("fee_multiplier", 1))}
        except Exception:  # noqa: BLE001
            fee = {"fee_type": "quadratic", "multiplier": "1"}
        self.cache.ttl = max(self.cache.ttl, 30)
        self.cache.set(key, fee)
        return fee

    def _top_quotes(self, m: dict, fee: dict, url: str, fetched: datetime) -> tuple[Quote | None, Quote | None]:
        params = {"rate": "0.07", "multiplier": fee["multiplier"], "unit_step": "1"}
        res = []
        for side, price_k, size_k, bid_k in (
            ("YES", "yes_ask_dollars", "yes_ask_size_fp", "yes_bid_dollars"),
            ("NO", "no_ask_dollars", "yes_bid_size_fp", "no_bid_dollars"),
        ):
            p = _d(m.get(price_k))
            size = _d(m.get(size_k))
            if not (Decimal("0") < p < Decimal("1")) or size <= 0:
                res.append(None)
                continue
            res.append(Quote(
                platform=self.name, kind=PriceKind.BINARY_CONTRACT,
                outcome_label=(m.get("yes_sub_title") if side == "YES" else m.get("no_sub_title")) or side,
                instrument_id=f"{m['ticker']}:{side}", best_price=p, levels=[Level(p, size)],
                fetched_at=fetched, min_size=Decimal("1"), fee_model="kalshi", fee_params=params,
                currency="USD", side=side, url=url, bid_price=_d(m.get(bid_k)) or None,
            ))
        return res[0], res[1]

    def _book_quotes(self, ticker: str, ob: dict, fee: dict, url: str, fetched: datetime,
                     labels: tuple[str, str]) -> tuple[Quote | None, Quote | None]:
        book = ob.get("orderbook_fp") or ob.get("orderbook") or {}
        yes_bids = [(_d(p), _d(s)) for p, s in (book.get("yes_dollars") or [])]
        no_bids = [(_d(p), _d(s)) for p, s in (book.get("no_dollars") or [])]
        params = {"rate": "0.07", "multiplier": fee["multiplier"], "unit_step": "1"}
        out = []
        for side, opp_bids, own_bids, label in (("YES", no_bids, yes_bids, labels[0]),
                                                ("NO", yes_bids, no_bids, labels[1])):
            asks = sorted((Level(Decimal("1") - p, s) for p, s in opp_bids if s > 0 and 0 < p < 1),
                          key=lambda lv: lv.price)
            if not asks:
                out.append(None)
                continue
            out.append(Quote(
                platform=self.name, kind=PriceKind.BINARY_CONTRACT, outcome_label=label,
                instrument_id=f"{ticker}:{side}", best_price=asks[0].price, levels=asks[:40],
                fetched_at=fetched, min_size=Decimal("1"), fee_model="kalshi", fee_params=params,
                currency="USD", side=side, url=url,
                bid_price=max((p for p, _ in own_bids), default=None),
            ))
        return out[0], out[1]

    def _build(self, ev: dict, series: str, fee: dict, fetched: datetime) -> Market | None:
        cat, mtype, period = SERIES[series]
        mkts = [m for m in ev.get("markets", []) if m.get("status") == "active"]
        if len(mkts) != 2:
            return None  # only clean head-to-head events (2 complementary markets)
        rules = mkts[0].get("rules_primary", "") + "\n" + mkts[0].get("rules_secondary", "")
        start = parse_scheduled(rules) or _dt(mkts[0].get("occurrence_datetime")) \
            or _dt(mkts[0].get("expected_expiration_time"))
        now = utcnow()
        if start and start > now + timedelta(hours=float(self.cfg["max_hours_ahead"])):
            return None
        if start and start < now - timedelta(hours=30):
            return None
        names = [m.get("yes_sub_title") or m.get("title") for m in mkts]
        url = f"https://kalshi.com/markets/{series.lower()}/{ev['event_ticker'].lower()}"
        outcomes = []
        for i, m in enumerate(mkts):
            other = mkts[1 - i]
            y, _ = self._top_quotes(m, fee, url, fetched)
            _, n_other = self._top_quotes(other, fee, url, fetched)
            if y:
                y.outcome_label = names[i]
            if n_other:
                n_other.outcome_label = f"{names[i]} (NO на «{names[1 - i]}»)"
            quotes = [q for q in (y, n_other) if q]
            outcomes.append(Outcome(key=basic(names[i]), label=names[i], quotes=quotes))
        return Market(
            platform=self.name, platform_type=self.platform_type, market_id=ev["event_ticker"],
            event_id=ev["event_ticker"], title=ev.get("title", ""), category=cat, market_type=mtype,
            period=period, outcomes=outcomes, start_time=start, participants=tuple(names),
            tournament=ev.get("sub_title", ""), exhaustive=True,
            settlement=kalshi_settlement(cat, rules), url=url, rules=rules[:1500],
            extra={"tickers": [m["ticker"] for m in mkts], "series": series, "fee": fee,
                   "can_close_early": mkts[0].get("can_close_early")},
        )

    async def fetch_markets(self) -> list[Market]:
        markets: list[Market] = []
        n_events = 0
        errors = 0
        for series in self.cfg["series"]:
            if series not in SERIES:
                continue
            try:
                fee = await self._series_fees(series)
                cursor = None
                while True:
                    params = {"series_ticker": series, "status": "open", "with_nested_markets": "true",
                              "limit": 200}
                    if cursor:
                        params["cursor"] = cursor
                    data = await self.request("GET", f"{API}/events", params=params)
                    fetched = utcnow()
                    for ev in data.get("events", []):
                        n_events += 1
                        mk = self._build(ev, series, fee, fetched)
                        if mk:
                            markets.append(mk)
                    cursor = data.get("cursor")
                    if not cursor:
                        break
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.log.warning("series %s failed: %s", series, e)
        if errors and not markets:
            self.mark_down(self.status.last_error or "все запросы завершились ошибкой")
        else:
            self.mark_ok(n_events, len(markets))
            if errors:
                self.status.state = ProviderState.DEGRADED
                self.status.message = f"{errors} серий не загрузились"
        return markets

    async def refresh_market(self, market: Market) -> Market:
        tickers = market.extra["tickers"]
        fee = market.extra["fee"]
        books = {}
        for t in tickers:
            books[t] = await self.request("GET", f"{API}/markets/{t}/orderbook")
        fetched = utcnow()
        names = [o.label for o in market.outcomes]
        for i, oc in enumerate(market.outcomes):
            y, _ = self._book_quotes(tickers[i], books[tickers[i]], fee, market.url, fetched, (names[i], names[i]))
            _, n = self._book_quotes(tickers[1 - i], books[tickers[1 - i]], fee, market.url, fetched,
                                     (names[1 - i], f"{names[i]} (NO на «{names[1 - i]}»)"))
            oc.quotes = [q for q in (y, n) if q]
        return market
