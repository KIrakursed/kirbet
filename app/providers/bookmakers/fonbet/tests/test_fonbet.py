from decimal import Decimal

from app.providers.bookmakers.common.conftest_helpers import FETCHED, fixture
from app.providers.bookmakers.fonbet.normalizer import normalize
from app.providers.bookmakers.fonbet.parser import parse


def test_parse_real_dom_columns():
    evs = parse(fixture("fonbet"), "main", FETCHED)
    assert len(evs) == 3
    e = evs[0]
    assert (e.external_id, e.home, e.away, e.sport) == ("69559405", "Ростов", "Крылья Советов", "football")
    assert e.tournament == "Россия. Премьер-Лига. Сезон 26/27"
    by = {m.name: m for m in e.markets}
    assert [(o.name, o.odds) for o in by["1X2"].outcomes] == [
        ("П1", Decimal("1.95")), ("Х", Decimal("3.50")), ("П2", Decimal("4.05"))]
    assert [o.name for o in by["Фора"].outcomes] == ["Ф1 (-1)", "Ф2 (+1)"]
    assert by["Тотал 2.5"].outcomes[0].param == "2.5" and by["Тотал 2.5"].kind == "total"
    assert e.start_time.isoformat() == "2026-11-20T16:30:00+00:00"  # "Завтра в 21:30"


def test_normalize():
    from app.domain import MarketType

    ms = normalize(parse(fixture("fonbet"), "main", FETCHED))
    winners = [m for m in ms if m.market_type == MarketType.MATCH_WINNER]
    assert len(winners) == 3 and winners[0].settlement.draw_possible
    assert winners[0].url == "https://fonbet.kz/sports/football/11935/69559405"
    # totals 2.5 of the three matches; their handicaps (-1 / 0) are whole lines → not used for arbitrage
    lines = [m for m in ms if m.market_type != MarketType.MATCH_WINNER]
    assert {m.period for m in lines} == {"regular_time|total=2.5"} and len(lines) == 3
