"""Winline DOM parser: home page ("Главные события" + "Live сейчас") and the "Линия" page /sport/all.

Event container: ``[data-t=eventContainer]`` with ``id`` = event id and
``data-sport-id``; competitors ``[data-t=competitorHome|competitorAway]``;
start time ``[data-t=scheduled]`` (absent → live); outcome buttons
``[data-t=outcome][data-market-id][data-outcome-id]`` with ``[data-t=odds]``.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup
from pydantic import ValidationError

from app.providers.bookmakers.common.textutil import clean, parse_odds, parse_ru_datetime, sport_slug
from app.providers.bookmakers.winline.models import MARKET_IDS, SPORT_IDS, ParsedEvent, ParsedMarket, ParsedOutcome

BASE = "https://winline.kz"


def outcome_names(n: int) -> list[str]:
    return ["П1", "Х", "П2"] if n == 3 else ["П1", "П2"]


def other_names(n: int) -> list[str]:
    return [str(i + 1) for i in range(n)]


_SIGNS = re.compile(r"^\s*([–—+-])?\s*([\d.]+)\s*([–—+-])?\s*$")


def _odds_of(buttons) -> list:
    out = []
    for b in buttons:
        o = b.select_one('[data-t="odds"]')
        out.append(parse_odds(o.get_text() if o else ""))
    return out


def parse_markets(ev) -> list[ParsedMarket]:
    """One ParsedMarket per market block. Totals/handicaps keep their line and their Winline market id:
    ids whose meaning is not confirmed (e.g. tennis 187/189 vs 203/204 — games or sets?) are labelled
    with the id, so they can never be matched to a different market of another bookmaker."""
    out: list[ParsedMarket] = []
    for block in ev.select('[data-t="simple-market"], [data-t="handicap-market"], [data-t="total-market"]'):
        buttons = block.select('[data-t="outcome"]')
        if not buttons:
            continue
        mid = buttons[0].get("data-market-id") or "?"
        odds = _odds_of(buttons)
        if any(o is None for o in odds):
            continue  # a locked outcome: the market is incomplete
        kind_attr = block.get("data-t")
        try:
            if kind_attr == "simple-market":
                name, kind = MARKET_IDS.get(mid, (f"Рынок #{mid}", "other"))
                if (kind == "1x2" and len(odds) != 3) or (kind == "winner" and len(odds) != 2):
                    continue
                names = outcome_names(len(odds)) if kind != "other" else other_names(len(odds))
                out.append(ParsedMarket(name=name, kind=kind,
                                        outcomes=[ParsedOutcome(name=n, odds=o) for n, o in zip(names, odds)]))
                continue
            spec = block.select_one('[data-t="current-specifier"]')
            m = _SIGNS.match(clean(spec.get_text()) if spec else "")
            if not m or len(odds) != 2:
                continue
            left, line, right = m.groups()
            if kind_attr == "total-market":  # column header "Тотал М - Б": under first, over second
                out.append(ParsedMarket(name=f"Тотал {line} (#{mid})", kind="total" if mid == "18" else "other",
                                        outcomes=[ParsedOutcome(name="Меньше", odds=odds[0], param=line),
                                                  ParsedOutcome(name="Больше", odds=odds[1], param=line)]))
            else:  # "– 2.5 +": home gets the left sign, away the right one
                h = ("-" if left in ("–", "—", "-") else "+") + line if left else line
                a = ("-" if right in ("–", "—", "-") else "+") + line if right else line
                out.append(ParsedMarket(name=f"Фора (#{mid})", kind="handicap" if mid == "16" else "other",
                                        outcomes=[ParsedOutcome(name=f"Ф1 ({h})", odds=odds[0], param=h),
                                                  ParsedOutcome(name=f"Ф2 ({a})", odds=odds[1], param=a)]))
        except ValidationError:
            continue
    return out


def parse(html: str, label: str, fetched_at: datetime) -> list[ParsedEvent]:
    soup = BeautifulSoup(html, "lxml")
    out: list[ParsedEvent] = []
    for ev in soup.select('[data-t="eventContainer"]'):
        eid = ev.get("id") or ""
        home = ev.select_one('[data-t="competitorHome"]')
        away = ev.select_one('[data-t="competitorAway"]')
        if not (eid and home and away):
            continue
        sid = ev.get("data-sport-id") or ev.get("data-t-sport") or ""  # home page / "Линия" page
        sport = SPORT_IDS.get(sid)
        icon = ev.select_one('[data-t="icon-sport"]')
        icon_name = ""
        if icon is not None:
            m = re.search(r"icons/([\w-]+)\.svg", icon.get("style", ""))
            icon_name = m.group(1) if m else ""
        if sport in (None, "esports"):
            sport = sport_slug(icon_name.replace("filled-", "").replace("outline-", "")) if icon_name else (sport or f"sport_{sid}")
            if sport in ("cs",):
                sport = "cs2"
        sched = ev.select_one('[data-t="scheduled"]')
        start_text = clean(sched.get_text()) if sched else ""
        link = ev.select_one("a.competitor-names, a[href*='/match/']")
        markets = parse_markets(ev)
        if not markets:
            continue
        try:
            out.append(ParsedEvent(
                bookmaker="winline", external_id=eid, sport=sport, home=clean(home.get_text()),
                away=clean(away.get_text()), start_text=start_text,
                start_time=parse_ru_datetime(start_text, fetched_at) if start_text else None,
                is_live=sched is None, url=BASE + link["href"] if link and link.get("href") else BASE,
                markets=markets, fetched_at=fetched_at,
            ))
        except ValidationError:
            continue
    return out
