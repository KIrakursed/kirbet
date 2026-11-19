"""Asia/Almaty everywhere: UTC ↔ Almaty conversion, schedule grid, day boundaries, no guessing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.providers.bookmakers.common.textutil import parse_ru_datetime
from app.timeutil import ALMATY, TZ_NAME, almaty, almaty_day_bounds, aware_or_none, next_aligned_run, utc

UTC = timezone.utc


def test_almaty_is_iana_zoneinfo_not_a_fixed_offset():
    assert isinstance(ALMATY, ZoneInfo) and ALMATY.key == TZ_NAME == "Asia/Almaty"


@pytest.mark.parametrize("u, local", [
    (datetime(2026, 11, 19, 12, 0, tzinfo=UTC), "2026-11-19 17:00"),
    (datetime(2026, 11, 19, 19, 0, tzinfo=UTC), "2026-11-20 00:00"),  # date rolls over at 19:00 UTC
    (datetime(2026, 12, 31, 20, 30, tzinfo=UTC), "2027-01-01 01:30"),  # year rolls over
    (datetime(2026, 11, 19, 18, 59), "2026-11-19 23:59"),  # naive DB value = UTC
])
def test_utc_to_almaty(u, local):
    assert almaty(u).strftime("%Y-%m-%d %H:%M") == local


def test_almaty_to_utc_roundtrip():
    local = datetime(2026, 11, 20, 0, 30, tzinfo=ALMATY)
    assert utc(local) == datetime(2026, 11, 19, 19, 30, tzinfo=UTC)
    assert almaty(utc(local)) == local


def test_bookmaker_text_times_are_almaty_wall_clock():
    fetched = datetime(2026, 11, 19, 18, 30, tzinfo=UTC)  # 23:30 in Almaty
    assert parse_ru_datetime("Сегодня 23:45", fetched) == datetime(2026, 11, 19, 18, 45, tzinfo=UTC)
    # "Завтра" is relative to the Almaty date (20.11), not the UTC date (19.11)
    fetched2 = datetime(2026, 11, 19, 19, 30, tzinfo=UTC)  # already 20.11 00:30 in Almaty
    assert parse_ru_datetime("Завтра 10:00", fetched2) == datetime(2026, 11, 21, 5, 0, tzinfo=UTC)


def test_naive_source_time_is_not_guessed():
    assert aware_or_none(datetime(2026, 11, 20, 10, 0)) is None
    assert aware_or_none(datetime(2026, 11, 20, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))) == \
        datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


def test_kalshi_rule_zones_use_iana_dst():
    from app.providers.prediction_markets.kalshi import parse_scheduled

    summer = parse_scheduled("game scheduled for July 4, 2026 at 7:00 PM ET")
    winter = parse_scheduled("game scheduled for Dec 4, 2026 at 7:00 PM ET")
    assert summer == datetime(2026, 7, 4, 23, 0, tzinfo=UTC)  # EDT = UTC-4
    assert winter == datetime(2026, 12, 5, 0, 0, tzinfo=UTC)  # EST = UTC-5
    assert parse_scheduled("game scheduled for Dec 4, 2026 at 7:00 PM XYZ") is None  # unknown → not guessed


def test_schedule_grid_follows_almaty_clock():
    now = datetime(2026, 11, 19, 13, 0, 10, tzinfo=UTC)  # 18:00:10 Almaty
    assert almaty(next_aligned_run(now, 90)).strftime("%H:%M:%S") == "18:01:30"
    assert almaty(next_aligned_run(now, 3600)).strftime("%H:%M") == "19:00"
    late = datetime(2026, 11, 19, 18, 59, 50, tzinfo=UTC)  # 23:59:50 Almaty
    nxt = next_aligned_run(late, 3600)
    assert almaty(nxt) == datetime(2026, 11, 20, 0, 0, tzinfo=ALMATY)  # restarts at Almaty midnight


def test_almaty_day_bounds_for_statistics():
    start, end = almaty_day_bounds(datetime(2026, 11, 19, 20, 0, tzinfo=UTC))  # 20.11 01:00 Almaty
    assert start == datetime(2026, 11, 19, 19, 0, tzinfo=UTC) and end - start == timedelta(days=1)


def test_odds_api_day_resets_at_almaty_midnight(tmp_path, monkeypatch):
    import app.providers.aggregators.the_odds_api as mod

    monkeypatch.setattr(mod.TheOddsApiProvider, "LEDGER", tmp_path / "c.json")
    p = mod.TheOddsApiProvider()
    monkeypatch.setattr(mod, "now_almaty", lambda: datetime(2026, 11, 19, 23, 50, tzinfo=ALMATY))
    p.last_headers = {"x-requests-remaining": "300", "x-requests-last": "3"}
    p._track_credits()
    assert p.credits()["used_today"] == 3
    # 19:10 UTC is still 19.11 in UTC, but already 20.11 in Almaty → a new budget day
    monkeypatch.setattr(mod, "now_almaty", lambda: datetime(2026, 11, 20, 0, 10, tzinfo=ALMATY))
    assert p.credits()["used_today"] == 0 and p.credits()["date"] == "2026-11-20"


def test_dom_page_in_other_zone_marks_timezone_unknown():
    from app.providers.bookmakers.common.conftest_helpers import FETCHED, fixture
    from app.providers.bookmakers.common.dom_provider import PageSpec
    from app.providers.bookmakers.winline.provider import WinlineProvider
    from app.providers.browser.zendriver_client import PageResult

    p = WinlineProvider(browser=object())
    spec = PageSpec("https://winline.kz/")
    ok = p.parse_page(PageResult(url=spec.url, html=fixture("winline"), fetched_at=FETCHED,
                                 page_timezone="Asia/Almaty"), spec)
    bad = p.parse_page(PageResult(url=spec.url, html=fixture("winline"), fetched_at=FETCHED,
                                  page_timezone="Europe/Berlin"), spec)
    assert any(e.start_time for e in ok) and not any(e.timezone_unknown for e in ok)
    assert all(e.timezone_unknown and e.start_time is None for e in bad)
