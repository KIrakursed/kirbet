from decimal import Decimal

from app.providers.bookmakers.common.conftest_helpers import FETCHED, fixture
from app.providers.bookmakers.parimatch.parser import parse
from app.providers.bookmakers.parimatch.provider import ParimatchProvider


def test_parse_real_dom():
    evs = parse(fixture("parimatch"), "football", FETCHED)
    assert [e.external_id for e in evs] == ["19115869", "19115870"]
    e = evs[0]
    assert (e.home, e.away, e.sport, e.is_live) == ("Васко да Гама РЖ", "Интернасьонал РС", "football", False)
    kinds = {m.kind: m for m in e.markets}
    assert [o.odds for o in kinds["1x2"].outcomes] == [Decimal("1.58"), Decimal("4.22"), Decimal("5.39")]
    assert kinds["total"].name == "Тотал 2.5"
    assert kinds["handicap"].outcomes[0].name == "Ф1 (-1)"
    assert e.start_time.isoformat() == "2026-11-19T22:30:00+00:00"  # "Завтра, 03:30"


def test_pages_avoid_forbidden_feed():
    assert all("direct-feed" not in p.url for p in ParimatchProvider.pages)
