"""Telegram layer of the commercial features (handlers called with fake updates, isolated DB)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.types import CallbackQuery, Message

import app.database.repo_ext  # noqa: F401
from app.billing import Billing
from app.config import get_settings
from app.database.repo import Repo
from app.telegram import commerce
from app.telegram.bot import B_FILTERS, B_SCAN


class Svc:
    def __init__(self, repo):
        self.repo, self.billing = repo, Billing(repo)
        self.counters = {"scans": 0, "cache_hits": 0, "waited": 0, "errors": 0, "notified": 0}
        self.providers, self.last_success_at = {}, None


def fake_msg(uid: int, text: str = "", **kw):
    sent = []

    async def answer(t, **_):
        sent.append(t)

    async def delete():
        return True

    m = Message.model_construct(text=text, from_user=SimpleNamespace(id=uid, username=f"u{uid}", is_bot=False),
                                **kw)
    object.__setattr__(m, "answer", answer)
    object.__setattr__(m, "delete", delete)
    object.__setattr__(m, "sent", sent)
    return m


@pytest.fixture
def svc(tmp_path, monkeypatch):
    async def make():
        repo = Repo(f"sqlite+aiosqlite:///{(tmp_path / 'b.db').as_posix()}")
        await repo.init()
        s = Svc(repo)
        await s.billing.ensure_plans()
        return s
    s = asyncio.run(make())
    monkeypatch.setattr(get_settings(), "bot_admin_ids", "900")
    monkeypatch.setattr(get_settings(), "admin_pin", "4321")
    monkeypatch.setattr(get_settings(), "payments_enabled", True)
    commerce._admin_sessions.clear()
    yield s
    asyncio.run(s.repo.close())


async def through(mw, event, svc, uid):
    called = []

    async def handler(ev, data):
        called.append(data.get("kb_plan").code)
        return "handled"

    await mw(handler, event, {"service": svc, "event_from_user": SimpleNamespace(id=uid, username="x")})
    return called


def test_free_scan_quota_and_filter_gate(svc):
    mw = commerce.PlanMiddleware()

    async def go():
        ok = [await through(mw, fake_msg(1, B_SCAN), svc, 1) for _ in range(6)]
        assert [bool(x) for x in ok] == [True] * 5 + [False]  # FREE: 5 scans per Almaty day
        m = fake_msg(1, B_FILTERS)
        assert not await through(mw, m, svc, 1) and "BASIC" in m.sent[0]
        await svc.billing.grant(1, "basic", 30, actor=900)
        assert await through(mw, fake_msg(1, B_FILTERS), svc, 1) == ["basic"]
        cb = CallbackQuery.model_construct(id="1", data="det:abc:1000", chat_instance="x",
                                           from_user=SimpleNamespace(id=1, username="x", is_bot=False))
        alerts = []

        async def cb_answer(text=None, show_alert=False, **_):
            alerts.append(text)
        object.__setattr__(cb, "answer", cb_answer)
        assert not await through(mw, cb, svc, 1) and "PRO" in alerts[0]  # detailed card = PRO+
    asyncio.run(go())


def test_blocked_user_is_refused(svc):
    async def go():
        await svc.repo.get_or_create_user(5, "bad")
        await svc.billing.set_blocked(5, True, actor=900)
        m = fake_msg(5, "/start")
        assert not await through(commerce.PlanMiddleware(), m, svc, 5)
        assert "ограничен" in m.sent[0]
    asyncio.run(go())


def test_admin_requires_id_and_pin(svc):
    async def go():
        await svc.repo.get_or_create_user(7, "target")
        outsider = fake_msg(1, "/grant 7 pro 30")
        await commerce.grant_cmd(outsider, SimpleNamespace(args="7 pro 30"), svc)
        assert outsider.sent == []  # not an admin: silently ignored
        admin = fake_msg(900, "/grant 7 pro 30")
        await commerce.grant_cmd(admin, SimpleNamespace(args="7 pro 30"), svc)
        assert "авторизация" in admin.sent[0]
        bad = fake_msg(900, "/auth 0000")
        await commerce.auth_cmd(bad, SimpleNamespace(args="0000"), svc)
        assert "Неверный" in bad.sent[0]
        good = fake_msg(900, "/auth 4321")
        await commerce.auth_cmd(good, SimpleNamespace(args="4321"), svc)
        admin2 = fake_msg(900, "/grant 7 pro 30")
        await commerce.grant_cmd(admin2, SimpleNamespace(args="7 pro 30"), svc)
        assert admin2.sent[0].startswith("✅ PRO для 7")
        u = await svc.repo.get_user(7)
        assert (await svc.billing.current_plan(u.id)).code == "pro"
    asyncio.run(go())


def test_successful_payment_handler_is_idempotent(svc):
    async def go():
        await svc.repo.get_or_create_user(3, "payer")
        pay = await svc.billing.create_payment(3, "basic", "telegram_stars")
        sp = SimpleNamespace(invoice_payload=pay.invoice_payload, telegram_payment_charge_id="tg_ch_1",
                             provider_payment_charge_id="", total_amount=pay.amount, currency="XTR")
        m1 = fake_msg(3, "", successful_payment=sp)
        await commerce.paid(m1, svc)
        m2 = fake_msg(3, "", successful_payment=sp)
        await commerce.paid(m2, svc)  # Telegram re-delivers the same update
        assert "Оплата подтверждена" in m1.sent[0] and m2.sent == []
        subs = await svc.billing.summary()
        assert subs["paid_count"] == 1 and subs["active_by_plan"] == {"basic": 1}
    asyncio.run(go())


def test_pre_checkout_refused_when_payments_off(svc, monkeypatch):
    monkeypatch.setattr(get_settings(), "payments_enabled", False)
    got = {}

    async def ans(ok, error_message=None):
        got.update(ok=ok, err=error_message)
    q = SimpleNamespace(invoice_payload="x", from_user=SimpleNamespace(id=1), total_amount=1, currency="XTR",
                        answer=ans)
    asyncio.run(commerce.pre_checkout(q, svc))
    assert got == {"ok": False, "err": "Оплата отключена"}
