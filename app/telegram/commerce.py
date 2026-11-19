"""Commercial Telegram layer: plans & quotas, payments (Telegram Stars), profile, info pages, admin panel.

Payments: Telegram Stars (currency XTR) — the subscription is activated only from Telegram's
``successful_payment`` update, whose ``telegram_payment_charge_id`` is stored UNIQUE (idempotent).
They stay OFF until ``PAYMENTS_ENABLED=true`` is set by the owner.

Admin: Telegram ids in ``BOT_ADMIN_IDS``; sensitive actions (grant / cancel / block / prices /
refund / sources) additionally need ``/auth <ADMIN_PIN>`` within the last 15 minutes.
"""

from __future__ import annotations

import hmac
import logging
import time
from html import escape
from pathlib import Path

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from app.billing import BillingError
from app.config import ROOT, get_settings
from app.timeutil import fmt_almaty

log = logging.getLogger("telegram.commerce")
router = Router(name="commerce")
IB = InlineKeyboardButton
DOCS = ROOT / "docs"
PIN_TTL = 15 * 60
_admin_sessions: dict[int, float] = {}

COMMANDS = [
    ("start", "Регистрация и главное меню"), ("scan", "Найти вилки сейчас"), ("plan", "Тарифы"),
    ("subscribe", "Оформить подписку"), ("profile", "Профиль"), ("balance", "Состояние подписки"),
    ("settings", "Настройки"), ("history", "История"), ("stats", "Статистика"), ("sports", "Виды спорта"),
    ("bookmakers", "Букмекеры и их состояние"), ("status", "Состояние системы"), ("help", "Помощь"),
    ("support", "Поддержка"), ("terms", "Соглашение, риски, конфиденциальность"),
]


async def set_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    except Exception as e:  # noqa: BLE001
        log.warning("set_my_commands: %s", e)


def admin_ids() -> set[int]:
    return {int(x) for x in get_settings().bot_admin_ids.replace(" ", "").split(",") if x.strip().isdigit()}


def is_admin(uid: int) -> bool:
    return uid in admin_ids()


def admin_authed(uid: int) -> bool:
    return is_admin(uid) and time.monotonic() - _admin_sessions.get(uid, -1e9) < PIN_TTL


# ---------------------------------------------------------------- plan middleware
class PlanMiddleware(BaseMiddleware):
    """Registers / refreshes the user, refuses blocked users, injects ``kb_user`` / ``kb_plan`` and
    enforces plan gates: daily scan quota, filters, detailed card."""

    async def __call__(self, handler, event, data):
        service = data.get("service")
        tg = data.get("event_from_user")
        if service is None or tg is None or getattr(service, "billing", None) is None:
            return await handler(event, data)
        u = await service.repo.get_or_create_user(tg.id, tg.username)
        if u.is_blocked and not is_admin(tg.id):
            if isinstance(event, Message):
                await event.answer("⛔ Доступ к боту ограничен. Если это ошибка — /support")
            return None
        await service.repo.touch_user(u.id)
        plan = await service.billing.current_plan(u.id)
        data["kb_user"], data["kb_plan"] = u, plan
        text = event.text if isinstance(event, Message) else None
        cb = event.data if isinstance(event, CallbackQuery) else None
        from app.telegram.bot import B_FILTERS, B_SCAN

        if text in (B_SCAN, "/scan") or (text or "").startswith("/scan@"):
            q = await service.billing.consume(u.id, "scan")
            if not q.allowed:
                await event.answer(f"⏳ Лимит сканирований тарифа {plan.title} на сегодня исчерпан "
                                   f"({q.used}/{q.limit}). Счётчик обнуляется в 00:00 по Алматы. Тарифы — /plan")
                return None
        if not plan.filters and (text == B_FILTERS or (cb or "").startswith(("fm:", "fp:", "fc:", "fr:", "fl:"))):
            msg = f"🎯 Свои фильтры букмекеров и видов спорта доступны с тарифа BASIC. Ваш тариф: {plan.title}. /plan"
            await (event.answer(msg, show_alert=True) if isinstance(event, CallbackQuery) else event.answer(msg))
            return None
        if not plan.details and (cb or "").startswith("det:"):
            await event.answer("📊 Подробный расчёт доступен на тарифах PRO и VIP. /plan", show_alert=True)
            return None
        return await handler(event, data)


