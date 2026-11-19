"""TENNISI Kazakhstan — checked through a normal page view (Zendriver).

Expected result today: BLOCKED — the home page redirects to a URL with a
query string, and robots.txt (``User-agent: * / Disallow: *?*``) forbids all
such URLs. The content is then discarded by the browser client and the
precise rule is reported. Legal path: an official B2B feed from the operator.
"""

from __future__ import annotations

from app.providers.bookmakers.common.dom_provider import DomBookmakerProvider, PageSpec
from app.providers.bookmakers.tennisi import parser


class TennisiProvider(DomBookmakerProvider):
    name = "tennisi"
    site = "https://tennisi.kz"
    pages = [PageSpec("https://tennisi.kz/", ready_js=None, scroll=0, settle=2)]
    parser = staticmethod(parser.parse)
