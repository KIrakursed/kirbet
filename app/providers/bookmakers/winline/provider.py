"""Winline Kazakhstan — public home page read through Chrome (Zendriver).

robots.txt (checked on every run): ``Disallow: /?`` and ``/*?`` — so only the
parameter-free home page is opened; the internal ``/sb/api/*`` and WebSocket
feeds are NOT called. Odds are read from the rendered DOM a visitor sees.
"""

from __future__ import annotations

from app.providers.bookmakers.common.dom_provider import DomBookmakerProvider, PageSpec
from app.providers.bookmakers.winline import parser

READY = "document.querySelectorAll('[data-t=\"eventContainer\"] [data-t=\"odds\"]').length > 5"


class WinlineProvider(DomBookmakerProvider):
    name = "winline"
    site = "https://winline.kz"
    # "Линия" (/sport/all, every sport, prematch + live, 1X2/winner + handicap + total) first, then the
    # home page for its "Главные события". Neither URL has a query string (robots.txt forbids those).
    pages = [PageSpec("https://winline.kz/sport/all", ready_js=READY, scroll=6, settle=3),
             PageSpec("https://winline.kz/", ready_js=READY, scroll=2, settle=3)]
    parser = staticmethod(parser.parse)
