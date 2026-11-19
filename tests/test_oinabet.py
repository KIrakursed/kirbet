from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.matching.engine import match_markets
from app.normalization.names import similarity, team_markers, translit
from app.providers.bookmakers.oinabet_kz import LineState, build_market, decode_price
from app.providers.bookmakers.oinabet_signalr import decode_frames, decode_update, encode_frame

START = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_price_decoding():
    assert decode_price({"p2": 147}) == D("1.47")
    assert decode_price({"p1": 68}) == D("6.8")
    assert decode_price({"p3": 1050}) == D("1.05")
    assert decode_price({}) is None


def test_frame_roundtrip():
    raw = encode_frame([1, {}, None, "updateState", 0, [{"t": "k", "d": {"r": True}}], []]) + encode_frame([6])
    msgs = decode_frames(raw)
    assert msgs[1] == [6]
    assert decode_update(msgs[0])[0]["t"] == "k"


def _snapshot():
    return {"t": "sport-1", "d": {"r": True, "o": {"10": {"n": "Ростов"}, "11": {"n": "Крылья Советов"}},
                                  "to": {"5": {"n": "РПЛ"}},
                                  "m": {"77": {"i": 77, "s": 1, "to": 5, "d": START, "tm": None, "o": {"1": 10, "2": 11},
                                               "st": {"900": {"i": 900, "t": 206138, "o": True, "p": {"partType": 106}}},
                                               "b": {"1": {"i": 1, "s": 900, "k": 1, "p2": 192, "o": True},
                                                     "2": {"i": 2, "s": 900, "k": 2, "p2": 362, "o": True},
                                                     "3": {"i": 3, "s": 900, "k": 3, "p2": 423, "o": True}}}}}}


def test_state_merge_and_market():
    st = LineState()
    st.apply(_snapshot())
    m = build_market(st.matches[77], st, datetime.now(timezone.utc))
    assert m.participants == ("Ростов", "Крылья Советов") and m.tournament == "РПЛ"
    assert [o.quotes[0].best_price for o in m.outcomes] == [D("1.92"), D("3.62"), D("4.23")]
    assert m.period == "regular_time" and m.settlement.draw_possible
    # incremental update: only the changed coefficient, other fields null
    st.apply({"t": "sport-1", "d": {"r": False, "m": {"77": {"i": 77, "s": None, "b": {"1": {"p2": 195}}}}}})
    m = build_market(st.matches[77], st, datetime.now(timezone.utc))
    assert m.outcomes[0].quotes[0].best_price == D("1.95") and m.outcomes[2].quotes[0].best_price == D("4.23")
    # live (timer present) → not used as prematch
    st.apply({"t": "sport-1", "d": {"m": {"77": {"tm": {"s": True}}}}})
    assert build_market(st.matches[77], st, datetime.now(timezone.utc)) is None
    st.apply({"t": "sport-1", "d": {"rm": [77]}})
    assert 77 not in st.matches


def test_team_markers_block_women_vs_men():
    assert team_markers("Динамо Краснодар (ж)") == {"women"}
    assert team_markers("Женис") == frozenset() and team_markers("Dinamo II") == {"reserve"}
    assert similarity("Динамо Краснодар (ж)", "Динамо Краснодар") == 0.0
    assert similarity("Спартак U19", "Спартак") == 0.0
    assert similarity("Спартак (ж)", "Спартак (жен)") > 0.9


def test_translit_matches_russian_to_english_person():
    assert translit("Даниил Медведев") == "daniil medvedev"
    assert similarity(translit("Даниил Медведев"), "Daniil Medvedev", person=True) >= 0.95


def test_cross_language_market_match():
    from tests.helpers import h2h_market
    ru = h2h_market("oinabet", "Даниил Медведев", "Александр Зверев", "1.6", "2.4")
    en = h2h_market("kalshi", "Daniil Medvedev", "Alexander Zverev", "0.6", "0.4")
    r = match_markets(ru, en)
    assert r is not None
    assert r.outcome_map["даниил медведев"] == "daniil medvedev"


def test_inexact_translit_is_not_forced():
    # "Тиафо" → "tiafo" ≠ "tiafoe": better to miss a pair than to merge the wrong one
    assert similarity(translit("Фрэнсис Тиафо"), "Frances Tiafoe", person=True) < 0.85
