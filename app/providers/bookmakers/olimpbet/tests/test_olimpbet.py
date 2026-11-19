from decimal import Decimal

from app.providers.bookmakers.common.conftest_helpers import FETCHED, fixture
from app.providers.bookmakers.olimpbet.parser import parse
from app.providers.bookmakers.olimpbet.provider import OlimpbetProvider


def test_unfiltered_view_is_skipped_without_sport():
    assert parse(fixture("olimpbet"), "main", FETCHED) == []


def test_parse_real_dom_with_tab_sport():
    evs = parse(fixture("olimpbet"), "tennis", FETCHED)
    e = next(x for x in evs if x.external_id == "9339574")
    assert (e.home, e.away, e.sport) == ("Шелтон Бен", "Ходар Рафаэль", "tennis")
    assert e.tournament.startswith("ATP Finals")
    assert e.markets[0].kind == "winner"
    assert [o.odds for o in e.markets[0].outcomes] == [Decimal("1.35"), Decimal("3.25")]
    assert e.start_time.isoformat() == "2026-11-19T13:00:00+00:00"
    three = next(x for x in evs if x.external_id == "9326019")
    assert [o.name for o in three.markets[0].outcomes] == ["П1", "Х", "П2"]


def test_provider_clicks_sport_tabs_on_home_only():
    (spec,) = OlimpbetProvider.pages
    assert spec.url == "https://olimpbet.kz/" and {lab for lab, _ in spec.actions} >= {"football", "tennis"}


def test_tournament_page_markets():
    from pathlib import Path

    from app.providers.bookmakers.olimpbet.parser import parse_any

    html = (Path(__file__).parent / "fixtures" / "olimpbet_tournament.html").read_text(encoding="utf-8")
    evs = parse_any(html, "football|Англия. Премьер-лига", FETCHED)
    assert len(evs) == 9
    e = evs[0]
    assert (e.home, e.away, e.sport, e.tournament) == ("Манчестер Сити", "Фулхэм", "football", "Англия. Премьер-лига")
    kinds = {m.kind: m for m in e.markets}
    assert [o.name for o in kinds["1x2"].outcomes] == ["П1", "Х", "П2"]
    assert [(o.name, o.param) for o in kinds["handicap"].outcomes] == [("Ф1 (-1.5)", "-1.5"), ("Ф2 (+1.5)", "+1.5")]
    assert kinds["total"].outcomes[0].param == "3" and kinds["total"].outcomes[0].name == "Больше"
    assert e.start_time.isoformat() == "2026-11-21T12:30:00+00:00"  # "21 ноября, 17:30" Almaty


def test_discovery_prefers_tournaments_shared_with_other_bookmakers():
    import asyncio

    from app.providers.bookmakers.olimpbet import provider as prov
    from app.providers.browser.zendriver_client import PageResult, ZendriverClient

    links = "".join(f'<a data-testid="line-tournaments-link-100-{i}" href="/line/football-100/{i}">{n} {c}</a>'
                    for i, (n, c) in enumerate([("Англия. Премьер-лига", 20), ("Испания. Ла Лига", 20),
                                                ("Россия. Премьер-лига", 8), ("Италия. Серия A", 19),
                                                ("Казахстан. Премьер-лига", 3), ("Германия. Бундеслига", 18)]))

    class B(ZendriverClient):
        async def fetch(self, url, **kw):
            html = links if url.endswith("football-100") else "<html></html>"
            return PageResult(url=url, http_status=200, html=html)
    p = prov.OlimpbetProvider(browser=B())
    old = dict(prov.LINE_SPORTS)
    prov.LINE_SPORTS.clear()
    prov.LINE_SPORTS["football-100"] = ("football", 3)
    try:
        specs = asyncio.run(p.discover())
    finally:
        prov.LINE_SPORTS.clear()
        prov.LINE_SPORTS.update(old)
    names = [s.label.split("|")[1] for s in specs]
    assert names[:2] == ["Россия. Премьер-лига", "Казахстан. Премьер-лига"] and len(names) == 3