# ---------------------------------------------------------------- plans & subscription
def plan_line(p) -> str:
    price = "бесплатно" if p.price_stars == 0 else f"{p.price_stars} ⭐ / {p.period_days} дн. (≈ {p.price_kzt:,} ₸)"
    feats = [f"сканирований в день: {p.scans_per_day}", f"уведомлений в день: {p.notifications_per_day}",
             f"история: {p.history_days} дн."]
    if p.filters:
        feats.append("свои фильтры букмекеров и видов спорта")
    if p.details:
        feats.append("подробный расчёт и статистика")
    return f"<b>{escape(p.title)}</b> — {price}\n  • " + "\n  • ".join(feats)


@router.message(Command("plan"))
async def plan_cmd(m: Message, service, kb_plan=None) -> None:
    plans = await service.billing.plans()
    head = f"💳 <b>Тарифы</b> (ваш: <b>{kb_plan.title if kb_plan else 'FREE'}</b>)\n\n"
    note = ("\n\nKirbet — информационный сервис: он не делает ставок и не гарантирует прибыль. "
            "Риски — /terms")
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text=f"Оформить {p.title}", callback_data=f"buy:{p.code}")]
                                               for p in plans if p.price_stars > 0])
    await m.answer(head + "\n\n".join(plan_line(p) for p in plans) + note, reply_markup=kb)


@router.message(Command("subscribe"))
async def subscribe_cmd(m: Message, service, kb_plan=None) -> None:
    await plan_cmd(m, service, kb_plan)


@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery, service, bot: Bot) -> None:
    code = cb.data.split(":", 1)[1]
    s = get_settings()
    if not s.payments_enabled:
        await cb.answer("Оплата ещё не включена владельцем сервиса. Следите за новостями в /support.",
                        show_alert=True)
        return
    try:
        pay = await service.billing.create_payment(cb.from_user.id, code, s.payments_provider)
        plan = await service.billing.plan(code)
    except BillingError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer()
    await bot.send_invoice(
        chat_id=cb.from_user.id, title=f"Kirbet {plan.title} — {plan.period_days} дней",
        description="Доступ к аналитике Kirbet по тарифу. Сервис не делает ставок и не гарантирует прибыль.",
        payload=pay.invoice_payload, currency="XTR", prices=[LabeledPrice(label=plan.title, amount=pay.amount)],
        provider_token="",  # Telegram Stars
    )


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery, service) -> None:
    if not get_settings().payments_enabled:
        await q.answer(ok=False, error_message="Оплата отключена")
        return
    err = await service.billing.validate_checkout(q.invoice_payload, q.from_user.id, q.total_amount, q.currency)
    await q.answer(ok=err is None, error_message=err)


@router.message(F.successful_payment)
async def paid(m: Message, service) -> None:
    sp = m.successful_payment
    try:
        sub, created = await service.billing.confirm_payment(
            provider=get_settings().payments_provider, payload=sp.invoice_payload,
            charge_id=sp.telegram_payment_charge_id, telegram_id=m.from_user.id, amount=sp.total_amount,
            currency=sp.currency, raw={"provider_charge_id": sp.provider_payment_charge_id})
    except BillingError as e:
        log.error("payment not applied for %s: %s", m.from_user.id, e)
        await m.answer(f"⚠️ Платёж получен, но не применён: {escape(str(e))}. Напишите в /support — разберёмся.")
        return
    if created and sub:
        await m.answer(f"✅ Оплата подтверждена Telegram. Тариф <b>{sub.plan_code.upper()}</b> активен до "
                       f"<b>{fmt_almaty(sub.ends_at, '%d.%m.%Y %H:%M')}</b> (Алматы).")


# ---------------------------------------------------------------- profile / balance
async def _profile_text(service, u, plan) -> str:
    sub = await service.billing.active_subscription(u.id)
    use = await service.billing.usage_today(u.id)
    lines = [
        "👤 <b>Профиль</b>",
        f"Telegram ID: <code>{u.telegram_id}</code>",
        f"Зарегистрирован: {fmt_almaty(u.created_at, '%d.%m.%Y')}",
        f"Тариф: <b>{plan.title}</b>" + (f" до {fmt_almaty(sub.ends_at, '%d.%m.%Y %H:%M')} (Алматы)" if sub else ""),
        f"Сегодня: сканирований {use.get('scan', 0)}/{plan.scans_per_day}, "
        f"уведомлений {use.get('notify', 0)}/{plan.notifications_per_day}",
        f"Уведомления: {'🔔 включены' if u.settings and u.settings.notifications else '🔕 выключены'} (⚙️ Настройки)",
    ]
    pays = await service.billing.payments(u.telegram_id, limit=5)
    if pays:
        lines.append("\n<b>Платежи:</b>")
        lines += [f"• {fmt_almaty(p.created_at, '%d.%m %H:%M')} {p.plan_code.upper()} {p.amount} {p.currency} — "
                  f"{p.status}" for p in pays]
    return "\n".join(lines)


