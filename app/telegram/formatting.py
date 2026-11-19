"""Russian-language rendering of opportunities and reports."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from html import escape

from app.arbitrage.finder import Opportunity
from app.arbitrage.optimizer import FX
from app.domain import PriceKind, utcnow

KIND_RU = {
    PriceKind.DECIMAL_ODDS: "Коэффициент (БК, decimal)",
    PriceKind.BINARY_CONTRACT: "Контракт",
    PriceKind.EXCHANGE_BACK: "Биржа BACK",
    PriceKind.EXCHANGE_LAY: "Биржа LAY",
}
CAT_RU = {
    "tennis": "Теннис", "cs2": "CS2", "dota2": "Dota 2", "lol": "League of Legends",
    "valorant": "Valorant", "volleyball": "Волейбол", "sc2": "StarCraft II", "politics": "Политика",
    "crypto": "Криптовалюты", "economics": "Экономика", "finance": "Финансы", "tech": "Технологии",
    "culture": "Развлечения", "science": "Наука", "geopolitics": "Геополитика", "weather": "Погода",
}
MTYPE_RU = {
    "match_winner": "Победитель матча", "set_winner": "Победитель сета", "map_winner": "Победитель карты",
    "binary_event": "Да/Нет", "multi_outcome": "Взаимоисключающие исходы",
}


def money(v: Decimal, q: str = "0.01") -> str:
    v = Decimal(v).quantize(Decimal(q), ROUND_HALF_UP)
    s = f"{v:,.{max(0, -Decimal(q).as_tuple().exponent)}f}".replace(",", " ")
    return s


def _b(s: str, html: bool) -> str:
    return f"<b>{s}</b>" if html else s


def format_opportunity(o: Opportunity, fx: FX, *, html: bool = True) -> str:
    e = escape if html else (lambda x: x)
    r = o.result
    lines = []
    head = "🚨 НАЙДЕНА АРБИТРАЖНАЯ ВОЗМОЖНОСТЬ" if o.status.value in ("VERIFIED", "NOTIFIED") \
        else f"ℹ️ Возможность ({o.status.value})"
    lines.append(_b(head, html))
    lines.append("")
    lines.append(f"Событие: {e(o.title)}")
    lines.append(f"Категория: {CAT_RU.get(o.category, o.category)}")
    lines.append(f"Рынок: {MTYPE_RU.get(o.market_type.value, o.market_type.value)}"
                 + (f" ({o.period})" if o.period not in ('match',) else ""))
    if o.start_time:
        st = o.start_time
        live = " 🔴 LIVE" if st < utcnow() else ""
        lines.append(f"Начало: {st:%d.%m %H:%M} UTC{live}")
    lines.append(f"Тип: {'межплатформенный' if o.kind == 'cross' else 'внутри площадки'}"
                 f" · уверенность сопоставления {o.confidence:.2f}")
    lines.append("")
    lines.append(_b("ПЛАТФОРМЫ:", html))
    if r:
        for i, f in enumerate(r.legs, 1):
            q = f.leg.quote
            scen = ", ".join(o.scenario_labels.get(s, s) for s in f.leg.covers)
            lines.append("")
            lines.append(f"{i}. {_b(e(q.platform), html)} — {KIND_RU[q.kind]}")
            if q.kind == PriceKind.BINARY_CONTRACT:
                lines.append(f"Позиция: BUY {q.side if q.side in ('YES', 'NO') else ''} «{e(q.outcome_label)}» "
                             f"(выигрывает при: {e(scen)})")
                lines.append(f"Цена покупки (ask): {q.best_price}  · продажи (bid): {q.bid_price if q.bid_price is not None else '—'}")
                lines.append(f"Средняя цена с учётом стакана: {f.avg_price.quantize(Decimal('0.0001'))}"
                             f" · худший уровень {f.worst_price}")
                depth = sum((lv.size for lv in q.levels), Decimal(0))
                lines.append(f"Доступно в стакане: {money(depth, '1')} контрактов · комиссия ${money(f.fee)}")
                lines.append(f"Купить: {money(f.units, '0.01')} контрактов на ${money(f.stake)} "
                             f"→ выплата ${money(f.payout_if_win)}")
            else:
                lines.append(f"Исход: {e(q.outcome_label)} ({e(scen)})")
                lines.append(f"Коэффициент: {q.best_price}")
                lines.append(f"Ставка: {money(f.units)} {q.currency} → выплата {money(f.payout_if_win)} {q.currency}")
            if q.url:
                lines.append(e(q.url))
        lines.append("")
        cost_kzt = fx.from_base(r.total_cost, "KZT") if "KZT" in fx.rates else None
        lines.append(f"Общий капитал: ${money(r.total_cost)}"
                     + (f" (≈ {money(cost_kzt, '1')} ₸)" if cost_kzt else ""))
        for s, p in r.scenarios.items():
            lines.append(f"Если «{e(o.scenario_labels.get(s, s))}»: {'+' if p >= 0 else ''}${money(p)}")
        lines.append(_b(f"Мин. чистая прибыль: ${money(r.min_profit)}  ·  ROI: {r.roi_pct.quantize(Decimal('0.01'))}%", html))
        if r.limited_by:
            lines.append(f"Объём ограничен: {'ликвидностью' if r.limited_by == 'liquidity' else 'капиталом'}")
    lines.append("")
    lines.append(f"Статус: {o.status.value}")
    if o.verified_at:
        lines.append(f"Проверено: {o.verified_at:%H:%M:%S} UTC")
    lines.append(f"Возраст котировок: {o.max_quote_age:.0f} c")
    warns = list(dict.fromkeys(o.warnings))
    if warns:
        lines.append("")
        lines.append(_b("Риски:", html))
        for w in warns[:6]:
            lines.append(f"• {e(w)}")
    lines.append("")
    lines.append(_b("ВНИМАНИЕ:", html))
    lines.append("Теоретическая прибыль не гарантирует успешное исполнение. Цены могут измениться "
                 "до исполнения, ордера — исполниться частично. Проверьте правила расчёта и доступность "
                 "площадок в вашей юрисдикции.")
    return "\n".join(lines)


def format_scan_report(rep, *, html: bool = True) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    lines = [b("📊 Результат сканирования"),
             f"Время: {rep.started:%d.%m %H:%M:%S} UTC, длительность {rep.duration_s:.1f} c"]
    for p, n in rep.markets_by_provider.items():
        lines.append(f"• {p}: событий {rep.events_by_provider.get(p, 0)}, рынков {n}")
    for p, err in rep.provider_errors.items():
        lines.append(f"⚠️ {p}: {escape(err[:150]) if html else err[:150]}")
    cats = ", ".join(f"{CAT_RU.get(k, k)} {v}" for k, v in sorted(rep.categories.items(), key=lambda x: -x[1])[:12])
    lines.append(f"Категории: {cats}")
    lines.append(f"Сопоставлено межплатформенных рынков: {rep.groups_cross}")
    st = rep.finder_stats
    lines.append(f"Комбинаций проверено: {st.get('combos_checked', 0)}, прошли префильтр: "
                 f"{st.get('prefilter_pass', 0)}, кандидатов: {rep.candidates}")
    lines.append(f"VERIFIED: {len(rep.verified)}, отклонено: {len(rep.rejected)}")
    return "\n".join(lines)
