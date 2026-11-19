"""Parimatch Kazakhstan — public sport pages read through Chrome (Zendriver).

robots.txt forbids ``*/direct-feed/api/eventcontent/`` (the internal feed) —
it is NOT called. The pages themselves are allowed (``Allow: /ru/football/``),
so we read only what the rendered page shows to any visitor.
"""

from __future__ import annotations

from app.providers.bookmakers.common.dom_provider import DomBookmakerProvider, PageSpec
from app.providers.bookmakers.parimatch import parser
from app.providers.bookmakers.parimatch.models import SPORT_PAGES

READY = "document.querySelectorAll('a[href^=\"/ru/events/\"] [data-testid=\"odd\"]').length > 3"


class ParimatchProvider(DomBookmakerProvider):
    name = "parimatch"
    site = "https://parimatch.kz"
    pages = [PageSpec(url, ready_js=READY, scroll=4, settle=3, label=sport) for sport, url in SPORT_PAGES.items()]
    parser = staticmethod(parser.parse)
