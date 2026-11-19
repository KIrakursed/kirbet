"""Telegram texts in plain Russian (HTML parse mode)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape

from app.arbitrage.finder import Opportunity
from app.domain import PriceKind, utcnow
from app.presentation.plan import COLOR_TEXT, Color, Plan, fmt_kzt, fmt_pct, plain_sentence
from app.timeutil import ALMATY, almaty  # noqa: E402,F401

NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
LINE = "━━━━━━━━━━━━━━"

CATEGORY_RU = {
    "tennis": "🎾 Теннис", "volleyball": "🏐 Волейбол", "football": "⚽ Футбол", "basketball": "🏀 Баскетбол",
    "cs2": "🎮 Киберспорт · CS2", "dota2": "🎮 Киберспорт · Dota 2", "lol": "🎮 Киберспорт · LoL",
    "valorant": "🎮 Киберспорт · Valorant", "sc2": "🎮 Киберспорт · StarCraft II",
    "politics": "🏛 Политика", "economics": "📈 Экономика", "finance": "📈 Финансы", "crypto": "🪙 Криптовалюты",
    "tech": "💻 Технологии", "culture": "🎬 Развлечения", "science": "🔬 Наука", "geopolitics": "🌍 Геополитика",
}
HEADER = {
    Color.GREEN: "🟢 НАЙДЕНА ВИЛКА",
    Color.YELLOW: "🟡 НАЙДЕНА ВИЛКА — ЕСТЬ РИСКИ",
    Color.RED: "🔴 ВИЛКА ИСЧЕЗЛА",
    Color.GREY: "⚪ НЕ ПОДТВЕРЖДЕНО",
}
GLOSSARY = (
    "<b>Словарик</b>\n"
    "• <b>Вилка</b> — ставки на все исходы на разных площадках так, чтобы при любом результате вернулось больше, чем вложено.\n"
    "• <b>Коэффициент</b> — во сколько раз вырастет ставка, если исход сыграет (ставка × коэф. = выплата).\n"
    "• <b>Контракт</b> (Polymarket, Kalshi) — стоит от 0 до 1 $, если исход сыграл — платит 1 $.\n"
    "• <b>Сколько реально доступно</b> — сколько денег можно поставить по показанной цене прямо сейчас.\n"
    "• <b>Устаревшие коэффициенты</b> — цены, которые давно не обновлялись; по ним вилку не показываем.\n"
    "• <b>Правила определения результата</b> — как площадка считает ставку при отмене матча или отказе игрока."
)


def local_time(dt: datetime | None) -> str:
    if dt is None:
        return "время не указано"
    return almaty(dt).strftime("%d.%m в %H:%M") + " (Алматы)"  # naive DB value = UTC, never server zone


def ago(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        n, w = s, ("секунду", "секунды", "секунд")
    elif s < 3600:
        n, w = s // 60, ("минуту", "минуты", "минут")
    else:
        n, w = s // 3600, ("час", "часа", "часов")
    form = w[2] if 11 <= n % 100 <= 14 else w[0] if n % 10 == 1 else w[1] if 2 <= n % 10 <= 4 else w[2]
    return f"{n} {form} назад"


def outcome_text(label: str, kind: PriceKind, mtype: str) -> str:
    low = label.strip().lower()
    if low in ("draw", "ничья", "x", "tie"):
        return "Ничья"
    if " (no на «" in low:
        who, rest = label.split(" (NO на «", 1)
        return f"Победа {who} (контракт «НЕТ» на {rest.rstrip('»)')})"
    if kind == PriceKind.EXCHANGE_LAY:
        return f"Против «{label}» (LAY на бирже)"
    if mtype in ("match_winner", "set_winner", "map_winner"):
        return f"Победа {label}"
    return f"«{label}»"


def _price_line(lp) -> str:
    if lp.kind == PriceKind.BINARY_CONTRACT:
        odds = (Decimal(1) / lp.price).quantize(Decimal("0.01"))
        return f"Цена контракта: {lp.price} $ (как коэффициент {odds})"
    return f"Коэффициент: {lp.price}"


def _stake_line(lp, plan: Plan) -> str:
    if lp.currency == "KZT":
        return f"Ставка: <b>{fmt_kzt(lp.stake_kzt)}</b>"
    contracts = ""
    if lp.kind == PriceKind.BINARY_CONTRACT:
        contracts = f", {lp.units.normalize():f} контр."
    return f"Ставка: <b>{fmt_kzt(lp.stake_kzt)}</b> (= {lp.stake_native.quantize(Decimal('0.01'))} ${contracts})"


def simple_card(opp: Opportunity, plan: Plan) -> str:
    e = escape
    out = [f"<b>{HEADER[plan.color]}</b>", ""]
    out.append(CATEGORY_RU.get(opp.category, "🏆 " + opp.category.capitalize()))
    out.append(f"<b>{e(opp.title.replace(' vs ', ' против ').replace(' — ', ' против '))}</b>")
    out.append(f"🕒 {local_time(opp.start_time)}")
    out += ["", LINE, "", f"💰 ТВОЙ КАПИТАЛ: <b>{fmt_kzt(plan.requested_kzt)}</b>", "", "<b>Куда вложить деньги:</b>"]
    for i, lp in enumerate(plan.legs):
        out.append("")
        out.append(f"{NUM_EMOJI[i] if i < len(NUM_EMOJI) else i + 1} <b>{e(lp.title)}</b>")
        out.append(f"Исход: {e(outcome_text(lp.outcome, lp.kind, opp.market_type.value))}")
        out.append(_price_line(lp))
        out.append(_stake_line(lp, plan))
    out += ["", LINE, "", "📊 <b>РЕЗУЛЬТАТ</b>", ""]
    out.append(f"Общая сумма ставок: {fmt_kzt(plan.total_kzt)}")
    if plan.payout_max - plan.payout_min <= max(Decimal(2), plan.total_kzt * Decimal("0.001")):
        out.append(f"Сколько вернётся: {fmt_kzt(plan.payout_min)}")
        same = True
    else:
        out.append(f"Сколько вернётся: от {fmt_kzt(plan.payout_min)} до {fmt_kzt(plan.payout_max)}")
        same = False
    out.append("")
    out.append(f"<b>ЧИСТАЯ ПРИБЫЛЬ: {fmt_kzt(plan.profit_kzt, sign=True)}</b>")
    out.append(f"<b>ПРИБЫЛЬ В ПРОЦЕНТАХ: {fmt_pct(plan.roi_pct)}</b>")
    if plan.profit_kzt > 0:
        out.append("При любом исходе выплата примерно одинаковая." if same else
                   "Прибыль указана для худшего исхода — при других исходах она больше.")
    if plan.limited_note:
        out += ["", f"ℹ️ {e(plan.limited_note)}"]
    out += ["", LINE, "", f"⏱ Данные обновлены: {ago(plan.age_s)}"]
    out.append(f"Статус: {plan.color.value} " + (
        "Цены повторно проверены." if plan.color in (Color.GREEN, Color.YELLOW) else COLOR_TEXT[plan.color].capitalize() + "."))
    if plan.color == Color.YELLOW and plan.risks:
        out.append(f"⚠️ Главный риск: {e(plan.risks[0])}")
    out += ["", "⚠️ Это расчётная возможность. Букмекер может изменить коэффициент, ограничить ставку или не принять её."]
    return "\n".join(out)


def details_card(opp: Opportunity, plan: Plan) -> str:
    e = escape
    r = opp.result
    out = [f"<b>📊 Подробно: {e(opp.title)}</b>", ""]
    tournaments = {m.tournament for m in opp.markets if m.tournament}
    out.append(f"Категория: {CATEGORY_RU.get(opp.category, opp.category)}")
    if tournaments:
        out.append(f"Турнир: {e('; '.join(sorted(tournaments))[:200])}")
    out.append(f"Дата и время: {local_time(opp.start_time)}")
    out.append(f"Площадки: {', '.join(lp.title for lp in plan.legs)}")
    out.append(f"Совпадение события на площадках: {int(opp.confidence * 100)}%")
    out.append("")
    for i, lp in enumerate(plan.legs, 1):
        out.append(f"<b>{i}. {e(lp.title)}</b> — {e(outcome_text(lp.outcome, lp.kind, opp.market_type.value))}")
        out.append(f"   {_price_line(lp)}")
        if lp.kind == PriceKind.BINARY_CONTRACT:
            out.append(f"   Средняя цена с учётом стакана: {lp.avg_price.quantize(Decimal('0.0001'))} $")
        out.append(f"   Размер позиции: {fmt_kzt(lp.stake_kzt)} → выплата {fmt_kzt(lp.payout_kzt)}")
        out.append(f"   Комиссия площадки: {fmt_kzt(lp.fee_kzt)}")
        out.append(f"   Сколько реально доступно по цене {lp.price}: " + (
            fmt_kzt(lp.available_kzt) if lp.available_kzt is not None else "неизвестно (букмекер не публикует лимит)"))
        if lp.url:
            out.append(f"   {e(lp.url)}")
    out.append("")
    out.append("<b>Прибыль (в процентах от вложенного):</b>")
    out.append(f"• Теоретическая (лучшие цены, без комиссий): {fmt_pct(plan.theoretical_roi_pct)}")
    out.append(f"• После комиссий: {fmt_pct(plan.after_fees_roi_pct)}")
    out.append(f"• С учётом доступного объёма: {fmt_pct(plan.liquidity_roi_pct)}")
    out.append(f"• В худшем сценарии (твоя сумма): <b>{fmt_kzt(plan.profit_kzt, sign=True)} ({fmt_pct(plan.roi_pct)})</b>")
    for s, p in plan.profits_kzt.items():
        out.append(f"   если «{e(opp.scenario_labels.get(s, s))}»: {fmt_kzt(p, sign=True)}")
    out.append(f"Комиссии всего: {fmt_kzt(sum((lp.fee_kzt for lp in plan.legs), Decimal(0)))}")
    if plan.max_volume_kzt is not None:
        out.append("Максимальный объём, при котором ещё есть прибыль: " + (
            fmt_kzt(plan.max_volume_kzt) if plan.max_volume_kzt > 0 else "нет"))
    if plan.fx_used and plan.kzt_per_usd:
        out.append(f"Курс: 1 $ = {plan.kzt_per_usd} ₸ (без комиссии обменника)")
    out.append("")
    out.append(f"Последнее обновление цен: {ago(plan.age_s)}"
               + (f", проверено {almaty(opp.verified_at):%H:%M:%S}" if opp.verified_at else ""))
    out.append("")
    out.append("<b>Правила определения результата:</b>")
    for m in opp.markets:
        note = m.settlement.notes or "см. правила площадки"
        out.append(f"• {e(m.platform)}: {e(note)}")
    if plan.risks:
        out.append("")
        out.append("<b>Почему может не получиться:</b>")
        out += [f"• {e(x)}" for x in plan.risks[:8]]
    out += ["", f"<i>{e(plain_sentence(plan))}</i>"]
    if r and r.limited_by:
        out.append(f"Объём ограничен: {'ликвидностью' if r.limited_by == 'liquidity' else 'капиталом'}.")
    out.append("Теоретическая прибыль не гарантирована, пока обе ставки не приняты.")
    return "\n".join(out)


def ago_from(dt: datetime | None) -> str:
    return ago((utcnow() - dt).total_seconds()) if dt else "—"