@router.message(Command("profile"))
async def profile_cmd(m: Message, service, kb_user=None, kb_plan=None) -> None:
    await m.answer(await _profile_text(service, kb_user, kb_plan))


@router.message(Command("balance"))
async def balance_cmd(m: Message, service, kb_user=None, kb_plan=None) -> None:
    sub = await service.billing.active_subscription(kb_user.id)
    if sub:
        await m.answer(f"💳 Тариф <b>{kb_plan.title}</b> активен до {fmt_almaty(sub.ends_at, '%d.%m.%Y %H:%M')} "
                       "(Алматы). Продление: /subscribe")
    else:
        await m.answer("💳 У вас бесплатный тариф FREE. Тарифы и оплата: /plan")


# ---------------------------------------------------------------- menu shortcuts
@router.message(Command("scan"))
async def scan_cmd(m: Message, service) -> None:
    from app.telegram.bot import do_scan

    await do_scan(m, service)


@router.message(Command("settings"))
async def settings_cmd(m: Message, service) -> None:
    from app.telegram.bot import settings_menu

    await settings_menu(m, service)


@router.message(Command("history"))
async def history_cmd(m: Message, service, kb_plan=None) -> None:
    from app.telegram.bot import history

    await history(m, service, days=kb_plan.history_days if kb_plan else 1)


@router.message(Command("stats"))
async def stats_cmd(m: Message, service) -> None:
    st = await service.repo.stats(24)
    await m.answer("📈 <b>Статистика за 24 ч</b>\n" + "\n".join(f"{escape(str(k))}: {escape(str(v))}"
                                                                   for k, v in st.items()))


@router.message(Command("sports"))
async def sports_cmd(m: Message, service) -> None:
    rep = service.scanner.last_report
    if not rep:
        await m.answer("Данных ещё нет — первый скан выполняется. Попробуйте через минуту.")
        return
    cats = sorted(rep.categories.items(), key=lambda kv: -kv[1])
    await m.answer("🏟 <b>Виды спорта в последнем скане</b> (рынков):\n" +
                   "\n".join(f"• {escape(c)}: {n}" for c, n in cats[:40]))


@router.message(Command("bookmakers"))
async def bookmakers_cmd(m: Message, service) -> None:
    from app.providers.registry import KZ_BOOKMAKERS
    from app.providers.status import public_status

    lines = ["🏢 <b>Букмекеры Казахстана (реестр ЕСУ)</b>\n"]
    for name in KZ_BOOKMAKERS:
        p = service.providers.get(name)
        if p is None:
            continue
        st, reason = public_status(p)
        upd = fmt_almaty(p.status.last_success, "%d.%m %H:%M") if p.status.last_success else "—"
        line = f"{st.icon} <b>{name.upper()}</b> — {st.value} · событий {p.status.events} · рынков {p.status.markets}" \
               f" · обновлено {upd}"
        if reason:
            line += f"\n   <i>{escape(reason[:200])}</i>"
        lines.append(line)
    await m.answer("\n".join(lines))


@router.message(Command("support"))
async def support_cmd(m: Message) -> None:
    c = get_settings().support_contact or "контакт поддержки будет опубликован владельцем сервиса"
    await m.answer(f"🛟 <b>Поддержка</b>\n{escape(c)}\n\nВаш Telegram ID для обращения: <code>{m.from_user.id}</code>")


@router.message(Command("terms"))
async def terms_cmd(m: Message) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[IB(text="Соглашение", callback_data="doc:terms"),
                                                IB(text="Риски", callback_data="doc:risks"),
                                                IB(text="Конфиденциальность", callback_data="doc:privacy")]])
    await m.answer("📄 Документы сервиса. Kirbet — аналитический сервис: ставки не размещает, прибыль не "
                   "гарантирует, вилки не являются безрисковыми.", reply_markup=kb)


@router.callback_query(F.data.startswith("doc:"))
async def doc_cb(cb: CallbackQuery) -> None:
    name = cb.data.split(":", 1)[1]
    if name not in ("terms", "risks", "privacy"):
        await cb.answer()
        return
    text = (DOCS / f"{name}.md").read_text(encoding="utf-8") if (DOCS / f"{name}.md").exists() else "—"
    await cb.answer()
    for i in range(0, len(text), 3800):
        await cb.message.answer(escape(text[i:i + 3800]))


