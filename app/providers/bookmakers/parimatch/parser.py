"""Parimatch DOM parser (sport pages, "Главное" event cards).

Card = ``a[href^="/ru/events/<slug>-<id>"]`` containing
``[data-testid=time-status]``, ``[data-testid=competitor-1|2]`` and
``[data-testid=outcomes-section-vertical]`` whose slides each hold one market:
buttons ``[data-testid=outcome]`` → ``[data-testid=odd]`` + ``[data-testid=outcome-name]``;
a line value (totals) sits in ``[data-testid=market-with-value]``.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from app.providers.bookmakers.common.textutil import clean, parse_odds, parse_ru_datetime
from app.providers.bookmakers.parimatch.models import ParsedEvent, ParsedMarket, ParsedOutcome

BASE = "https://parimatch.kz"
EVENT_HREF = re.compile(r"^/ru/events/[\w-]*?-(\d+)$")
LIVE = re.compile(r"\d+['’]|тайм|период|сет|перерыв|live", re.I)


def _market_from_buttons(group: Tag) -> ParsedMarket | None:
    outs: list[ParsedOutcome] = []
    for b in group.select('[data-testid="outcome"]'):
        o = b.select_one('[data-testid="odd"]')
        n = b.select_one('[data-testid="outcome-name"]')
        odds = parse_odds(o.get_text() if o else "")
        if odds is None or n is None:
            continue
        try:
            outs.append(ParsedOutcome(name=clean(n.get_text()), odds=odds))
        except ValidationError:
            continue
    names = [o.name for o in outs]
    try:
        if names == ["П1", "Х", "П2"]:
            return ParsedMarket(name="1X2", kind="1x2", outcomes=outs)
        if names == ["П1", "П2"]:
            return ParsedMarket(name="Победитель", kind="winner", outcomes=outs)
        if len(outs) == 2 and names[0].startswith("Больше") and names[1].startswith("Меньше"):
            mv = group.select_one('[data-testid="market-with-value"] [data-testid="odd"]')
            line = clean(mv.get_text()) if mv else None
            outs = [o.model_copy(update={"param": line}) for o in outs]
            return ParsedMarket(name=f"Тотал {line}" if line else "Тотал", kind="total", outcomes=outs)
        if len(outs) == 2 and names[0].startswith("Ф1") and names[1].startswith("Ф2"):
            return ParsedMarket(name="Фора", kind="handicap", outcomes=outs)
        if len(outs) >= 2:
            return ParsedMarket(name=" / ".join(names), kind="other", outcomes=outs)
    except ValidationError:
        return None
    return None


def parse(html: str, label: str, fetched_at: datetime) -> list[ParsedEvent]:
    """``label`` is the sport of the page (football / tennis / ...)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[ParsedEvent] = []
    for card in soup.find_all("a", href=EVENT_HREF):
        c1 = card.select_one('[data-testid="competitor-1"]')
        c2 = card.select_one('[data-testid="competitor-2"]')
        section = card.select_one('[data-testid="outcomes-section-vertical"]')
        if not (c1 and c2 and section):
            continue
        eid = EVENT_HREF.match(card["href"]).group(1)
        ts = card.select_one('[data-testid="time-status"]')
        start_text = clean(ts.get_text()) if ts else ""
        start = parse_ru_datetime(start_text, fetched_at) if start_text else None
        is_live = start is None or bool(card.select_one('[data-testid*="score"] [data-testid*="period"]')) \
            or (bool(LIVE.search(start_text)) and not re.search(r"\d{1,2}:\d{2}", start_text))
        # each market is the closest common container of a group of outcome buttons
        groups: list[Tag] = []
        for b in section.select('[data-testid="outcome"]'):
            g = b.parent
            if g not in groups:
                groups.append(g)
        markets = [m for m in (_market_from_buttons(g) for g in groups) if m]
        if not markets:
            continue
        try:
            out.append(ParsedEvent(
                bookmaker="parimatch", external_id=eid, sport=label if label != "main" else "unknown",
                home=clean(c1.get_text()), away=clean(c2.get_text()), start_text=start_text, start_time=start,
                is_live=is_live, url=BASE + card["href"], markets=markets, fetched_at=fetched_at,
            ))
        except ValidationError:
            continue
    return out
