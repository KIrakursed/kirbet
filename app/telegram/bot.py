"""Telegram interface (aiogram 3) — simple Russian UI in tenge."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.calculator.calc import CalcError, calculate
from app.config import get_settings
from app.database.repo import CATEGORY_GROUPS, PLATFORM_GROUPS, user_groups
from app.presentation.messages import GLOSSARY, details_card, simple_card
from app.presentation.plan import PRESETS_KZT, Color, build_plan, fmt_kzt, fmt_pct, ptitle
from app.timeutil import fmt_almaty

log = logging.getLogger("bot")
router = Router()
IB = InlineKeyboardButton

B_OVERVIEW, B_SCAN, B_ARBS, B_CALC = "📊 Обзор рынка", "🔎 Сканировать", "💎 Вилки сейчас", "💰 Калькулятор"
B_FILTERS, B_PAPER, B_HIST, B_PLAT = "🎯 Фильтры", "📒 Paper Trading", "📚 История", "🌐 Площадки"
B_SET, B_HELP = "⚙️ Настройки", "❓ Как это работает"
B_MANUAL = "✍️ Ввести коэффициенты"
ROI_PRESETS = [("0.5", "от 0.5%"), ("1", "от 1%"), ("2", "от 2%"), ("3", "от 3%"), ("5", "от 5%"), ("0", "любая")]
LIQ_PRESETS = [0, 10_000, 50_000, 100_000, 500_000]
STATE_RU = {"ok": "🟢 работает", "degraded": "🟡 частично", "down": "🔴 не отвечает", "needs_key": "🔑 нужен ключ",
            "unavailable": "⛔ нет доступа", "disabled": "⚪ выключен"}


def main_kb() -> ReplyKeyboardMarkup:
    rows = [[B_OVERVIEW, B_SCAN], [B_ARBS, B_MANUAL], [B_CALC, B_FILTERS], [B_PAPER, B_HIST],
            [B_PLAT, B_SET], [B_HELP]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


class Calc(StatesGroup):
    capital = State()
    lines = State()


class CustomAmount(StatesGroup):
    value = State()


class ManualOdds(StatesGroup):
    text = State()


SHARED_SCAN_SECONDS = 90  # public bot: a scan younger than this is reused for every user


class AccessMiddleware(BaseMiddleware):
    """If TELEGRAM_ADMIN_IDS is set, only those users may use the bot; empty = public bot (anyone).

    Always: the update must come from a real Telegram user (positive id, not a bot), private chats only."""

    async def __call__(self, handler, event, data):
        allowed = {x.strip() for x in get_settings().telegram_admin_ids.split(",") if x.strip()}
        user = data.get("event_from_user")
        if user is None or user.is_bot or not isinstance(user.id, int) or user.id <= 0:
            return None
        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            return None
        if allowed and str(user.id) not in allowed:
            return None
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Per-user rate limit for a public bot (sliding windows, in memory).

    * at most ``burst`` updates per ``burst_window`` seconds and ``per_minute`` per minute;
    * the user is told once per window, extra updates are dropped silently;
    * ``SCAN_COOLDOWN`` seconds between two «Сканировать» presses of the same user.
    """

    def __init__(self, burst: int = 4, burst_window: float = 3.0, per_minute: int = 30):
        self.burst, self.burst_window, self.per_minute = burst, burst_window, per_minute
        self.hits: dict[int, deque] = defaultdict(deque)
        self.warned: dict[int, float] = {}
        self.last_scan: dict[int, float] = {}
        self.dropped = 0

    def allow(self, uid: int, now: float) -> bool:
        q = self.hits[uid]
        while q and now - q[0] > 60:
            q.popleft()
        recent = sum(1 for t in q if now - t <= self.burst_window)
        if recent >= self.burst or len(q) >= self.per_minute:
            return False
        q.append(now)
        return True

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return None
        now = time.monotonic()
        if not self.allow(user.id, now):
            self.dropped += 1
            if now - self.warned.get(user.id, 0) > 10:
                self.warned[user.id] = now
                msg = "⏳ Слишком часто. Подождите несколько секунд."
                if isinstance(event, CallbackQuery):
                    await event.answer(msg, show_alert=False)
                elif isinstance(event, Message):
                    await event.answer(msg)
            return None
        if isinstance(event, Message) and event.text == B_SCAN:
            left = SCAN_COOLDOWN - (now - self.last_scan.get(user.id, -1e9))
            if left > 0:
                await event.answer(f"⏳ Новое сканирование можно запустить через {int(left) + 1} с. "
                                   "Последние результаты — в «💎 Вилки сейчас».")
                return None
            self.last_scan[user.id] = now
        if len(self.hits) > 50_000:  # bound memory on a very busy bot
            for uid in [u for u, q in self.hits.items() if not q or now - q[-1] > 60]:
                self.hits.pop(uid, None)
        return await handler(event, data)


SCAN_COOLDOWN = 30
THROTTLE = ThrottleMiddleware()


