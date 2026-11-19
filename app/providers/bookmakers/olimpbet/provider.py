"""OLIMPBET Kazakhstan — public pages read through Chrome (Zendriver).

robots.txt forbids ``/api/``, ``/ajax/``, ``/ws/``, ``/socket/`` and every URL with ``?`` — none of them
is requested. Allowed and used (``Allow: /line``):
  * the home page ``/`` — the sport tabs of "Топ события" are clicked like a visitor would;
  * ``/line/<sport>-<id>`` — the tournament list of a sport (with the number of events);
  * ``/line/<sport>-<id>/<tournament>`` — a tournament page: 1X2, double chance, handicap, total.
Tournaments are re-discovered every ``discover_minutes``; per sport only the ``tournaments_per_sport``
tournaments with the most events are read, so one pass is at most ~20 page views.
"""

from __future__ import annotations

import time

from app.providers.bookmakers.common.dom_provider import DomBookmakerProvider, PageSpec
from app.providers.bookmakers.olimpbet import parser
from app.providers.bookmakers.olimpbet.models import TOP_TABS

READY = "document.querySelectorAll('[data-testid^=\"main-top-events-event-cell-\"]').length > 3"
LINE_READY = "document.querySelectorAll('[data-testid^=\"line-events-event-cell-\"]').length > 3"
LIST_READY = "document.querySelectorAll('[data-testid^=\"line-tournaments-link-\"]').length > 0"
# sport list pages (sports-menu ids on olimpbet.kz) → our sport slug, and tournaments read per sport
LINE_SPORTS = {"football-100": ("football", 8), "tennis-101": ("tennis", 4), "hockey-103": ("hockey", 3),
               "basketball-102": ("basketball", 3), "volleyball-104": ("volleyball", 2)}
# Measured 19.11.2026: FONBET's football page lists mostly the Russian Premier League, so tournaments
# also offered by the other KZ bookmakers are read first (overlap = where cross-book arbitrage can exist).
PRIORITY_TOURNAMENTS = ("россия. премьер", "казахстан", "лига чемпионов", "лига европы", "лига конференций",
                        "кхл", "единая лига втб", "евролига")


def click_tab(tab_id: str) -> str:
    return f"document.querySelector('[data-testid=\"main-top-events-sports-tab-{tab_id}\"]').click()"


HOME = PageSpec("https://olimpbet.kz/", ready_js=READY, scroll=0, settle=2,
                actions=[(sport, click_tab(tid)) for tid, sport in TOP_TABS.items()])


class OlimpbetProvider(DomBookmakerProvider):
    name = "olimpbet"
    site = "https://olimpbet.kz"
    pages = [HOME]
    parser = staticmethod(parser.parse_any)

    def default_config(self) -> dict:
        return {**super().default_config(), "min_refresh_seconds": 900, "discover_minutes": 120}

    _tours: list[PageSpec] = []
    _tours_at: float = 0.0

    async def discover(self) -> list[PageSpec]:
        specs: list[PageSpec] = []
        for slug, (sport, top) in LINE_SPORTS.items():
            res = await self.browser.fetch(f"{self.site}/line/{slug}", ready_js=LIST_READY, timeout=40, settle=2,
                                           scroll=0)
            if res.error:
                if res.blocked:
                    break  # the site refused us: no further page views in this pass
                continue
            links = [x for x in parser.parse_tournament_links(res.html) if x[2] > 0]
            # tournaments the other KZ bookmakers also list come first (that is where arbitrage can exist),
            # the remaining slots go to the biggest tournaments
            prio = [x for x in links if any(k in x[1].lower() for k in PRIORITY_TOURNAMENTS)]
            rest = sorted((x for x in links if x not in prio), key=lambda x: -x[2])
            chosen = (prio[:top] + rest)[: max(top, len(prio[:top]))]
            specs += [PageSpec(self.site + href, ready_js=LINE_READY, scroll=1, settle=2, label=f"{sport}|{name}")
                      for href, name, _ in chosen]
        return specs

    async def collect(self):
        if not self._tours or time.monotonic() - self._tours_at > 60 * float(self.cfg["discover_minutes"]):
            try:
                found = await self.discover()
                if found:
                    self._tours, self._tours_at = found, time.monotonic()
            except Exception as e:  # noqa: BLE001
                self.log.warning("tournament discovery failed: %s", e)
        self.pages = [HOME, *self._tours]
        return await super().collect()
