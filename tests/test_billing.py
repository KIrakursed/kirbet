"""Plans, quotas, subscriptions and payment idempotency (isolated SQLite DB per test)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.billing import Billing, BillingError
from app.database.repo import Repo
from app.timeutil import ALMATY, utc

UTC = timezone.utc
T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    async def make():
        repo = Repo(f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
        await repo.init()
        b = Billing(repo)
        await b.ensure_plans()
        u = await repo.get_or_create_user(111, "u1")
        await repo.get_or_create_user(222, "u2")
        return repo, b, u
    repo, b, u = asyncio.run(make())
    yield repo, b, u
    asyncio.run(repo.close())


def run(c):
    return asyncio.run(c)


def test_plans_seeded_and_editable(env):
    repo, b, u = env
    assert [p.code for p in run(b.plans())] == ["free", "basic", "pro", "vip"]
    run(b.set_plan_field("pro", "price_stars", "900", actor=1))
    assert run(b.plan("pro")).price_stars == 900
    with pytest.raises(BillingError):
        run(b.set_plan_field("pro", "code", "x", actor=1))
    with pytest.raises(BillingError):
        run(b.set_plan_field("pro", "price_stars", "-5", actor=1))


def test_free_quota_counts_per_almaty_day(env):
    repo, b, u = env
    for i in range(5):
        assert run(b.consume(u.id, "scan", now=T0 + timedelta(minutes=i))).allowed
    q = run(b.consume(u.id, "scan", now=T0 + timedelta(minutes=10)))
    assert not q.allowed and q.used == 5 and q.plan == "free"
    # 19:00 UTC = 00:00 in Almaty → a new day, the quota resets (UTC day has not changed)
    assert run(b.consume(u.id, "scan", now=datetime(2026, 11, 19, 19, 0, 1, tzinfo=UTC))).allowed


def test_payment_activates_once_even_if_callback_repeats(env):
    repo, b, u = env
    pay = run(b.create_payment(111, "basic", "telegram_stars"))
    assert run(b.validate_checkout(pay.invoice_payload, 111, 250, "XTR")) is None
    assert run(b.validate_checkout(pay.invoice_payload, 222, 250, "XTR")) == "счёт выставлен другому пользователю"
    assert "сумма" in run(b.validate_checkout(pay.invoice_payload, 111, 1, "XTR"))
    sub, created = run(b.confirm_payment(provider="telegram_stars", payload=pay.invoice_payload, charge_id="ch_1",
                                         telegram_id=111, amount=250, currency="XTR", now=T0))
    assert created and sub.plan_code == "basic" and utc(sub.ends_at) == T0 + timedelta(days=30)
    again, created2 = run(b.confirm_payment(provider="telegram_stars", payload=pay.invoice_payload,
                                            charge_id="ch_1", telegram_id=111, amount=250, currency="XTR", now=T0))
    assert not created2 and again.id == sub.id  # duplicate webhook: no second activation
    assert run(b.current_plan(u.id, now=T0 + timedelta(days=1))).code == "basic"
    assert run(b.current_plan(u.id, now=T0 + timedelta(days=31))).code == "free"  # expired


def test_payment_requires_provider_charge_and_matching_amount(env):
    repo, b, u = env
    pay = run(b.create_payment(111, "pro", "telegram_stars"))
    with pytest.raises(BillingError, match="идентификатора"):
        run(b.confirm_payment(provider="telegram_stars", payload=pay.invoice_payload, charge_id="",
                              telegram_id=111, amount=750, currency="XTR"))
    with pytest.raises(BillingError, match="не совпадают"):
        run(b.confirm_payment(provider="telegram_stars", payload=pay.invoice_payload, charge_id="ch_x",
                              telegram_id=111, amount=1, currency="XTR"))
    with pytest.raises(BillingError):
        run(b.create_payment(111, "free", "telegram_stars"))


def test_renewal_extends_from_current_end(env):
    repo, b, u = env
    run(b.grant(111, "pro", 30, actor=1, now=T0))
    s2 = run(b.grant(111, "pro", 30, actor=1, now=T0 + timedelta(days=5)))
    assert utc(s2.ends_at) == T0 + timedelta(days=60)


def test_expiry_reminders_refund_block(env):
    repo, b, u = env
    pay = run(b.create_payment(111, "basic", "telegram_stars"))
    run(b.confirm_payment(provider="telegram_stars", payload=pay.invoice_payload, charge_id="ch_9",
                          telegram_id=111, amount=250, currency="XTR", now=T0))
    rem = run(b.due_reminders(3, now=T0 + timedelta(days=28)))
    assert [tg for tg, _ in rem] == [111]
    assert run(b.due_reminders(3, now=T0 + timedelta(days=28, hours=1))) == []  # reminded once
    run(b.mark_refunded("ch_9", "telegram_stars", actor=1))
    assert run(b.current_plan(u.id, now=T0 + timedelta(days=1))).code == "free"
    run(b.set_blocked(111, True, actor=1))
    with pytest.raises(BillingError, match="заблокирован"):
        run(b.create_payment(111, "basic", "telegram_stars"))
    assert run(b.expire_due(now=T0 + timedelta(days=400))) >= 0


def test_almaty_constant_is_zoneinfo():
    assert ALMATY.key == "Asia/Almaty"