def build_dispatcher(service) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.message.middleware(THROTTLE)
    dp.callback_query.middleware(THROTTLE)
    from app.telegram.commerce import PlanMiddleware
    from app.telegram.commerce import router as commerce_router

    dp.message.middleware(PlanMiddleware())
    dp.callback_query.middleware(PlanMiddleware())
    dp.pre_checkout_query.middleware(AccessMiddleware())
    dp.include_router(commerce_router)  # /plan /subscribe /profile ... /admin, payments (before menu router)
    dp["service"] = service
    dp.include_router(router)
    return dp


async def run_bot(service) -> None:
    bot = Bot(get_settings().telegram_bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    service.bot = bot
    from app.telegram.commerce import set_commands

    await set_commands(bot)
    await build_dispatcher(service).start_polling(bot)


# ------------------------------------------------------------------ cards
def card_kb(sig: str, plan, kzt: Decimal) -> InlineKeyboardMarkup:
    rows, seen = [], set()
    for lp in plan.legs:
        if lp.url and lp.url.startswith("http") and lp.platform not in seen:
            seen.add(lp.platform)
            rows.append([IB(text=f"🔗 Открыть {lp.title}", url=lp.url)])
    k = int(kzt)
    rows.append([IB(text="🧮 Изменить сумму", callback_data=f"amt:{sig}"),
                 IB(text="📊 Подробнее", callback_data=f"det:{sig}:{k}")])
    rows.append([IB(text="📝 В Paper Trading", callback_data=f"pt:{sig}:{k}"),
                 IB(text="❌ Скрыть", callback_data="hide")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def amount_kb(sig: str) -> InlineKeyboardMarkup:
    pr = [IB(text=fmt_kzt(Decimal(v)), callback_data=f"cap:{sig}:{v}") for v in PRESETS_KZT]
    return InlineKeyboardMarkup(inline_keyboard=[pr[:3], pr[3:], [IB(text="✏️ Своя сумма", callback_data=f"own:{sig}")]])


async def send_card(target: Message, service, opp, kzt: Decimal, *, edit: bool = False) -> None:
    plan = build_plan(opp, service.fx, kzt)
    if plan is None:
        text = "⚪ По текущим ценам эту сумму разместить нельзя (не хватает объёма или ниже минимальной ставки)."
        await (target.edit_text(text) if edit else target.answer(text))
        return
    text, kb = simple_card(opp, plan), card_kb(opp.signature, plan, kzt)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def _user(service, tg_user):
    return await service.repo.get_or_create_user(tg_user.id, tg_user.username)


# ------------------------------------------------------------------ start / help
@router.message(CommandStart())
async def start(m: Message, service) -> None:
    await _user(service, m.from_user)
    await m.answer(
        "👋 Я ищу <b>вилки</b> — ситуации, когда можно поставить на все исходы события на разных площадках "
        "и при любом результате получить обратно больше, чем вложили.\n\n"
        "Я сам считаю, <b>сколько и куда</b> поставить, сколько вернётся и сколько чистой прибыли. "
        "Ставки я не делаю — только считаю и проверяю.\n\n"
        "Начните с «📊 Обзор рынка» или «🔎 Сканировать». Свою сумму задайте в «⚙️ Настройки».",
        reply_markup=main_kb())


@router.message(Command("menu"))
async def menu(m: Message) -> None:
    await m.answer("Главное меню", reply_markup=main_kb())


@router.message(F.text == B_HELP)
async def help_(m: Message) -> None:
    await m.answer(
        "<b>Как это работает</b>\n"
        "1. Каждые 1–2 минуты я собираю цены с площадок.\n"
        "2. Нахожу одно и то же событие на разных площадках.\n"
        "3. Если по лучшим ценам можно покрыть все исходы с прибылью — считаю ставки под вашу сумму "
        "с учётом комиссий, доступного объёма и округления.\n"
        "4. Перед уведомлением заново загружаю цены и пересчитываю.\n\n"
        "🟢 проверено, прибыль положительная · 🟡 есть ограничения или риск · "
        "🔴 исчезла/убыток · ⚪ мало данных\n\n" + GLOSSARY)


# ------------------------------------------------------------------ overview / lists
async def overview_text(service, u) -> tuple[str, InlineKeyboardMarkup]:
    ov = await service.repo.overview(24)
    working, total = service.sources_working()
    platforms = len(service.available_platforms())
    text = (
        "📊 <b>ОБЗОР РЫНКА</b> (за 24 часа)\n\n"
        f"Баланс для расчётов: <b>{fmt_kzt(u.settings.capital_kzt)}</b>\n\n"
        f"Найдено возможностей: <b>{ov['found']}</b>\n"
        f"Прошли повторную проверку: <b>{ov['verified']}</b>\n"
        f"Исчезли / не подтвердились: <b>{ov['expired']}</b>\n"
        f"Активных прямо сейчас: <b>{len(service.current_opps())}</b>\n"
        f"Самая высокая расчётная доходность: <b>{fmt_pct(ov['best_roi']) if ov['best_roi'] is not None else '—'}</b>\n\n"
        f"Доступные площадки: <b>{platforms}</b>\n"
        f"Работающие источники данных: <b>{working}/{total}</b>"
    )
    if service.scanner.last_report:
        text += f"\nПоследнее сканирование: {fmt_almaty(service.scanner.last_report.started, '%H:%M:%S')+' (Алматы)'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🆕 Новые возможности", callback_data="ov:new")],
        [IB(text="🏆 Самая высокая прибыль", callback_data="ov:roi"),
         IB(text="💧 Больше всего объёма", callback_data="ov:liq")],
        [IB(text="💨 Недавно исчезнувшие", callback_data="ov:gone"),
         IB(text="📚 История расчётов", callback_data="ov:hist")],
    ])
    return text, kb


