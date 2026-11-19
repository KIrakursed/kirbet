"""Plans, limits, subscriptions and payments.

Rules:
  * a subscription is activated ONLY from a provider-confirmed payment (Telegram ``successful_payment``
    carrying ``telegram_payment_charge_id``) or by an admin action — never from a user's message or
    screenshot;
  * every confirmed charge is processed once: (provider, provider_charge_id) is UNIQUE in the DB and the
    handler is idempotent, so a repeated callback cannot activate or extend twice;
  * limits are counted per Asia/Almaty calendar day from ``usage_events``;
  * all money-relevant and admin actions are written to ``audit_logs``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.database import models as M
from app.domain import utcnow
from app.timeutil import almaty_day_bounds, utc

# Defaults seeded into the empty ``plans`` table; the admin changes prices / limits afterwards.
# Only features that really exist are listed: scan & notification quotas, history depth,
# own bookmaker / sport filters, the detailed card. (Markets beyond 1X2 / winner are collected and
# stored but do not take part in arbitrage yet, so no plan sells them.)
DEFAULT_PLANS = [
    dict(code="free", title="FREE", price_stars=0, price_kzt=0, period_days=0, scans_per_day=5,
         notifications_per_day=3, history_days=1, filters=False, details=False, sort=0),
    dict(code="basic", title="BASIC", price_stars=250, price_kzt=2990, period_days=30, scans_per_day=30,
         notifications_per_day=20, history_days=7, filters=True, details=False, sort=1),
    dict(code="pro", title="PRO", price_stars=750, price_kzt=7990, period_days=30, scans_per_day=200,
         notifications_per_day=100, history_days=30, filters=True, details=True, sort=2),
    dict(code="vip", title="VIP", price_stars=1500, price_kzt=14990, period_days=30, scans_per_day=1000,
         notifications_per_day=300, history_days=90, filters=True, details=True, sort=3),
]
EDITABLE = {"price_stars", "price_kzt", "period_days", "scans_per_day", "notifications_per_day", "history_days",
            "filters", "details", "active", "title"}


class BillingError(Exception):
    pass


@dataclass
class Quota:
    allowed: bool
    used: int
    limit: int
    plan: str


class Billing:
    def __init__(self, repo):
        self.repo = repo

    # ---- plans -------------------------------------------------------------------
    async def ensure_plans(self) -> None:
        async with self.repo.session() as s:
            have = {p.code for p in (await s.execute(select(M.Plan))).scalars()}
            for d in DEFAULT_PLANS:
                if d["code"] not in have:
                    s.add(M.Plan(**d))
            await s.commit()

    async def plans(self, *, only_active: bool = True) -> list[M.Plan]:
        async with self.repo.session() as s:
            q = select(M.Plan).order_by(M.Plan.sort)
            if only_active:
                q = q.where(M.Plan.active.is_(True))
            return list((await s.execute(q)).scalars())

    async def plan(self, code: str) -> M.Plan | None:
        async with self.repo.session() as s:
            return (await s.execute(select(M.Plan).where(M.Plan.code == code))).scalar_one_or_none()

    async def set_plan_field(self, code: str, field: str, value, actor: int) -> M.Plan:
        if field not in EDITABLE:
            raise BillingError(f"поле {field} менять нельзя; можно: {', '.join(sorted(EDITABLE))}")
        async with self.repo.session() as s:
            p = (await s.execute(select(M.Plan).where(M.Plan.code == code))).scalar_one_or_none()
            if p is None:
                raise BillingError(f"нет тарифа {code}")
            col = M.Plan.__table__.c[field]
            if col.type.python_type is bool:
                value = str(value).lower() in ("1", "true", "yes", "on", "да")
            else:
                value = col.type.python_type(value)
                if isinstance(value, int) and value < 0:
                    raise BillingError("значение не может быть отрицательным")
            old = getattr(p, field)
            setattr(p, field, value)
            s.add(M.AuditLog(actor_telegram_id=actor, action="plan.set", target=code,
                             details={"field": field, "old": str(old), "new": str(value)}))
            await s.commit()
            return p

    # ---- users / subscriptions -----------------------------------------------------
    async def _user(self, s, telegram_id: int) -> M.User:
        u = (await s.execute(select(M.User).where(M.User.telegram_id == telegram_id))).unique().scalar_one_or_none()
        if u is None:
            raise BillingError("пользователь не найден — он должен сначала отправить /start")
        return u

    async def active_subscription(self, user_id: int, now: datetime | None = None) -> M.Subscription | None:
        now = now or utcnow()
        async with self.repo.session() as s:
            return (await s.execute(
                select(M.Subscription).where(M.Subscription.user_id == user_id, M.Subscription.status == "active",
                                             M.Subscription.ends_at > now)
                .order_by(M.Subscription.ends_at.desc()).limit(1))).scalar_one_or_none()

    async def current_plan(self, user_id: int, now: datetime | None = None) -> M.Plan:
        sub = await self.active_subscription(user_id, now)
        code = sub.plan_code if sub else "free"
        p = await self.plan(code)
        if p is None:  # plans table not seeded yet
            await self.ensure_plans()
            p = await self.plan(code)
        return p

    async def _grant(self, s, user: M.User, plan: M.Plan, days: int, source: str, payment_id: int | None,
                     now: datetime) -> M.Subscription:
        """Extend from the end of the current subscription of the same plan, else start now."""
        cur = (await s.execute(
            select(M.Subscription).where(M.Subscription.user_id == user.id, M.Subscription.status == "active",
                                         M.Subscription.ends_at > now, M.Subscription.plan_code == plan.code)
            .order_by(M.Subscription.ends_at.desc()).limit(1))).scalar_one_or_none()
        start = utc(cur.ends_at) if cur else now  # SQLite returns naive UTC
        sub = M.Subscription(user_id=user.id, plan_code=plan.code, status="active", starts_at=start,
                             ends_at=start + timedelta(days=days), source=source, payment_id=payment_id)
        s.add(sub)
        return sub

    async def grant(self, telegram_id: int, code: str, days: int, *, actor: int, source: str = "admin",
                    now: datetime | None = None) -> M.Subscription:
        if days <= 0 or days > 366:
            raise BillingError("срок должен быть от 1 до 366 дней")
        now = now or utcnow()
        async with self.repo.session() as s:
            user = await self._user(s, telegram_id)
            plan = (await s.execute(select(M.Plan).where(M.Plan.code == code))).scalar_one_or_none()
            if plan is None or plan.code == "free":
                raise BillingError(f"нет платного тарифа {code}")
            sub = await self._grant(s, user, plan, days, source, None, now)
            s.add(M.AuditLog(actor_telegram_id=actor, action=f"subscription.{source}", target=str(telegram_id),
                             details={"plan": code, "days": days}))
            await s.commit()
            return sub

    async def cancel(self, telegram_id: int, *, actor: int) -> int:
        async with self.repo.session() as s:
            user = await self._user(s, telegram_id)
            r = await s.execute(update(M.Subscription).where(M.Subscription.user_id == user.id,
                                                             M.Subscription.status == "active")
                                .values(status="cancelled"))
            s.add(M.AuditLog(actor_telegram_id=actor, action="subscription.cancel", target=str(telegram_id)))
            await s.commit()
            return r.rowcount or 0

    async def expire_due(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        async with self.repo.session() as s:
            r = await s.execute(update(M.Subscription).where(M.Subscription.status == "active",
                                                             M.Subscription.ends_at <= now).values(status="expired"))
            await s.commit()
            return r.rowcount or 0

    async def due_reminders(self, days: int = 3, now: datetime | None = None) -> list[tuple[int, M.Subscription]]:
        """Active subscriptions ending within ``days`` and not reminded yet → [(telegram_id, sub)]; marks them."""
        now = now or utcnow()
        async with self.repo.session() as s:
            rows = (await s.execute(
                select(M.Subscription, M.User.telegram_id).join(M.User, M.User.id == M.Subscription.user_id)
                .where(M.Subscription.status == "active", M.Subscription.ends_at > now,
                       M.Subscription.ends_at <= now + timedelta(days=days), M.Subscription.reminded_at.is_(None))
            )).all()
            for sub, _ in rows:
                sub.reminded_at = now
            await s.commit()
            return [(tg, sub) for sub, tg in rows]

    # ---- quotas ----------------------------------------------------------------------
    async def consume(self, user_id: int, kind: str, now: datetime | None = None) -> Quota:
        """Check the plan's daily limit (Asia/Almaty day) and, if allowed, count one use."""
        now = now or utcnow()
        plan = await self.current_plan(user_id, now)
        limit = plan.scans_per_day if kind == "scan" else plan.notifications_per_day
        start, end = almaty_day_bounds(now)
        async with self.repo.session() as s:
            used = (await s.execute(select(func.count(M.UsageEvent.id)).where(
                M.UsageEvent.user_id == user_id, M.UsageEvent.kind == kind,
                M.UsageEvent.at >= start, M.UsageEvent.at < end))).scalar_one()
            if used >= limit:
                return Quota(False, used, limit, plan.code)
            s.add(M.UsageEvent(user_id=user_id, kind=kind, at=now))
            await s.commit()
            return Quota(True, used + 1, limit, plan.code)

    async def usage_today(self, user_id: int, now: datetime | None = None) -> dict[str, int]:
        start, end = almaty_day_bounds(now or utcnow())
        async with self.repo.session() as s:
            rows = (await s.execute(select(M.UsageEvent.kind, func.count(M.UsageEvent.id)).where(
                M.UsageEvent.user_id == user_id, M.UsageEvent.at >= start, M.UsageEvent.at < end)
                .group_by(M.UsageEvent.kind))).all()
        return {k: n for k, n in rows}

    # ---- payments --------------------------------------------------------------------
    async def create_payment(self, telegram_id: int, code: str, provider: str) -> M.Payment:
        async with self.repo.session() as s:
            user = await self._user(s, telegram_id)
            if user.is_blocked:
                raise BillingError("аккаунт заблокирован")
            plan = (await s.execute(select(M.Plan).where(M.Plan.code == code, M.Plan.active.is_(True)))) \
                .scalar_one_or_none()
            if plan is None or plan.price_stars <= 0:
                raise BillingError(f"тариф {code} нельзя оплатить")
            pay = M.Payment(user_id=user.id, plan_code=code, provider=provider,
                            invoice_payload=f"kb_{secrets.token_urlsafe(18)}", amount=plan.price_stars,
                            currency="XTR", status="created")
            s.add(pay)
            await s.commit()
            return pay

    async def validate_checkout(self, payload: str, telegram_id: int, amount: int, currency: str) -> str | None:
        """Pre-checkout: None = ok, otherwise the reason to refuse."""
        async with self.repo.session() as s:
            pay = (await s.execute(select(M.Payment).where(M.Payment.invoice_payload == payload))).scalar_one_or_none()
            if pay is None:
                return "счёт не найден"
            user = (await s.execute(select(M.User).where(M.User.id == pay.user_id))).unique().scalar_one()
            if user.telegram_id != telegram_id:
                return "счёт выставлен другому пользователю"
            if pay.status != "created":
                return "счёт уже оплачен или закрыт"
            if pay.amount != amount or pay.currency != currency:
                return "сумма не совпадает со счётом"
            return None

    async def confirm_payment(self, *, provider: str, payload: str, charge_id: str, telegram_id: int,
                              amount: int, currency: str, raw: dict | None = None,
                              now: datetime | None = None) -> tuple[M.Subscription | None, bool]:
        """Provider-confirmed payment → (subscription, created_now). Idempotent per charge id."""
        if not charge_id:
            raise BillingError("нет идентификатора платежа от провайдера — платёж не засчитывается")
        now = now or utcnow()
        async with self.repo.session() as s:
            dup = (await s.execute(select(M.Payment).where(M.Payment.provider == provider,
                                                           M.Payment.provider_charge_id == charge_id))) \
                .scalar_one_or_none()
            if dup is not None:  # repeated callback: nothing changes
                sub = (await s.execute(select(M.Subscription).where(M.Subscription.payment_id == dup.id))) \
                    .scalar_one_or_none()
                return sub, False
            pay = (await s.execute(select(M.Payment).where(M.Payment.invoice_payload == payload))) \
                .scalar_one_or_none()
            if pay is None:
                raise BillingError("платёж по неизвестному счёту")
            user = (await s.execute(select(M.User).where(M.User.id == pay.user_id))).unique().scalar_one()
            if user.telegram_id != telegram_id or pay.amount != amount or pay.currency != currency:
                pay.status = "failed"
                pay.raw = {**(raw or {}), "error": "mismatch"}
                s.add(M.AuditLog(actor_telegram_id=telegram_id, action="payment.mismatch", target=payload))
                await s.commit()
                raise BillingError("данные платежа не совпадают со счётом")
            if pay.status != "created":
                raise BillingError(f"счёт в статусе {pay.status}")
            plan = (await s.execute(select(M.Plan).where(M.Plan.code == pay.plan_code))).scalar_one()
            pay.status, pay.provider_charge_id, pay.paid_at, pay.raw = "paid", charge_id, now, raw or {}
            await s.flush()
            sub = await self._grant(s, user, plan, plan.period_days, "payment", pay.id, now)
            s.add(M.AuditLog(actor_telegram_id=telegram_id, action="payment.paid", target=payload,
                             details={"plan": plan.code, "amount": amount, "currency": currency,
                                      "charge_id": charge_id}))
            try:
                await s.commit()
            except IntegrityError:  # concurrent duplicate callback lost the race
                await s.rollback()
                return None, False
            return sub, True

    async def mark_refunded(self, charge_id: str, provider: str, *, actor: int) -> M.Payment:
        async with self.repo.session() as s:
            pay = (await s.execute(select(M.Payment).where(M.Payment.provider == provider,
                                                           M.Payment.provider_charge_id == charge_id))) \
                .scalar_one_or_none()
            if pay is None or pay.status != "paid":
                raise BillingError("оплаченный платёж не найден")
            pay.status = "refunded"
            await s.execute(update(M.Subscription).where(M.Subscription.payment_id == pay.id)
                            .values(status="refunded"))
            s.add(M.AuditLog(actor_telegram_id=actor, action="payment.refund", target=charge_id))
            await s.commit()
            return pay

    async def payments(self, telegram_id: int | None = None, limit: int = 20) -> list[M.Payment]:
        async with self.repo.session() as s:
            q = select(M.Payment).order_by(M.Payment.id.desc()).limit(limit)
            if telegram_id is not None:
                u = await self._user(s, telegram_id)
                q = q.where(M.Payment.user_id == u.id)
            return list((await s.execute(q)).scalars())

    # ---- admin -----------------------------------------------------------------------
    async def set_blocked(self, telegram_id: int, blocked: bool, *, actor: int) -> None:
        async with self.repo.session() as s:
            u = await self._user(s, telegram_id)
            u.is_blocked = blocked
            s.add(M.AuditLog(actor_telegram_id=actor, action="user.block" if blocked else "user.unblock",
                             target=str(telegram_id)))
            await s.commit()

    async def summary(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        async with self.repo.session() as s:
            users = (await s.execute(select(func.count(M.User.id)))).scalar_one()
            blocked = (await s.execute(select(func.count(M.User.id)).where(M.User.is_blocked.is_(True)))).scalar_one()
            active = dict((await s.execute(
                select(M.Subscription.plan_code, func.count(func.distinct(M.Subscription.user_id)))
                .where(M.Subscription.status == "active", M.Subscription.ends_at > now)
                .group_by(M.Subscription.plan_code))).all())
            paid = (await s.execute(select(func.count(M.Payment.id), func.coalesce(func.sum(M.Payment.amount), 0))
                                    .where(M.Payment.status == "paid"))).one()
        return {"users": users, "blocked": blocked, "active_by_plan": active, "paid_count": paid[0],
                "paid_stars": int(paid[1])}
