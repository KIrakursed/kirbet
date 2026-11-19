"""OLIMPBET DOM parser — "Топ события" block of the home page.

Per event id N: ``main-top-events-event-home-competitor-N`` / ``-away-competitor-N``,
``-event-date-N`` + ``-event-time-N`` and one or more
``main-top-events-event-cell-N`` cells, each "<label><odds>" (П1 / Х / П2).
The tournament name is the first ``<span>`` of the row header. The block has
no sport marker, so the provider clicks each sport tab and passes the sport
as the snapshot label; the unfiltered "Все" view (label ``main``) is skipped.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from app.providers.bookmakers.common.textutil import clean, parse_odds, parse_ru_datetime
from app.providers.bookmakers.olimpbet.models import ParsedEvent, ParsedMarket, ParsedOutcome

BASE = "https://olimpbet.kz"
P_HOME = "main-top-events-event-home-competitor-"


def _row(home: Tag, first_cell: Tag) -> Tag | None:
    """Lowest common ancestor of the competitor name and the event's first odds cell (linear)."""
    cell_ancestors = {id(a) for a in first_cell.parents}
    for a in home.parents:
        if id(a) in cell_ancestors:
            return a
    return None


def parse(html: str, label: str, fetched_at: datetime) -> list[ParsedEvent]:
    if label == "main":
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[ParsedEvent] = []
    # one pass over the document: every data-testid of the "Топ события" block, grouped by event id
    by_id: dict[str, dict[str, list[Tag]]] = {}
    for el in soup.find_all(attrs={"data-testid": lambda v: bool(v) and v.startswith("main-top-events-event-")}):
        kind, _, eid = el["data-testid"][len("main-top-events-event-"):].rpartition("-")
        by_id.setdefault(eid, {}).setdefault(kind, []).append(el)
    for eid, parts in by_id.items():
        homes, aways, cells = parts.get("home-competitor"), parts.get("away-competitor"), parts.get("cell", [])
        if not (homes and aways and cells):
            continue
        h, a = homes[0], aways[0]
        row = _row(h, cells[0])
        if row is None:
            continue
        d = (parts.get("date") or [None])[0]
        t = (parts.get("time") or [None])[0]
        start_text = clean(f"{d.get_text() if d else ''} {t.get_text() if t else ''}")
        span = row.find("span")
        tournament = clean(span.get_text()) if span and not span.get("data-testid") else ""
        outs: list[ParsedOutcome] = []
        for cell in cells:
            parts = [clean(x.get_text()) for x in cell.find_all("div", recursive=False)]
            if len(parts) != 2:
                continue
            odds = parse_odds(parts[1])
            if odds is None:
                continue
            try:
                outs.append(ParsedOutcome(name=parts[0], odds=odds))
            except ValidationError:
                continue
        names = [o.name for o in outs]
        try:
            if names == ["П1", "Х", "П2"]:
                markets = [ParsedMarket(name="1X2", kind="1x2", outcomes=outs)]
            elif names == ["П1", "П2"]:
                markets = [ParsedMarket(name="Победитель", kind="winner", outcomes=outs)]
            elif len(outs) >= 2:
                markets = [ParsedMarket(name=" / ".join(names), kind="other", outcomes=outs)]
            else:
                continue
            out.append(ParsedEvent(
                bookmaker="olimpbet", external_id=eid, sport=label, tournament=tournament,
                home=clean(h.get_text()), away=clean(a.get_text()), start_text=start_text,
                start_time=parse_ru_datetime(start_text, fetched_at) if start_text else None, is_live=False,
                url=BASE + "/", markets=markets, fetched_at=fetched_at,
            ))
        except ValidationError:
            continue
    return out


# ---------------------------------------------------------------- tournament pages /line/<sport>-<id>/<tid>
ROW = "line-events-event-row-"
LINE_CELLS = {  # data-testid cell code → (market group, outcome name)
    "w1": ("main", "П1"), "wx": ("main", "Х"), "w2": ("main", "П2"),
    "w1x": ("dc", "1Х"), "w12": ("dc", "12"), "wx2": ("dc", "Х2"),
    "hc1": ("hc", "Ф1"), "hc2": ("hc", "Ф2"), "to": ("total", "Больше"), "tu": ("total", "Меньше"),
}
TOUR_LINK = re.compile(r"^/line/([a-z_]+)-(\d+)/(\d+)$")


