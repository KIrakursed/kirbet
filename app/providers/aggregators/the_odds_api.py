"""The Odds API aggregator (https://the-odds-api.com/liveapi/guides/v4/).

Requires ODDS_API_KEY (free plan: 500 credits / month; each /odds call costs
markets x regions credits). Covers bookmakers such as 1xBet (international
site, key ``onexbet``), Pinnacle, Marathonbet and exchanges Betfair/Matchbook
(back + lay via the ``h2h_lay`` market). No esports / volleyball coverage.
Important: odds of 1xBet *international* may differ from 1xbet.kz.
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta
from decimal import Decimal

from app.config import ROOT, get_settings
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
from app.timeutil import now_almaty

API = "https://api.the-odds-api.com/v4"
EXCHANGES = {"betfair_ex_eu", "betfair_ex_uk", "betfair_ex_au", "matchbook"}

# Typical bookmaker tennis rules: if a player retires the bet is void (refund).
# This is *different* from Polymarket/Kalshi and will be flagged by the matcher.
BOOKMAKER_TENNIS = SettlementProfile(walkover=VoidRule.REFUND, retirement=RetirementRule.VOID,
                                     notes="Большинство БК: retirement/walkover → возврат ставки")
EXCHANGE_TENNIS = SettlementProfile(walkover=VoidRule.REFUND, retirement=RetirementRule.ADVANCING_PLAYER,
                                    notes="Betfair Match Odds: при retirement выигрывает прошедший дальше")


class TheOddsApiProvider(BaseProvider):
    name = "the_odds_api"
    platform_type = PlatformType.AGGREGATOR
    docs_url = "https://the-odds-api.com/liveapi/guides/v4/"
    requires_key = True
    default_rate = 2.0

    def default_config(self) -> dict:
        return {
            "regions": "eu",
            "markets": "h2h",
            "min_refresh_minutes": 120,
            "credit_reserve": 50,
            "low_credit_warning": 100,
            "health_cache_minutes": 30,
            "sport_prefixes": ["tennis_"],
            "bookmakers": [],  # empty = all in region
            "exchange_commission": "0.05",
            "max_sports_per_scan": 4,
            "coverage": ["tennis (турниры ATP/WTA)", "1xBet(int)", "Pinnacle", "Betfair Exchange", "Matchbook"],
        }

    _cached: list[Market] | None = None
    _cached_at: float = 0.0
    _ev_cache: dict | None = None
    _counted_headers = None
    _warned_low = False
    _health_at: float = 0.0

    # ---- credit guard -------------------------------------------------------------
    # Free plan: 500 credits / month. Spending is spread over the rest of the (Almaty) month:
    # daily cap = (remaining - reserve) / days left; nothing is requested while a fresh cache exists.
    LEDGER = ROOT / "data" / "odds_api_credits.json"

    def _ledger(self) -> dict:
        try:
            d = json.loads(self.LEDGER.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        today = now_almaty().date().isoformat()
        if d.get("date") != today:
            d = {"date": today, "used_today": 0, "requests_today": 0, "remaining": d.get("remaining"),
                 "month_used": d.get("month_used") if (d.get("date") or "")[:7] == today[:7] else 0}
        return d

    def _save_ledger(self, d: dict) -> None:
        try:
            self.LEDGER.parent.mkdir(parents=True, exist_ok=True)
            self.LEDGER.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _track_credits(self) -> int | None:
        h = self.last_headers or {}
        rem = h.get("x-requests-remaining")
        if rem is None:
            return None
        try:
            n = int(float(rem))
            last = int(float(h.get("x-requests-last", 0) or 0))
        except ValueError:
            return None
        d = self._ledger()
        if h is not self._counted_headers:  # count each response once
            self._counted_headers = h
            d["used_today"] += last
            d["month_used"] = (d.get("month_used") or 0) + last
            d["requests_today"] += 1
        d["remaining"] = n
        self._save_ledger(d)
        cap = self.daily_cap(n)
        warn = "⚠️ мало кредитов! " if n <= int(self.cfg["low_credit_warning"]) else ""
        self.status.message = (f"{warn}кредитов осталось: {n}; сегодня потрачено {d['used_today']} из {cap} "
                               f"(запросов {d['requests_today']})")
        if warn and not self._warned_low:
            self._warned_low = True
            self.log.warning("The Odds API: осталось %d кредитов", n)
        return n

    def daily_cap(self, remaining: int | None) -> int:
        if remaining is None:
            return int(self.cfg["credit_reserve"])
        today = now_almaty().date()
        days_left = (calendar.monthrange(today.year, today.month)[1] - today.day) + 1
        return max(1, (remaining - int(self.cfg["credit_reserve"])) // days_left)

    def credits(self) -> dict:
        d = self._ledger()
        return {**d, "daily_cap": self.daily_cap(d.get("remaining"))}

    def _budget_ok(self) -> bool:
        d = self._ledger()
        rem = d.get("remaining")
        if rem is not None and rem <= int(self.cfg["credit_reserve"]):
            return False
        return d["used_today"] < self.daily_cap(rem)

    @property
    def key(self) -> str:
        return get_settings().odds_api_key

    async def health_check(self):
        if not self.key:
            self.status.state = ProviderState.NEEDS_KEY
            self.status.message = "нужен ODDS_API_KEY в .env (бесплатный план на the-odds-api.com)"
            return self.status
        import time as _t

        if self._health_at and _t.monotonic() - self._health_at < 60 * float(self.cfg["health_cache_minutes"])                 and self.status.state == ProviderState.OK:
            return self.status  # no request per scan just to prove the key still works
        try:
            await self.request("GET", f"{API}/sports", params={"apiKey": self.key}, retries=1, cache_key="sports")
            self._health_at = _t.monotonic()
            self._track_credits()
            if self.status.state != ProviderState.OK:
                self.status.state = ProviderState.OK
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
        return self.status

    def _parse_event(self, ev: dict, fetched: datetime) -> list[Market]:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        home, away = ev.get("home_team"), ev.get("away_team")
        out = []
        tennis = ev["sport_key"].startswith("tennis")
        per_book: dict[str, Market] = {}
        for bk in ev.get("bookmakers", []):
            is_ex = bk["key"] in EXCHANGES
            settle = (EXCHANGE_TENNIS if is_ex else BOOKMAKER_TENNIS) if tennis else SettlementProfile()
            for mk in bk.get("markets", []):
                if mk["key"] not in ("h2h", "h2h_lay"):
                    continue
                lay = mk["key"] == "h2h_lay"
                plat = bk["key"]
                market = per_book.get(plat)
                if market is None:
                    market = Market(
                        platform=plat, platform_type=PlatformType.EXCHANGE if is_ex else PlatformType.BOOKMAKER,
                        market_id=f"{plat}:{ev['id']}", event_id=ev["id"], title=f"{home} vs {away}",
                        category="tennis" if tennis else ev["sport_key"].split("_")[0],
                        market_type=MarketType.MATCH_WINNER, outcomes=[], start_time=start,
                        participants=(home, away), tournament=ev.get("sport_title", ""),
                        settlement=settle, extra={"source": "the_odds_api", "bookmaker_title": bk.get("title"),
                               "sport_key": ev["sport_key"]},
                    )
                    per_book[plat] = market
                upd = mk.get("last_update") or bk.get("last_update")
                ts = datetime.fromisoformat(upd.replace("Z", "+00:00")) if upd else fetched
                for o in mk.get("outcomes", []):
                    price = Decimal(str(o["price"]))
                    if price <= 1:
                        continue
                    if is_ex:
                        kind = PriceKind.EXCHANGE_LAY if lay else PriceKind.EXCHANGE_BACK
                        params = {"commission": self.cfg["exchange_commission"], "unit_step": "0.01"}
                    else:
                        kind = PriceKind.DECIMAL_ODDS
                        params = {"unit_step": "0.01"}
                    q = Quote(
                        platform=plat, kind=kind, outcome_label=o["name"], instrument_id=f"{plat}:{ev['id']}:{o['name']}:{mk['key']}",
                        best_price=price, levels=[], depth_known=False, fetched_at=ts, fee_model="none",
                        fee_params=params, currency="USD", side="LAY" if lay else "BACK",
                    )
                    key = basic(o["name"])
                    oc = next((x for x in market.outcomes if x.key == key), None)
                    if oc is None:
                        oc = Outcome(key=key, label=o["name"])
                        market.outcomes.append(oc)
                    oc.quotes.append(q)
        for m in per_book.values():
            if len(m.outcomes) == 2:
                out.append(m)
        return out

    async def fetch_markets(self) -> list[Market]:
        if not self.key:
            self.status.state = ProviderState.NEEDS_KEY
            return []
        import time as _t

        age_min = (_t.monotonic() - self._cached_at) / 60
        if self._cached is not None and age_min < float(self.cfg["min_refresh_minutes"]):
            return self._cached  # credit saving: odds are re-fetched for candidates during validation
        if not self._budget_ok():
            self.status.state = ProviderState.DEGRADED
            self.status.message += " — резерв, загрузка приостановлена"
            return self._cached or []
        try:
            sports = await self.request("GET", f"{API}/sports", params={"apiKey": self.key}, cache_key="sports")
            keys = [s["key"] for s in sports if s.get("active")
                    and any(s["key"].startswith(p) for p in self.cfg["sport_prefixes"])]
            keys = keys[: int(self.cfg["max_sports_per_scan"])]
            markets: list[Market] = []
            n = 0
            horizon = utcnow() + timedelta(hours=72)
            for i, k in enumerate(keys):
                if i and not self._budget_ok():  # daily cap reached mid-refresh: keep what we have
                    break
                params = {"apiKey": self.key, "regions": self.cfg["regions"], "markets": self.cfg["markets"],
                          "oddsFormat": "decimal"}
                if self.cfg["bookmakers"]:
                    params["bookmakers"] = ",".join(self.cfg["bookmakers"])
                data = await self.request("GET", f"{API}/sports/{k}/odds", params=params)
                self._track_credits()  # count every paid response
                fetched = utcnow()
                for ev in data:
                    n += 1
                    for m in self._parse_event(ev, fetched):
                        if m.start_time and m.start_time <= horizon:
                            markets.append(m)
            self.mark_ok(n, len(markets))
            self._track_credits()
            self._cached, self._cached_at = markets, _t.monotonic()
            return markets
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
            return []

    async def refresh_market(self, market: Market) -> Market:
        # one cheap event-level call: /events/{id}/odds
        import time as _t

        sport = market.extra.get("sport_key")
        if not sport:
            return market
        self._ev_cache = self._ev_cache or {}
        hit = self._ev_cache.get(market.event_id)
        if hit and _t.monotonic() - hit[0] < 30:
            ev = hit[1]  # one credit per event, shared by all bookmakers of that event
        else:
            if not self._budget_ok():
                raise RuntimeError("исчерпан бюджет кредитов The Odds API")
            params = {"apiKey": self.key, "regions": self.cfg["regions"], "markets": self.cfg["markets"],
                      "oddsFormat": "decimal"}
            ev = await self.request("GET", f"{API}/sports/{sport}/events/{market.event_id}/odds", params=params)
            self._track_credits()
            self._ev_cache[market.event_id] = (_t.monotonic(), ev)
        fresh = [m for m in self._parse_event(ev, utcnow()) if m.platform == market.platform]
        if fresh:
            market.outcomes = fresh[0].outcomes
        return market
