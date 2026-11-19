import asyncio
from decimal import Decimal as D

import pytest

from app.arbitrage.optimizer import FX
from app.calculator.calc import CalcError, calculate
from app.database.repo import Repo
from app.domain import OppStatus
from app.providers.prediction_markets.kalshi import kalshi_settlement, parse_scheduled
from app.providers.prediction_markets.polymarket import parse_settlement
from app.telegram.formatting import format_opportunity
from tests.test_matching_validation import FakeProvider, _cross_opp, _validator


def test_calculator_basic():
    out = calculate(D("10000"), ["2.20", "2.10"])
    assert out.result.is_arb
    assert out.result.total_cost <= D("10000")
    out2 = calculate(D("100"), ["0.40 pm vol=50", "0.50 kalshi"])
    assert out2.result is not None
    assert out2.result.payout_units <= D("50.01")
    with pytest.raises(CalcError):
        calculate(D("100"), ["2.0"])
    with pytest.raises(CalcError):
        calculate(D("100"), ["abc", "2"])


def test_parse_rules():
    pm = parse_settlement(
        "If the match is canceled ... this market will resolve to 50-50.\n\nIf the match begins but is not "
        "completed, and one player advances due to the opponent's retirement, default, or disqualification, "
        "this market will resolve to the player who advances.")
    assert pm.walkover.value == "50_50" and pm.retirement.value == "advancing_player"
    k = kalshi_settlement("tennis", "If X wins ... after a ball has been played ... resolve to a fair price")
    assert k.retirement.value == "advancing_player" and k.walkover.value == "fair_price"
    dt = parse_scheduled("CS2 match originally scheduled for Nov 17, 2026 at 9:00 AM EST, then")
    assert dt.hour == 14 and dt.day == 17


def test_formatting_has_required_fields():
    o = _cross_opp()
    asyncio.run(_validator(polymarket=FakeProvider(), kalshi=FakeProvider()).validate(o))
    t = format_opportunity(o, FX("USD", {"KZT": D(1) / D(500)}), html=False)
    for s in ("Событие", "Категория", "Рынок", "ROI", "Статус: VERIFIED", "Возраст котировок",
              "не гарантирует", "Цена покупки (ask)", "Доступно в стакане", "комиссия"):
        assert s in t, s


# 22. duplicate notifications
def test_db_roundtrip_and_dedupe(tmp_path):
    async def run():
        repo = Repo(f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
        await repo.init()
        u = await repo.get_or_create_user(42, "x")
        assert u.settings.notifications
        await repo.update_settings(42, min_roi_pct=D("1.5"), sports=["tennis"])
        u = await repo.get_or_create_user(42)
        assert u.settings.min_roi_pct == D("1.5") and u.settings.sports == ["tennis"]
        o = _cross_opp()
        await _validator(polymarket=FakeProvider(), kalshi=FakeProvider()).validate(o)
        oid = await repo.save_opportunity(o, "text")
        assert oid
        assert await repo.should_notify(u.id, o.signature, D("2"))
        await repo.record_notification(u.id, o.signature, D("2"), oid)
        assert not await repo.should_notify(u.id, o.signature, D("2.1"))  # duplicate
        assert await repo.should_notify(u.id, o.signature, D("3"))  # ROI grew notably
        rows = await repo.recent_opportunities(statuses=(OppStatus.VERIFIED.value,))
        assert rows and rows[0].legs is not None
        st = await repo.stats()
        assert st["notifications"] == 1
        await repo.close()

    asyncio.run(run())


def test_bot_router_builds():
    from app.telegram.bot import build_dispatcher, main_kb

    dp = build_dispatcher(object())
    assert dp is not None
    texts = [b.text for row in main_kb().keyboard for b in row]
    assert "🔎 Сканировать" in texts and "⚙️ Настройки" in texts and "📊 Обзор рынка" in texts and "✍️ Ввести коэффициенты" in texts and len(texts) == 11
