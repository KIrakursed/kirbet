from decimal import Decimal

from app.domain import MarketType
from app.providers.bookmakers.common.conftest_helpers import FETCHED, fixture
from app.providers.bookmakers.winline.normalizer import normalize
from app.providers.bookmakers.winline.parser import parse
from app.providers.bookmakers.winline.provider import WinlineProvider


def test_parse_real_dom():
    evs = parse(fixture("winline"), "main", FETCHED)
    assert len(evs) == 5
    e = next(x for x in evs if x.external_id == "117196204")
    assert (e.home, e.away, e.sport) == ("Зверев А.", "Филс А.", "tennis")
    assert e.markets[0].name == "Победитель"
    assert [(o.name, o.odds) for o in e.markets[0].outcomes] == [("П1", Decimal("1.37")), ("П2", Decimal("3.10"))]
    assert e.start_time.isoformat() == "2026-11-19T19:30:00+00:00"  # "Завтра 00:30" Almaty
    assert e.url.startswith("https://winline.kz/sport/167/")
    assert all(x.is_complete() for x in evs)


def test_football_1x2_and_esports_split():
    evs = {e.external_id: e for e in parse(fixture("winline"), "main", FETCHED)}
    fb = evs["117150022"]
    assert fb.sport == "football" and fb.markets[0].kind == "1x2"
    assert [o.name for o in fb.markets[0].outcomes] == ["П1", "Х", "П2"]
    assert evs["117204193"].sport == "dota2"


def test_normalize_to_domain():
    ms = normalize(parse(fixture("winline"), "main", FETCHED))
    assert ms and all(m.market_type == MarketType.MATCH_WINNER for m in ms)
    fb = next(m for m in ms if m.event_id == "117150022")
    assert [o.label for o in fb.outcomes] == ["Аль-Васл Дубай", "Draw", "Хатта"]
    assert fb.outcomes[1].quotes[0].best_price == Decimal("8.00")


def test_provider_only_opens_parameter_free_pages():
    urls = [p.url for p in WinlineProvider.pages]
    assert urls == ["https://winline.kz/sport/all", "https://winline.kz/"]
    assert all("?" not in u and "/api/" not in u for u in urls)  # robots.txt: Disallow /*?


def test_line_page_handicap_and_total():
    from bs4 import BeautifulSoup

    from app.providers.bookmakers.winline.parser import parse_markets

    html = ('<div data-t="eventContainer"><div data-t="handicap-market">'
            '<button data-t="outcome" data-market-id="16"><span data-t="odds">2.12</span></button>'
            '<div data-t="current-specifier">– <span>2.5</span> +</div>'
            '<button data-t="outcome" data-market-id="16"><span data-t="odds">1.62</span></button></div>'
            '<div data-t="total-market"><button data-t="outcome" data-market-id="18"><span data-t="odds">2.39</span>'
            '</button><div data-t="current-specifier"><span>2.5</span></div>'
            '<button data-t="outcome" data-market-id="18"><span data-t="odds">1.48</span></button></div>'
            '<div data-t="total-market"><button data-t="outcome" data-market-id="225"><span data-t="odds">1.8</span>'
            '</button><div data-t="current-specifier"><span>51.5</span></div>'
            '<button data-t="outcome" data-market-id="225"><span data-t="odds">1.9</span></button></div></div>')
    ms = parse_markets(BeautifulSoup(html, "lxml").select_one('[data-t="eventContainer"]'))
    hc, tot, unknown = ms
    assert hc.kind == "handicap" and [o.name for o in hc.outcomes] == ["Ф1 (-2.5)", "Ф2 (+2.5)"]
    assert tot.kind == "total" and [(o.name, o.param) for o in tot.outcomes] == [("Меньше", "2.5"), ("Больше", "2.5")]
    assert unknown.kind == "other" and "#225" in unknown.name  # unconfirmed market id is never matched
