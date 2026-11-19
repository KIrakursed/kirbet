"""TENNISI DOM parser.

The site is a CGI application: ``/`` redirects to
``/mtg2/cgi/free.Welcome?lang=rus`` — a ``<frameset>`` whose frames
(``!free.WorkFrames?…``, ``!free.TopToolbar?…``) and line data
(``!free.AjaxAPI?cmd=…``) all live on URLs with a query string, which
robots.txt forbids for every agent (``Disallow: *?*``). So the adapter never
receives a usable line page; this parser recognises that situation and only
extracts odds if a parameter-free page ever renders a plain HTML line table.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup
from pydantic import ValidationError

from app.providers.bookmakers.common.textutil import clean, parse_odds, parse_ru_datetime, sport_slug
from app.providers.bookmakers.tennisi.models import ParsedEvent, ParsedMarket, ParsedOutcome

VS = re.compile(r"^(.+?)\s+(?:-|—|–|vs\.?)\s+(.+)$")


def is_frameset(html: str) -> bool:
    return bool(re.search(r"<frameset\b", html or "", re.I))


def parse(html: str, label: str, fetched_at: datetime) -> list[ParsedEvent]:
    if not html or is_frameset(html):
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[ParsedEvent] = []
    sport = "unknown"
    for tr in soup.find_all("tr"):
        cells = [clean(td.get_text()) for td in tr.find_all(["td", "th"], recursive=False)]
        if len(cells) == 1 and cells[0]:
            s = sport_slug(cells[0])
            sport = s if s in ("football", "tennis", "hockey", "basketball", "volleyball") else sport
            continue
        name = next((c for c in cells if VS.match(c)), None)
        odds = [parse_odds(c) for c in cells]
        odds = [o for o in odds if o]
        if not name or len(odds) < 2 or sport == "unknown":
            continue
        home, away = VS.match(name).groups()
        when = next((c for c in cells if re.search(r"\d{1,2}:\d{2}", c)), "")
        names = ["П1", "Х", "П2"] if len(odds) >= 3 else ["П1", "П2"]
        try:
            out.append(ParsedEvent(
                bookmaker="tennisi", external_id=f"{home}|{away}|{when}", sport=sport, home=home, away=away,
                start_text=when, start_time=parse_ru_datetime(when, fetched_at) if when else None,
                markets=[ParsedMarket(name="1X2" if len(names) == 3 else "Победитель",
                                      kind="1x2" if len(names) == 3 else "winner",
                                      outcomes=[ParsedOutcome(name=n, odds=o) for n, o in zip(names, odds)])],
                fetched_at=fetched_at, url="https://tennisi.kz/",
            ))
        except ValidationError:
            continue
    return out
