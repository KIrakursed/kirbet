"""Betfair Exchange API-NG adapter (read-only).

Docs: https://docs.developer.betfair.com/
Requires BETFAIR_APP_KEY and BETFAIR_SESSION_TOKEN (obtained by the user via
their own Betfair account — this project never logs in for you).
Betfair does not serve customers in many jurisdictions; from the test
environment (KZ IP) api.betfair.com did not respond. Status is reported
honestly as DOWN/NEEDS_KEY until the user supplies working credentials.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import get_settings
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

API = "https://api.betfair.com/exchange/betting/rest/v1.0"
TENNIS_EVENT_TYPE = "2"


class BetfairProvider(BaseProvider):
    name = "betfair"
    platform_type = PlatformType.EXCHANGE
    docs_url = "https://docs.developer.betfair.com/"
    requires_key = True
    default_rate = 4.0

    def default_config(self) -> dict:
        return {"commission": "0.05", "max_markets": 100,
                "coverage": ["tennis Match Odds (back/lay)"],
                "region_note": "Betfair недоступен во многих странах; проверьте условия для KZ"}

    def _headers(self) -> dict:
        s = get_settings()
        return {"X-Application": s.betfair_app_key, "X-Authentication": s.betfair_session_token,
                "Content-Type": "application/json"}

    @property
    def has_keys(self) -> bool:
        s = get_settings()
        return bool(s.betfair_app_key and s.betfair_session_token)

    async def health_check(self):
        if not self.has_keys:
            self.status.state = ProviderState.NEEDS_KEY
            self.status.message = "нужны BETFAIR_APP_KEY и BETFAIR_SESSION_TOKEN"
            return self.status
        try:
            await self.request("POST", f"{API}/listEventTypes/", json={"filter": {}}, headers=self._headers(), retries=1)
            self.status.state = ProviderState.OK
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
        return self.status

    async def fetch_markets(self) -> list[Market]:
        if not self.has_keys:
            self.status.state = ProviderState.NEEDS_KEY
            return []
        try:
            cat = await self.request("POST", f"{API}/listMarketCatalogue/", headers=self._headers(), json={
                "filter": {"eventTypeIds": [TENNIS_EVENT_TYPE], "marketTypeCodes": ["MATCH_ODDS"]},
                "maxResults": int(self.cfg["max_markets"]),
                "marketProjection": ["RUNNER_DESCRIPTION", "EVENT", "MARKET_START_TIME"],
            })
            markets = []
            for i in range(0, len(cat), 40):
                chunk = cat[i:i + 40]
                books = await self.request("POST", f"{API}/listMarketBook/", headers=self._headers(), json={
                    "marketIds": [c["marketId"] for c in chunk],
                    "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
                })
                byid = {b["marketId"]: b for b in books}
                for c in chunk:
                    m = self._build(c, byid.get(c["marketId"]))
                    if m:
                        markets.append(m)
            self.mark_ok(len(cat), len(markets))
            return markets
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
            return []

    def _build(self, cat: dict, book: dict | None) -> Market | None:
        if not book or book.get("status") != "OPEN" or len(cat.get("runners", [])) != 2:
            return None
        from datetime import datetime
        fetched = utcnow()
        names = {r["selectionId"]: r["runnerName"] for r in cat["runners"]}
        outcomes = []
        params = {"commission": self.cfg["commission"], "unit_step": "0.01"}
        for r in book.get("runners", []):
            ex = r.get("ex", {})
            name = names.get(r["selectionId"], str(r["selectionId"]))
            quotes = []
            back = [Level(Decimal(str(x["price"])), Decimal(str(x["size"]))) for x in ex.get("availableToBack", [])]
            lay = [Level(Decimal(str(x["price"])), Decimal(str(x["size"]))) for x in ex.get("availableToLay", [])]
            # we BACK at the best availableToBack price (highest first)
            if back:
                quotes.append(Quote(self.name, PriceKind.EXCHANGE_BACK, name, f"{cat['marketId']}:{r['selectionId']}:B",
                                    back[0].price, back, fetched, Decimal("1"), fee_model="none",
                                    fee_params=params, currency="GBP", side="BACK"))
            # we LAY at availableToLay prices (lowest first)
            if lay:
                quotes.append(Quote(self.name, PriceKind.EXCHANGE_LAY, name, f"{cat['marketId']}:{r['selectionId']}:L",
                                    lay[0].price, lay, fetched, Decimal("1"), fee_model="none",
                                    fee_params=params, currency="GBP", side="LAY"))
            outcomes.append(Outcome(basic(name), name, quotes))
        start = datetime.fromisoformat(cat["marketStartTime"].replace("Z", "+00:00"))
        return Market(
            platform=self.name, platform_type=self.platform_type, market_id=cat["marketId"],
            event_id=cat.get("event", {}).get("id", ""), title=cat.get("event", {}).get("name", ""),
            category="tennis", market_type=MarketType.MATCH_WINNER, outcomes=outcomes, start_time=start,
            participants=tuple(names.values()),
            settlement=SettlementProfile(walkover=VoidRule.REFUND, retirement=RetirementRule.ADVANCING_PLAYER),
            url=f"https://www.betfair.com/exchange/plus/tennis/market/{cat['marketId']}",
        )

    async def refresh_market(self, market: Market) -> Market:
        return market