# ---------------------------------------------------------------- admin
def _need(m: Message, sensitive: bool) -> str | None:
    if not is_admin(m.from_user.id):
        return "denied"
    if sensitive and not admin_authed(m.from_user.id):
        return "Нужна повторная авторизация: /auth <PIN> (действует 15 минут)."
    return None


@router.message(Command("auth"))
async def auth_cmd(m: Message, command: CommandObject, service) -> None:
    if not is_admin(m.from_user.id):
        return
    pin = get_settings().admin_pin
    try:
        await m.delete()  # do not leave the PIN in the chat history
    except Exception:  # noqa: BLE001
        pass
    if not pin:
        await m.answer("ADMIN_PIN не задан на сервере — чувствительные действия отключены.")
        return
    if hmac.compare_digest((command.args or "").strip(), pin):
        _admin_sessions[m.from_user.id] = time.monotonic()
        await service.repo.audit(m.from_user.id, "admin.auth", "ok")
        await m.answer("🔐 Авторизация на 15 минут.")
    else:
        await service.repo.audit(m.from_user.id, "admin.auth", "fail")
        await m.answer("Неверный PIN.")


@router.message(Command("admin"))
async def admin_cmd(m: Message, service) -> None:
    if _need(m, False):
        return
    from app.providers.browser.zendriver_client import ZendriverClient

    sm = await service.billing.summary()
    c = service.counters
    h = ZendriverClient.shared().health()
    import psutil

    vm = psutil.virtual_memory()
    proc = psutil.Process()
    await m.answer(
        "🛠 <b>Админ-панель</b>\n"
        f"Пользователей: {sm['users']} (заблокировано {sm['blocked']})\n"
        f"Активные подписки: {sm['active_by_plan'] or '—'}\n"
        f"Оплачено: {sm['paid_count']} платежей, {sm['paid_stars']} ⭐ · платежи "
        f"{'ВКЛЮЧЕНЫ' if get_settings().payments_enabled else 'выключены'}\n"
        f"Сканов: {c['scans']}, из кеша {c['cache_hits']}, ошибок {c['errors']}, уведомлений {c['notified']}\n"
        f"Последний скан: {fmt_almaty(service.last_success_at)} (Алматы)\n"
        f"Сервер: RAM {vm.used // 2**20}/{vm.total // 2**20} МБ, бот {proc.memory_info().rss // 2**20} МБ, "
        f"CPU {psutil.cpu_percent(interval=None)}% · Chrome {h['rss_mb']} МБ\n\n"
        "Команды: /users · /user ID · /payments · /errors · /sources · /scans\n"
        "С /auth PIN: /grant ID PLAN DAYS · /cancel ID · /block ID · /unblock ID · "
        "/setplan CODE FIELD VALUE · /refund CHARGE_ID · /source NAME on|off")


@router.message(Command("users"))
async def users_cmd(m: Message, service) -> None:
    if _need(m, False):
        return
    rows = await service.repo.list_users(30)
    await m.answer("👥 <b>Последние пользователи</b>\n" + "\n".join(
        f"<code>{u.telegram_id}</code> @{escape(u.username or '—')} · с {fmt_almaty(u.created_at, '%d.%m')}"
        f"{' · ⛔' if u.is_blocked else ''}" for u in rows) or "—")


@router.message(Command("user"))
async def user_cmd(m: Message, command: CommandObject, service) -> None:
    if _need(m, False) or not (command.args or "").strip().isdigit():
        return
    tg = int(command.args.strip())
    u = await service.repo.get_user(tg)
    if not u:
        await m.answer("Не найден")
        return
    await m.answer(await _profile_text(service, u, await service.billing.current_plan(u.id)))


async def _admin_do(m: Message, service, sensitive: bool, fn) -> None:
    err = _need(m, sensitive)
    if err == "denied":
        return
    if err:
        await m.answer(err)
        return
    try:
        await m.answer(await fn())
    except (BillingError, ValueError, IndexError) as e:
        await m.answer(f"Ошибка: {escape(str(e))}")


@router.message(Command("grant"))
async def grant_cmd(m: Message, command: CommandObject, service) -> None:
    async def fn():
        tg, code, days = (command.args or "").split()
        sub = await service.billing.grant(int(tg), code.lower(), int(days), actor=m.from_user.id)
        return f"✅ {code.upper()} для {tg} до {fmt_almaty(sub.ends_at, '%d.%m.%Y %H:%M')}"
    await _admin_do(m, service, True, fn)


