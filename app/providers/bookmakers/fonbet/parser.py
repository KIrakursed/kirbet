"""FONBET DOM parser (sport pages ``/sports/<sport>``).

The line is a virtual table: competition headers (``sport-competition--*``)
carry column labels "1 Х 2 ФОРА 1 ФОРА 2 Тотал Б М"; each event row
(``sport-base-event--*``) has a link ``/sports/<sport>/<compId>/<eventId>``,
the name "Home — Away", the planned time and one factor cell per column.
Class names have hashed suffixes, so they are matched by prefix.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from app.providers.bookmakers.common.textutil import clean, parse_odds, parse_ru_datetime, sport_slug
from app.providers.bookmakers.fonbet.models import (
    COLUMN_GROUPS,
    SECOND_TIME,
    ParsedEvent,
    ParsedMarket,
    ParsedOutcome,
)

BASE = "https://fonbet.kz"
HREF = re.compile(r"^/sports/([\w-]+)/(\d+)/(\d+)")


def has_cls(tag: Tag, prefix: str) -> bool:
    return any(c.startswith(prefix) for c in (tag.get("class") or []))


def _match(prefix: str):
    def f(c):
        if not c:
            return False
        return any(x.startswith(prefix) for x in (c.split() if isinstance(c, str) else c))
    return f


def _cell_value(cell: Tag) -> tuple[str | None, str]:
    """→ (param, value_text) for single / complex / param cells."""
    param = cell.find(class_=_match("param--"))
    val = cell.find(class_=_match("value--"))
    return (clean(param.get_text()) if param else None), (clean(val.get_text()) if val else "")


def columns_to_groups(columns: list[str]) -> list[tuple[str | None, str | None]]:
    """Header labels → (group, outcome) per column; a repeated "1"/"2" is the handicap column."""
    seen: set[str] = set()
    out = []
    for col in columns:
        if col in seen and col in SECOND_TIME:
            out.append(SECOND_TIME[col])
        else:
            out.append(COLUMN_GROUPS.get(col, (None, None)))
        seen.add(col)
    return out


def parse(html: str, label: str, fetched_at: datetime) -> list[ParsedEvent]:
    soup = BeautifulSoup(html, "lxml")
    out: list[ParsedEvent] = []
    columns: list[str] = []
    tournament = ""
    nodes = soup.find_all("div", class_=lambda c: _match("sport-competition--")(c) or _match("sport-base-event--")(c))
    for node in nodes:
        if has_cls(node, "sport-competition--"):
            facs = [clean(t.get_text()) for t in node.find_all("div", class_=_match("sport-competition__factor--"))]
            texts = [clean(t.get_text()) for t in node.find_all("div", class_=_match("table-component-text--"))]
            tournament = next((t for t in texts if t and t not in facs), "")
            columns = [f.lower() for f in facs]
            continue
        a = node.find("a", href=HREF)
        if a is None or not columns:
            continue
        m = HREF.match(a["href"])
        sport, eid = sport_slug(m.group(1)), m.group(3)
        if label not in ("main", "") and sport_slug(label) != sport:
            continue  # the page fell back to another sport's list
        if sport == "esports":  # CS2 / Dota 2 / LoL / Valorant / SC2 from the tournament name
            sub = sport_slug(tournament)
            sport = sub if sub in ("cs2", "dota2", "lol", "valorant", "sc2") else "esports"
        name = clean(a.get_text())
        if " — " not in name:
            continue
        home, away = (s.strip() for s in name.split(" — ", 1))
        t = node.find(class_=_match("event-block-planned-time__time--"))
        start_text = clean(t.get_text()) if t else ""
        cells = node.find_all("div", class_=_match("factor-value--"))
        groups: dict[str, list[ParsedOutcome]] = {}
        total_line = None
        for (key, oname), cell in zip(columns_to_groups(columns), cells):
            if key is None:
                continue
            param, val = _cell_value(cell)
            if key == "total_line":
                total_line = param or val or None
                continue
            odds = parse_odds(val)
            if odds is None:
                continue  # empty / locked cell
            try:
                groups.setdefault(key, []).append(
                    ParsedOutcome(name=f"{oname} ({param})" if param else oname, odds=odds, param=param))
            except ValidationError:
                continue
        markets: list[ParsedMarket] = []
        main = groups.get("main", [])
        if len(main) == 3:
            markets.append(ParsedMarket(name="1X2", kind="1x2", outcomes=main))
        elif len(main) == 2 and [o.name for o in main] == ["П1", "П2"]:
            markets.append(ParsedMarket(name="Победитель", kind="winner", outcomes=main))
        if len(groups.get("handicap", [])) == 2:
            markets.append(ParsedMarket(name="Фора", kind="handicap", outcomes=groups["handicap"]))
        if len(groups.get("total", [])) == 2 and total_line:
            tot = [o.model_copy(update={"param": total_line}) for o in groups["total"]]
            markets.append(ParsedMarket(name=f"Тотал {total_line}", kind="total", outcomes=tot))
        if not markets:
            continue
        try:
            out.append(ParsedEvent(
                bookmaker="fonbet", external_id=eid, sport=sport, tournament=tournament, home=home, away=away,
                start_text=start_text, start_time=parse_ru_datetime(start_text, fetched_at) if start_text else None,
                is_live=not start_text, url=BASE + a["href"], markets=markets, fetched_at=fetched_at,
            ))
        except ValidationError:
            continue
    return out