def parse_tournament_links(html: str) -> list[tuple[str, str, int]]:
    """/line/<sport>-<id> page → [(href, tournament name, events count)] (the count ends the link text)."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.select('a[data-testid^="line-tournaments-link-"]'):
        href = a.get("href") or ""
        if not TOUR_LINK.match(href):
            continue
        txt = clean(a.get_text(" "))
        m = re.match(r"^(.*?)\s+(\d+)$", txt)
        out.append((href, m.group(1) if m else txt, int(m.group(2)) if m else 0))
    return out


def parse_line(html: str, label: str, fetched_at: datetime) -> list[ParsedEvent]:
    """Tournament page. ``label`` = "<sport>|<tournament name>"."""
    sport, _, tournament = label.partition("|")
    soup = BeautifulSoup(html, "lxml")
    out: list[ParsedEvent] = []
    for row in soup.select(f'tr[data-testid^="{ROW}"]'):
        eid = row["data-testid"][len(ROW):]
        comp = row.select_one(f'[data-testid="line-events-event-competitors-{eid}"]')
        if comp is None:
            continue
        names = [clean(s.get_text()) for s in comp.find_all("span")]
        names = [n for n in names if n]
        if len(names) != 2:
            continue
        d = row.select_one(f'[data-testid="line-events-event-date-{eid}"]')
        start_text = clean(d.get_text()) if d else ""
        groups: dict[str, list[ParsedOutcome]] = {}
        total_line = None
        for cell in row.select(f'[data-testid^="line-events-event-cell-"][data-testid$="-{eid}"]'):
            code = cell["data-testid"][len("line-events-event-cell-"):-len(eid) - 1]
            if code not in LINE_CELLS or cell.get("aria-disabled") == "true":
                continue
            grp, oname = LINE_CELLS[code]
            odds = parse_odds(cell.get_text())
            if odds is None:
                continue
            param = None
            prev = cell.find_previous_sibling("div")
            if prev is not None and grp in ("hc", "total"):
                param = clean(prev.get_text()) or None
            if grp == "total":
                total_line = param or total_line
            try:
                groups.setdefault(grp, []).append(
                    ParsedOutcome(name=f"{oname} ({param})" if grp == "hc" and param else oname, odds=odds,
                                  param=param))
            except ValidationError:
                continue
        markets: list[ParsedMarket] = []
        try:
            main = groups.get("main", [])
            if [o.name for o in main] == ["П1", "Х", "П2"]:
                markets.append(ParsedMarket(name="1X2", kind="1x2", outcomes=main))
            elif [o.name for o in main] == ["П1", "П2"]:
                markets.append(ParsedMarket(name="Победитель", kind="winner", outcomes=main))
            if len(groups.get("dc", [])) == 3:
                markets.append(ParsedMarket(name="Двойной шанс", kind="other", outcomes=groups["dc"]))
            if len(groups.get("hc", [])) == 2:
                markets.append(ParsedMarket(name="Фора", kind="handicap", outcomes=groups["hc"]))
            if len(groups.get("total", [])) == 2 and total_line:
                tot = [o.model_copy(update={"param": total_line}) for o in groups["total"]]
                markets.append(ParsedMarket(name=f"Тотал {total_line}", kind="total", outcomes=tot))
        except ValidationError:
            pass
        if not markets:
            continue
        try:
            out.append(ParsedEvent(
                bookmaker="olimpbet", external_id=eid, sport=sport, tournament=tournament, home=names[0],
                away=names[1], start_text=start_text,
                start_time=parse_ru_datetime(start_text, fetched_at) if start_text else None, is_live=False,
                url=BASE + (comp.get("href") or "/"), markets=markets, fetched_at=fetched_at))
        except ValidationError:
            continue
    return out


def parse_any(html: str, label: str, fetched_at: datetime) -> list[ParsedEvent]:
    """Home-page tab snapshot (label = sport) or tournament page (label = "sport|tournament")."""
    return parse_line(html, label, fetched_at) if "|" in label else parse(html, label, fetched_at)
