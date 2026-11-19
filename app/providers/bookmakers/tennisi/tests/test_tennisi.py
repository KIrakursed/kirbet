from app.providers.bookmakers.common.conftest_helpers import FETCHED, fixture
from app.providers.bookmakers.tennisi.parser import is_frameset, parse


def test_frameset_has_no_line():
    html = fixture("tennisi")
    assert is_frameset(html)
    assert parse(html, "main", FETCHED) == []


def test_plain_table_would_be_parsed():
    html = ("<table><tr><td>Футбол</td></tr>"
            "<tr><td>20.11 19:00</td><td>Астана - Кайрат</td><td>2.10</td><td>3.30</td><td>3.40</td></tr></table>")
    (e,) = parse(html, "main", FETCHED)
    assert (e.home, e.away, e.sport, e.markets[0].kind) == ("Астана", "Кайрат", "football", "1x2")