@router.message(Command("cancel"))
async def cancel_cmd(m: Message, command: CommandObject, service) -> None:
    async def fn():
        n = await service.billing.cancel(int((command.args or "").strip()), actor=m.from_user.id)
        return f"Отменено подписок: {n}"
    await _admin_do(m, service, True, fn)


@router.message(Command("block", "unblock"))
async def block_cmd(m: Message, command: CommandObject, service) -> None:
    async def fn():
        tg = int((command.args or "").strip())
        blocked = command.command == "block"
        await service.billing.set_blocked(tg, blocked, actor=m.from_user.id)
        return f"{'⛔ Заблокирован' if blocked else '✅ Разблокирован'} {tg}"
    await _admin_do(m, service, True, fn)


@router.message(Command("setplan"))
async def setplan_cmd(m: Message, command: CommandObject, service) -> None:
    async def fn():
        code, field, value = (command.args or "").split(maxsplit=2)
        p = await service.billing.set_plan_field(code.lower(), field, value, actor=m.from_user.id)
        return "✅ " + plan_line(p)
    await _admin_do(m, service, True, fn)


@router.message(Command("refund"))
async def refund_cmd(m: Message, command: CommandObject, service, bot: Bot) -> None:
    async def fn():
        charge = (command.args or "").strip()
        pays = [p for p in await service.billing.payments(limit=500) if p.provider_charge_id == charge]
        if not pays:
            raise BillingError("платёж не найден")
        u = await service.repo.get_user_by_id(pays[0].user_id)
        await bot.refund_star_payment(user_id=u.telegram_id, telegram_payment_charge_id=charge)  # real refund
        await service.billing.mark_refunded(charge, pays[0].provider, actor=m.from_user.id)
        return f"↩️ Возврат выполнен: {charge}"
    await _admin_do(m, service, True, fn)


@router.message(Command("payments"))
async def payments_cmd(m: Message, service) -> None:
    if _need(m, False):
        return
    pays = await service.billing.payments(limit=20)
    await m.answer("💳 <b>Платежи</b>\n" + ("\n".join(
        f"#{p.id} {fmt_almaty(p.created_at, '%d.%m %H:%M')} user#{p.user_id} {p.plan_code} {p.amount} {p.currency} "
        f"{p.status} {escape(p.provider_charge_id or '')}" for p in pays) or "—"))


@router.message(Command("errors"))
async def errors_cmd(m: Message, service) -> None:
    if _need(m, False):
        return
    from app.providers.status import public_status

    lines = ["🧯 <b>Источники с проблемами</b>"]
    for p in service.providers.values():
        st, reason = public_status(p)
        if st.value != "WORKING":
            lines.append(f"{st.icon} {p.name}: {st.value} — {escape((reason or p.status.last_error)[:180])}")
    await m.answer("\n".join(lines))


@router.message(Command("sources"))
async def sources_cmd(m: Message, service) -> None:
    if _need(m, False):
        return
    from app.providers.status import public_status

    rows = []
    for p in service.providers.values():
        st, _ = public_status(p)
        lat = f"{p.status.latency_ms:.0f} мс" if p.status.latency_ms else "—"
        rows.append(f"{st.icon} {p.name}: {st.value} · событий {p.status.events} · рынков {p.status.markets} · "
                    f"запросов {p.status.requests} · ошибок {p.status.errors} · {lat}")
    await m.answer("🔌 <b>Источники</b>\n" + "\n".join(rows))


@router.message(Command("source"))
async def source_cmd(m: Message, command: CommandObject, service) -> None:
    async def fn():
        name, flag = (command.args or "").split()
        if name not in service.providers:
            raise ValueError(f"нет источника {name}")
        await service.set_source_enabled(name, flag.lower() in ("on", "1", "true"), actor=m.from_user.id)
        return f"Источник {name}: {'включён' if flag.lower() in ('on', '1', 'true') else 'DISABLED'}"
    await _admin_do(m, service, True, fn)


@router.message(Command("scans"))
async def scans_cmd(m: Message, service) -> None:
    if _need(m, False):
        return
    rows = await service.repo.recent_scans(10)
    await m.answer("🧾 <b>Последние сканы</b>\n" + "\n".join(
        f"{fmt_almaty(r.started_at, '%d.%m %H:%M:%S')} · {r.duration_s:.0f} с · рынков {r.markets} · групп "
        f"{r.cross_groups} · кандидатов {r.candidates} · подтверждено {r.verified}" for r in rows) or "—")


def doc_path(name: str) -> Path:
    return DOCS / f"{name}.md"
