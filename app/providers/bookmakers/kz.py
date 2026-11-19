"""Kazakhstan bookmakers — research results (verified 2026-11-18 from a KZ IP).

None of these operators publishes an official odds API. 1xBet and Ubet are
connected (public line, robots.txt allows it, no anti-bot). Olimpbet, Fonbet,
Parimatch, Winline and Tennisi are read from the public rendered DOM through
Zendriver — see app/providers/bookmakers/<name>/. RESEARCH lists operators
that still have no adapter (currently none).
"""

from __future__ import annotations

from app.domain import Market, PlatformType
from app.providers.base import BaseProvider, ProviderState

RESEARCH: dict[str, dict] = {
    # olimpbet, fonbet, parimatch, winline and tennisi now have real Zendriver/DOM
    # adapters (app/providers/bookmakers/<name>/). Their 2026-11-18 research notes
    # are kept in data/research_kz.json. Add here only operators without an adapter.
}


class KzBookmakerProvider(BaseProvider):
    platform_type = PlatformType.BOOKMAKER

    def __init__(self, key: str, **kw):
        self.name = key
        super().__init__(config={}, **kw)
        info = RESEARCH[key]
        self.docs_url = info["site"]
        self.status.docs = info["site"]
        self.status.state = ProviderState.UNAVAILABLE
        self.status.message = f"{info['api']}. Проверка: {info['probe']}. Путь: {info['legal_path']}"
        self.status.region_note = "Лицензированный оператор РК" if info["kz_license"] else ""

    @property
    def enabled(self) -> bool:
        return False

    async def health_check(self):
        return self.status

    async def fetch_markets(self) -> list[Market]:
        return []

    async def refresh_market(self, market: Market) -> Market:
        return market