@router.message(F.text == B_OVERVIEW)
async def overview(m: Message, service) -> None:
    u = await _user(service, m.from_user)
    text, kb = await overview_text(service, u)
    await m.answer(text, reply_markup=kb)


def _list_kb(items) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text=t[:60], callback_data=f"o:{sig}")] for sig, t in items])


@router.callback_query(F.data.startswith("ov:"))
async def overview_section(cb: CallbackQuery, service) -> None:
    what = cb.data.split(":", 1)[1]
    u = await _user(service, cb.from_user)
    kzt = u.settings.capital_kzt
    await cb.answer()
    if what == "hist":
        await history(cb.message, service, cb.from_user)
        return
    if what == "gone":
        gone = service.gone[-10:][::-1]
        if not gone:
            await cb.message.answer("Недавно исчезнувших возможностей нет.")
            return
        lines = ["💨 <b>Недавно исчезнувшие</b>"]
        for when, title, roi, reason in gone:
            lines.append(f"🔴 {fmt_almaty(when, '%H:%M')} · {escape(title[:50])} · было {fmt_pct(roi)}\n   {escape(reason[:120])}")
        await cb.message.answer("\n".join(lines))
        return
    opps = service.filter_for_user(service.current_opps(), u.settings)
    if not opps:
        await cb.message.answer("Сейчас подходящих проверенных возможностей нет. Это нормально: "
                                "вилки редки и живут от секунд до минут. Я пришлю уведомление, когда найду.")
        return
    plans = [(o, build_plan(o, service.fx, kzt)) for o in opps]
    plans = [(o, p) for o, p in plans if p is not None]
    if what == "new":
        plans.sort(key=lambda x: x[0].verified_at or x[0].created_at, reverse=True)
    elif what == "roi":
        plans.sort(key=lambda x: x[1].roi_pct, reverse=True)
    elif what == "liq":
        plans.sort(key=lambda x: service.max_volume_kzt(x[0]), reverse=True)
    items = [(o.signature, f"{p.color.value} {fmt_pct(p.roi_pct)} · {fmt_kzt(p.profit_kzt, sign=True)} · {o.title}")
             for o, p in plans[:10]]
    await cb.message.answer("Нажмите, чтобы открыть расчёт:", reply_markup=_list_kb(items))


@router.message(F.text == B_ARBS)
async def arbs_now(m: Message, service) -> None:
    u = await _user(service, m.from_user)
    opps = service.filter_for_user(service.current_opps(), u.settings)
    if not opps:
        await m.answer("Сейчас подходящих проверенных вилок нет.\n"
                       "Проверьте «🎯 Фильтры» или нажмите «🔎 Сканировать». Уведомление придёт само.")
        return
    for o in opps[:5]:
        await send_card(m, service, o, u.settings.capital_kzt)


@router.callback_query(F.data.startswith("o:"))
async def open_opp(cb: CallbackQuery, service) -> None:
    sig = cb.data.split(":", 1)[1]
    opp = service.live.get(sig)
    if opp is None:
        await cb.answer("Возможность устарела — нажмите «Сканировать»", show_alert=True)
        return
    u = await _user(service, cb.from_user)
    await cb.answer()
    await send_card(cb.message, service, opp, u.settings.capital_kzt)


# ------------------------------------------------------------------ scan
@router.message(F.text == B_SCAN)
async def do_scan(m: Message, service) -> None:
    u = await _user(service, m.from_user)
    msg = await m.answer("⏳ Собираю цены со всех площадок и проверяю… обычно 30–90 секунд.")
    try:
        rep, cached = await service.scan_shared(max_age=SHARED_SCAN_SECONDS)
    except Exception:  # noqa: BLE001
        log.exception("scan failed for user %s", m.from_user.id)
        last = service.last_success_at
        await msg.edit_text("⚠️ Сканирование не удалось. Свежих данных нет"
                            + (f"; последнее успешное обновление было в {fmt_almaty(last, '%H:%M:%S')} (Алматы) — "
                               "эти цены могли устареть." if last else "."))
        return
    found = service.filter_for_user(rep.verified, u.settings)
    when = fmt_almaty(service.last_success_at, "%H:%M:%S")
    src = f"общий результат из кеша, обновлён в {when} (Алматы)" if cached else f"обновлено в {when} (Алматы)"
    await msg.edit_text(
        f"✅ Готово ({src}), скан занял {rep.duration_s:.0f} с. Проверено рынков: {rep.total_markets}, "
        f"одинаковых событий на разных площадках: {rep.groups_cross}.\n"
        f"Проверенных вилок: <b>{len(rep.verified)}</b>, из них под ваши фильтры: <b>{len(found)}</b>.")
    for o in found[:5]:
        await send_card(m, service, o, u.settings.capital_kzt)


