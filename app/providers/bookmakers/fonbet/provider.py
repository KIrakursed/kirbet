"""FONBET Kazakhstan — public sport pages read through Chrome (Zendriver).

History: on 18.11.2026 the same pages answered ``403 Forbidden`` ("If you are
not a bot…") to a headless Playwright Chromium. If that happens again the
adapter reports BLOCKED with the HTTP status and does not try to get around it.
"""

from __future__ import annotations

from app.providers.bookmakers.common.dom_provider import DomBookmakerProvider, PageSpec
from app.providers.bookmakers.fonbet import parser
from app.providers.bookmakers.fonbet.models import SPORT_PAGES

READY = "document.querySelectorAll('[class*=\"sport-base-event--\"] [class*=\"value--\"]').length > 5"


class FonbetProvider(DomBookmakerProvider):
    name = "fonbet"
    site = "https://fonbet.kz"
    pages = [PageSpec(f"https://fonbet.kz/sports/{slug}", ready_js=READY, scroll=3, settle=2, label=sport)
             for sport, slug in SPORT_PAGES.items()]
    parser = staticmethod(parser.parse)

    def default_config(self) -> dict:
        # 8 sport pages: refresh every 10 min (one page view ≈ every 75 s) — FONBET answered 403 at 5 min
        return {**super().default_config(), "min_refresh_seconds": 600}