# ------------------------------------------------------------------ card buttons
@router.callback_query(F.data == "hide")
async def hide(cb: CallbackQuery) -> None:
    await cb.answer("Скрыто")
    try:
        await cb.message.delete()
    except Exception:  # noqa: BLE001
        await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("amt:"))
async def change_amount(cb: CallbackQuery) -> None:
    sig = cb.data.split(":", 1)[1]
    await cb.answer()
    await cb.message.answer("Выберите сумму — я пересчитаю ставки:", reply_markup=amount_kb(sig))


@router.callback_query(F.data.startswith("cap:"))
async def set_amount(cb: CallbackQuery, service) -> None:
    _, sig, v = cb.data.split(":")
    opp = service.live.get(sig)
    if opp is None:
        await cb.answer("Возможность устарела — нажмите «Сканировать»", show_alert=True)
        return
    await cb.answer("Пересчитываю…")
    await send_card(cb.message, service, opp, Decimal(v), edit=True)


@router.callback_query(F.data.startswith("own:"))
async def own_amount(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CustomAmount.value)
    await state.update_data(sig=cb.data.split(":", 1)[1])
    await cb.answer()
    await cb.message.answer("Введите сумму в тенге (например 75000):")


@router.message(CustomAmount.value)
async def own_amount_value(m: Message, state: FSMContext, service) -> None:
    v = parse_kzt(m.text)
    if v is None:
        await m.answer("Нужно число от 1 000 до 100 000 000. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    await state.clear()
    sig = data.get("sig")
    if sig == "__settings__":
        await service.repo.update_settings(m.from_user.id, capital_kzt=v)
        await m.answer(f"Баланс для расчётов: {fmt_kzt(v)}")
        return
    opp = service.live.get(sig)
    if opp is None:
        await m.answer("Возможность устарела — нажмите «Сканировать».")
        return
    await send_card(m, service, opp, v)


def parse_kzt(text: str | None) -> Decimal | None:
    try:
        v = Decimal((text or "").replace(" ", "").replace("₸", "").replace(",", "."))
    except InvalidOperation:
        return None
    if not (Decimal(1000) <= v <= Decimal(100_000_000)):
        return None
    return v.quantize(Decimal(1))


@router.callback_query(F.data.startswith("det:"))
async def details(cb: CallbackQuery, service) -> None:
    _, sig, v = cb.data.split(":")
    opp = service.live.get(sig)
    if opp is None:
        await cb.answer("Возможность устарела — нажмите «Сканировать»", show_alert=True)
        return
    plan = build_plan(opp, service.fx, Decimal(v), with_max_volume=True)
    await cb.answer()
    if plan is None:
        await cb.message.answer("По текущим ценам расчёт невозможен.")
        return
    text = details_card(opp, plan)
    for i in range(0, len(text), 3900):
        await cb.message.answer(text[i:i + 3900])


@router.callback_query(F.data.startswith("pt:"))
async def paper_add(cb: CallbackQuery, service) -> None:
    _, sig, v = cb.data.split(":")
    opp = service.live.get(sig)
    if opp is None:
        await cb.answer("Возможность устарела", show_alert=True)
        return
    u = await _user(service, cb.from_user)
    plan = build_plan(opp, service.fx, Decimal(v))
    if plan is None:
        await cb.answer("Расчёт невозможен", show_alert=True)
        return
    tid = await service.repo.paper_add(u.id, opp, plan, opp.db_id)
    await cb.answer("Добавлено в Paper Trading (симуляция)")
    await cb.message.answer(
        f"📝 Симуляция №{tid}: {escape(opp.title)}\nВложено (виртуально): {fmt_kzt(plan.total_kzt)} · "
        f"расчётная прибыль {fmt_kzt(plan.profit_kzt, sign=True)}.\n"
        "Это <b>не реальные деньги</b>. Если вы действительно сделали обе ставки — подтвердите в «📒 Paper Trading».")


# ------------------------------------------------------------------ paper trading
@router.message(F.text == B_PAPER)
async def paper(m: Message, service) -> None:
    u = await _user(service, m.from_user)
    s = await service.repo.paper_summary(u.id)
    rows = await service.repo.paper_list(u.id, 10)
    text = (
        "📒 <b>PAPER TRADING</b> — симуляция без реальных денег\n\n"
        f"Симуляций: {s['count']} · подтверждено вами: {s['confirmed']}\n"
        f"Виртуально «вложено»: {fmt_kzt(s['invested'])}\n\n"
        f"Теоретическая прибыль (лучшие цены без комиссий): {fmt_kzt(s['theoretical'], sign=True)}\n"
        f"Смоделированная прибыль (комиссии, объём, округление): {fmt_kzt(s['simulated'], sign=True)}\n"
        f"Подтверждённая вами (обе ставки исполнены): <b>{fmt_kzt(s['confirmed_profit'], sign=True)}</b>\n\n"
        "<i>Симуляция — не заработок. Подтверждайте только если реально поставили на всех площадках.</i>"
    )
    kb_rows = []
    for r in rows:
        mark = {"simulated": "📝", "confirmed": "✅", "cancelled": "🗑"}.get(r.status, "•")
        text += f"\n{mark} №{r.id} {escape(r.title[:40])} · {fmt_kzt(r.simulated_profit_kzt, sign=True)}"
        if r.status == "simulated":
            kb_rows.append([IB(text=f"✅ №{r.id}: обе ставки сделаны", callback_data=f"ptc:{r.id}"),
                            IB(text="🗑", callback_data=f"ptx:{r.id}")])
    await m.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None)


@router.callback_query(F.data.startswith(("ptc:", "ptx:")))
async def paper_status(cb: CallbackQuery, service) -> None:
    kind, tid = cb.data.split(":")
    u = await _user(service, cb.from_user)
    row = await service.repo.paper_set(u.id, int(tid), "confirmed" if kind == "ptc" else "cancelled")
    await cb.answer("Готово" if row else "Не найдено")
    if row:
        await cb.message.answer(f"№{row.id}: {'подтверждено ✅' if row.status == 'confirmed' else 'отменено 🗑'}")


# ------------------------------------------------------------------ filters
def filters_text(st) -> str:
    plats, cats = user_groups(st)
    return (
        "🎯 <b>Фильтры</b>\n\n"
        f"Площадки: {', '.join(PLATFORM_GROUPS[p] for p in plats if p in PLATFORM_GROUPS) or 'ничего не выбрано'}\n"
        f"События: {', '.join(CATEGORY_GROUPS[c] for c in cats if c in CATEGORY_GROUPS) or 'ничего не выбрано'}\n"
        f"Минимальная прибыль: {'любая' if Decimal(st.min_roi_pct) <= 0 else 'от ' + fmt_pct(st.min_roi_pct, sign=False)}\n"
        f"Минимальный доступный объём: {fmt_kzt(st.min_liquidity_kzt) if st.min_liquidity_kzt else 'любой'}"
    )


def filters_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [IB(text="🏦 Площадки", callback_data="fm:plat"), IB(text="🏆 События", callback_data="fm:cat")],
        [IB(text="📈 Мин. прибыль", callback_data="fm:roi"), IB(text="💧 Мин. объём", callback_data="fm:liq")],
    ])


def toggle_kb(prefix: str, options: dict, enabled: list[str]) -> InlineKeyboardMarkup:
    rows = [[IB(text=("✅ " if k in enabled else "▫️ ") + v, callback_data=f"{prefix}:{k}")] for k, v in options.items()]
    rows.append([IB(text="⬅️ Назад к фильтрам", callback_data="fm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == B_FILTERS)
async def filters(m: Message, service) -> None:
    u = await _user(service, m.from_user)
    await m.answer(filters_text(u.settings), reply_markup=filters_kb())


@router.callback_query(F.data.startswith("fm:"))
async def filter_menu(cb: CallbackQuery, service) -> None:
    what = cb.data.split(":", 1)[1]
    u = await _user(service, cb.from_user)
    plats, cats = user_groups(u.settings)
    await cb.answer()
    if what == "plat":
        await cb.message.edit_text("Где искать вилки:", reply_markup=toggle_kb("fp", PLATFORM_GROUPS, plats))
    elif what == "cat":
        await cb.message.edit_text("Какие события показывать:", reply_markup=toggle_kb("fc", CATEGORY_GROUPS, cats))
    elif what == "roi":
        kb = [[IB(text=t, callback_data=f"fr:{v}") for v, t in ROI_PRESETS[:3]],
              [IB(text=t, callback_data=f"fr:{v}") for v, t in ROI_PRESETS[3:]]]
        await cb.message.edit_text("Показывать вилки с прибылью:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    elif what == "liq":
        kb = [[IB(text=("любой" if v == 0 else "от " + fmt_kzt(Decimal(v))), callback_data=f"fl:{v}")] for v in LIQ_PRESETS]
        await cb.message.edit_text(
            "Минимальный объём, который реально можно поставить по показанным ценам:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        await cb.message.edit_text(filters_text(u.settings), reply_markup=filters_kb())


@router.callback_query(F.data.startswith(("fp:", "fc:")))
async def filter_toggle(cb: CallbackQuery, service) -> None:
    prefix, key = cb.data.split(":", 1)
    u = await _user(service, cb.from_user)
    plats, cats = user_groups(u.settings)
    cur = plats if prefix == "fp" else cats
    cur = [x for x in cur if x != key] if key in cur else cur + [key]
    field, opts = ("platform_groups", PLATFORM_GROUPS) if prefix == "fp" else ("categories", CATEGORY_GROUPS)
    await service.repo.update_settings(cb.from_user.id, **{field: cur})
    await cb.message.edit_reply_markup(reply_markup=toggle_kb(prefix, opts, cur))
    await cb.answer("Сохранено")


@router.callback_query(F.data.startswith(("fr:", "fl:")))
async def filter_value(cb: CallbackQuery, service) -> None:
    prefix, v = cb.data.split(":", 1)
    field = "min_roi_pct" if prefix == "fr" else "min_liquidity_kzt"
    st = await service.repo.update_settings(cb.from_user.id, **{field: Decimal(v)})
    await cb.answer("Сохранено")
    await cb.message.edit_text(filters_text(st), reply_markup=filters_kb())


# ------------------------------------------------------------------ settings
def settings_text(st) -> str:
    return ("⚙️ <b>Настройки</b>\n\n"
            f"Баланс для расчётов: <b>{fmt_kzt(st.capital_kzt)}</b>\n"
            f"Уведомления: {'🔔 включены' if st.notifications else '🔕 выключены'}\n"
            f"Не показывать цены старше: {st.max_quote_age_s} с")


def settings_kb(st) -> InlineKeyboardMarkup:
    caps = [IB(text=fmt_kzt(Decimal(v)), callback_data=f"sc:{v}") for v in PRESETS_KZT]
    return InlineKeyboardMarkup(inline_keyboard=[
        caps[:3], caps[3:], [IB(text="✏️ Своя сумма", callback_data="own:__settings__")],
        [IB(text=("🔕 Выключить" if st.notifications else "🔔 Включить") + " уведомления", callback_data="sn")],
        [IB(text=f"⏱ {s} с", callback_data=f"sa:{s}") for s in (10, 20, 60)],
    ])


@router.message(F.text == B_SET)
async def settings_menu(m: Message, service) -> None:
    u = await _user(service, m.from_user)
    await m.answer(settings_text(u.settings), reply_markup=settings_kb(u.settings))


@router.callback_query(F.data.startswith(("sc:", "sa:")) | (F.data == "sn"))
async def settings_set(cb: CallbackQuery, service) -> None:
    u = await _user(service, cb.from_user)
    if cb.data == "sn":
        st = await service.repo.update_settings(cb.from_user.id, notifications=not u.settings.notifications)
    elif cb.data.startswith("sc:"):
        st = await service.repo.update_settings(cb.from_user.id, capital_kzt=Decimal(cb.data[3:]))
    else:
        st = await service.repo.update_settings(cb.from_user.id, max_quote_age_s=int(cb.data[3:]))
    await cb.answer("Сохранено")
    await cb.message.edit_text(settings_text(st), reply_markup=settings_kb(st))


# ------------------------------------------------------------------ platforms
@router.message(F.text == B_PLAT)
async def platforms(m: Message, service) -> None:
    working, total = service.sources_working()
    lines = [f"🌐 <b>Площадки</b> — работают {working} из {total} источников данных\n"]
    for p in service.providers.values():
        s = p.status
        line = f"{STATE_RU.get(s.state.value, s.state.value)} <b>{escape(ptitle(p.name))}</b>"
        if s.last_success:
            line += f" · обновлено {fmt_almaty(s.last_success, '%H:%M:%S')}, рынков {s.markets}"
        if s.state.value not in ("ok",) and s.message:
            line += f"\n   <i>{escape(s.message[:220])}</i>"
        lines.append(line)
    text = "\n".join(lines)
    for i in range(0, len(text), 3900):
        await m.answer(text[i:i + 3900])


# ------------------------------------------------------------------ history & stats
async def history(m: Message, service, tg_user=None, days: int = 7) -> None:
    tg_user = tg_user or m.from_user
    u = await _user(service, tg_user)
    ov = await service.repo.overview(24 * days)
    ps = await service.repo.paper_summary(u.id)
    rows = await service.repo.recent_opportunities(limit=15, since_hours=24 * days)
    lines = [
        f"📚 <b>История и статистика</b> ({days} дн., по вашему тарифу)\n",
        f"Найдено возможностей: {ov['found']}",
        f"Прошли повторную проверку: {ov['verified']}",
        f"Исчезли до исполнения / не подтвердились: {ov['expired']}",
        f"Средняя расчётная доходность: {fmt_pct(ov['avg_roi']) if ov['avg_roi'] is not None else '—'}",
        f"Максимальная расчётная доходность: {fmt_pct(ov['best_roi']) if ov['best_roi'] is not None else '—'}",
        f"Paper Trading: смоделировано {fmt_kzt(ps['simulated'], sign=True)}, подтверждено {fmt_kzt(ps['confirmed_profit'], sign=True)}",
        "",
        "<b>Последние расчёты:</b>",
    ]
    dot = {"VERIFIED": Color.GREEN, "NOTIFIED": Color.GREEN, "REJECTED": Color.RED, "EXPIRED": Color.RED,
           "INSUFFICIENT_LIQUIDITY": Color.RED, "SETTLEMENT_MISMATCH": Color.YELLOW}
    for r in rows:
        c = dot.get(r.status, Color.GREY).value
        roi = fmt_pct(r.roi_pct) if r.roi_pct is not None else "—"
        lines.append(f"{c} {fmt_almaty(r.created_at, '%d.%m %H:%M')} · {escape(r.title[:45])} · {roi}")
    await m.answer("\n".join(lines))


@router.message(F.text == B_HIST)
async def history_btn(m: Message, service, kb_plan=None) -> None:
    await history(m, service, days=kb_plan.history_days if kb_plan else 7)


# ------------------------------------------------------------------ calculator
@router.message(F.text == B_CALC)
async def calc_start(m: Message, state: FSMContext) -> None:
    await state.set_state(Calc.capital)
    await m.answer("💰 <b>Калькулятор</b>\nСколько хотите вложить всего? (в тенге, например 100000)")


@router.message(Calc.capital)
async def calc_capital(m: Message, state: FSMContext) -> None:
    v = parse_kzt(m.text)
    if v is None:
        await m.answer("Нужно число от 1 000 до 100 000 000.")
        return
    await state.update_data(capital=str(v))
    await state.set_state(Calc.lines)
    await m.answer(
        "Теперь коэффициенты всех исходов — по одному в строке. Например:\n"
        "<code>2.20\n2.15</code>\n\n"
        "Можно добавить лимит: <code>2.05 max=30000</code>\n"
        "Контракт Polymarket: <code>0.47 pm</code> (цена в $), Kalshi: <code>0.47 kalshi</code>\n"
        "Биржа: <code>back 2.2 c=0.05</code> или <code>lay 1.9 c=0.05</code>")


@router.message(Calc.lines)
async def calc_lines(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        out = calculate(Decimal(data["capital"]), (m.text or "").splitlines())
    except CalcError as e:
        await m.answer(f"Не понял: {escape(str(e))}. Попробуйте ещё раз.")
        return
    await state.clear()
    r = out.result
    if r is None:
        await m.answer("Не получилось распределить сумму: не хватает объёма или ставки меньше минимальных.")
        return
    total = sum((f.stake.quantize(Decimal(1)) for f in r.legs), Decimal(0))
    lines = ["💰 <b>Расчёт</b>\n"]
    for i, f in enumerate(r.legs, 1):
        q = f.leg.quote
        lines.append(f"{i}. Коэффициент/цена {q.best_price}: ставка <b>{fmt_kzt(f.stake)}</b> → вернётся {fmt_kzt(f.payout_if_win)}"
                     + (f" (комиссия {fmt_kzt(f.fee)})" if f.fee else ""))
    worst = min(f.payout_if_win.quantize(Decimal(1)) for f in r.legs) - total
    roi = worst / total * 100 if total else Decimal(0)
    lines += ["", f"Общая сумма ставок: {fmt_kzt(total)}",
              f"<b>Чистая прибыль: {fmt_kzt(worst, sign=True)}</b>",
              f"<b>Прибыль в процентах: {fmt_pct(roi)}</b>",
              "✅ Это вилка: прибыль при любом исходе" if worst > 0 else "❌ Вилки нет: при каком-то исходе будет убыток"]
    if out.max_payout_profitable > 0:
        lines.append(f"Максимальная выплата, при которой ещё есть прибыль: {fmt_kzt(out.max_payout_profitable)}")
    lines += ["", "<b>Риски:</b>"] + [f"• {x}" for x in out.risks]
    await m.answer("\n".join(lines))


# ------------------------------------------------------------------ manual odds entry
@router.message(F.text == B_MANUAL)
async def manual_start(m: Message) -> None:
    from app.manual.entry import MANUAL_BOOKMAKERS

    btns = [IB(text=t, callback_data=f"mb:{k}") for k, t in MANUAL_BOOKMAKERS.items()]
    await m.answer(
        "✍️ <b>Ввод коэффициентов вручную</b>\n"
        "Подходит для контор, цены которых бот не может получать сам. "
        "Я найду этот матч у 1XBET, OINABET, UBET, Polymarket и Kalshi и посчитаю вилку.\n\n"
        "Выберите контору:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)]))


@router.callback_query(F.data.startswith("mb:"))
async def manual_bookmaker(cb: CallbackQuery, state: FSMContext) -> None:
    from app.manual.entry import MANUAL_BOOKMAKERS

    bk = cb.data.split(":", 1)[1]
    await state.set_state(ManualOdds.text)
    await state.update_data(bk=bk)
    await cb.answer()
    await cb.message.answer(
        f"<b>{escape(MANUAL_BOOKMAKERS.get(bk, bk))}</b>: напишите матч и коэффициенты одной строкой.\n\n"
        "Футбол (П1, Ничья, П2 — основное время):\n<code>Ростов — Крылья Советов 1.92 3.62 4.23</code>\n"
        "Теннис / киберспорт / волейбол (победа в матче):\n<code>Медведев — Зверев 1.60 2.40</code>\n\n"
        "Коэффициенты — в том же порядке, что и команды.")


def manual_no_arb_text(res, bk_title: str, head: str) -> str:
    blocked = [o for o in res.opportunities if o.status.value == "SETTLEMENT_MISMATCH"]
    lines = [head, "", "❌ <b>Вилки нет</b> при этих коэффициентах."]
    if blocked:
        lines.append("Есть комбинация с прибылью, но правила расчёта площадок несовместимы: "
                     + escape(blocked[0].blocking[0][:150]))
    if res.best_s is not None and res.best_s > 1:
        lines.append(f"Не хватает примерно {((res.best_s - 1) * 100).quantize(Decimal('0.01'))}% до вилки.")
    need = [(lbl, cur, nd) for lbl, cur, nd in res.thresholds if nd is not None]
    if need:
        lines += ["", f"<b>Вилка появится, если у {escape(bk_title)}:</b>"]
        for lbl, cur, nd in need:
            name = "Ничья" if lbl == "Draw" else f"Победа {lbl}"
            lines.append(f"• {escape(name)}: коэффициент выше <b>{nd}</b> (сейчас {cur})")
        lines.append("Когда коэффициенты изменятся — снова нажмите «✍️ Ввести коэффициенты».")
    return "\n".join(lines)


@router.message(ManualOdds.text)
async def manual_text(m: Message, state: FSMContext, service) -> None:
    from app.manual.entry import MANUAL_BOOKMAKERS, ManualError, evaluate_manual, parse_manual
    from app.presentation.messages import local_time

    data = await state.get_data()
    bk = data.get("bk", "manual")
    bk_title = MANUAL_BOOKMAKERS.get(bk, bk)
    try:
        inp = parse_manual(m.text)
    except ManualError as e:
        await m.answer(f"Не понял: {escape(str(e))}.\nПример: <code>Ростов — Крылья Советов 1.92 3.62 4.23</code>")
        return
    await state.clear()
    if not service.scanner.last_markets or service.fx is None:
        await m.answer("⏳ Цены ещё загружаются (первое сканирование после запуска). Попробуйте через пару минут.")
        return
    u = await _user(service, m.from_user)
    wait = await m.answer("🔎 Ищу матч на других площадках и обновляю их цены…")
    capital_usd = Decimal(u.settings.capital_kzt) * service.fx.rates["KZT"]
    res = await evaluate_manual(providers=service.providers, markets=service.scanner.last_markets, bk=bk, inp=inp,
                                fx=service.fx, capital_usd=capital_usd)
    if res is None:
        await wait.edit_text(
            f"Не нашёл «{escape(inp.team_a)} — {escape(inp.team_b)}» в линиях 1XBET, OINABET, UBET, Polymarket, Kalshi.\n"
            "Проверьте написание (можно по-русски или по-английски) и число коэффициентов: "
            "3 для футбола, 2 для тенниса, киберспорта и волейбола.")
        return
    head = (f"Нашёл: <b>{escape(res.event_title)}</b>, {local_time(res.event_start)}\n"
            f"Сравниваю {escape(bk_title)} с: {', '.join(ptitle(p) for p in res.matched_platforms)}")
    good = [o for o in res.opportunities if o.status.value == "VERIFIED"]
    if good:
        await wait.edit_text(head + f"\n\n✅ Есть вилка! Вариантов: {len(good)}. Лучший ниже.")
        for o in good[:2]:
            service.live[o.signature] = o
            await send_card(m, service, o, u.settings.capital_kzt)
        return
    await wait.edit_text(manual_no_arb_text(res, bk_title, head))


# ------------------------------------------------------------------ public status
@router.message(Command("status"))
async def status_cmd(m: Message, service) -> None:
    await m.answer(status_text(service))


def status_text(service) -> str:
    from app.providers.browser.zendriver_client import ZendriverClient
    from app.proxy_manager import shared as proxy_manager
    from app.timeutil import now_almaty

    c = service.counters
    ch = ZendriverClient.shared().health()
    odds = service.providers.get("the_odds_api")
    cr = odds.credits() if odds is not None and hasattr(odds, "credits") else {}
    pm = proxy_manager().status()
    working, total = service.sources_working()
    users = service.user_count if service.user_count is not None else "—"
    lines = [
        "🩺 <b>Состояние бота</b>",
        f"Время: {now_almaty():%d.%m %H:%M:%S} (Asia/Almaty)",
        f"Пользователей: <b>{users}</b>",
        f"Сканирований: <b>{c['scans']}</b> · из кеша: <b>{c['cache_hits']}</b> · ожидали общий скан: {c['waited']}"
        f" · ошибок: {c['errors']}",
        f"Последнее успешное обновление: <b>{fmt_almaty(service.last_success_at, '%d.%m %H:%M:%S')}</b> (Алматы)",
        f"Плановый скан: каждые {get_settings().scan_interval_seconds} с · уведомлений отправлено: {c['notified']}",
        f"Источники данных: {working}/{total} работают",
        "The Odds API: " + (f"осталось <b>{'—' if cr.get('remaining') is None else cr['remaining']}</b> кредитов, сегодня {cr.get('used_today', 0)}"
                            f" из {cr.get('daily_cap', '—')}" if cr else "не подключён"),
        "Chrome/Zendriver: " + (f"работает, {ch['processes']} процессов, {ch['rss_mb']} МБ, CPU {ch['cpu_percent']}%,"
                                f" страниц {ch['pages']}, перезапусков {ch['restarts']}" if ch["running"]
                                else "не запущен (стартует при скане)"),
        f"Прокси-менеджер: {'включён' if pm.enabled else 'выключен'} · {escape(pm.source_state[:120])}",
        f"Антиспам: отклонено запросов {THROTTLE.dropped}",
    ]
    return "\n".join(lines)
