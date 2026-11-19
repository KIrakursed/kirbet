"""Stake calculator for mixed instruments.

Input: one line per mutually exclusive outcome, e.g.
    2.10                      decimal bookmaker odds
    2.05 max=300              odds with a stake limit (leg currency)
    0.47 pm                   binary contract at $0.47, Polymarket sports fee (rate 0.05)
    0.47 kalshi vol=200       Kalshi contract, 200 contracts available
    0.47 fee=0.02             binary with a 2% of cost fee
    back 2.2 c=0.05           exchange back, 5% commission on winnings
    lay 1.9 c=0.05            exchange lay (covers 'outcome does NOT happen')
Lines describe a partition of all scenarios: each line = one scenario.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.arbitrage.optimizer import FX, max_profitable_size, optimize
from app.domain import ArbResult, Leg, Level, PriceKind, Quote


class CalcError(ValueError):
    pass


@dataclass
class CalcOutput:
    result: ArbResult | None
    max_payout_profitable: Decimal
    risks: list[str]
    labels: list[str]


def _dec(s: str) -> Decimal:
    try:
        return Decimal(s.replace(",", "."))
    except InvalidOperation as e:
        raise CalcError(f"не число: {s}") from e


def parse_line(line: str, idx: int) -> Quote:
    toks = line.strip().lower().split()
    if not toks:
        raise CalcError("пустая строка")
    kind = None
    if toks[0] in ("back", "lay"):
        kind = PriceKind.EXCHANGE_BACK if toks[0] == "back" else PriceKind.EXCHANGE_LAY
        toks = toks[1:]
    price = _dec(toks[0])
    opts = {}
    flags = set()
    for t in toks[1:]:
        if "=" in t:
            k, v = t.split("=", 1)
            opts[k] = v
        else:
            flags.add(t)
    if kind is None:
        kind = PriceKind.BINARY_CONTRACT if price < 1 else PriceKind.DECIMAL_ODDS
    if kind == PriceKind.BINARY_CONTRACT and not (0 < price < 1):
        raise CalcError("цена контракта должна быть между 0 и 1")
    if kind != PriceKind.BINARY_CONTRACT and price <= 1:
        raise CalcError("коэффициент должен быть > 1")
    fee_model, params = "none", {"unit_step": "0.01"}
    if "pm" in flags or "polymarket" in flags:
        fee_model = "polymarket"
        params = {"enabled": True, "rate": opts.get("rate", "0.05"), "exponent": 1, "unit_step": "0.01"}
    elif "kalshi" in flags:
        fee_model, params = "kalshi", {"rate": "0.07", "multiplier": "1", "unit_step": "1"}
    elif "fee" in opts:
        fee_model, params = "flat_pct_of_cost", {"pct": opts["fee"], "unit_step": "0.01"}
    if "c" in opts:
        params["commission"] = opts["c"]
    if "step" in opts:
        params["unit_step"] = opts["step"]
    vol = _dec(opts["vol"]) if "vol" in opts else None
    mx = _dec(opts["max"]) if "max" in opts else None
    size = vol if vol is not None else (mx if mx is not None else Decimal("1e12"))
    return Quote(
        platform=f"#{idx}", kind=kind, outcome_label=f"Исход {idx}", instrument_id=f"calc{idx}",
        best_price=price, levels=[Level(price, size)], depth_known=True, fee_model=fee_model,
        fee_params=params, max_stake=mx, min_size=_dec(opts.get("min", "0")),
    )


def calculate(capital: Decimal, lines: list[str]) -> CalcOutput:
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) < 2:
        raise CalcError("нужно минимум 2 исхода")
    quotes = [parse_line(ln, i + 1) for i, ln in enumerate(lines)]
    scen = [f"s{i + 1}" for i in range(len(quotes))]
    legs = [Leg(q, frozenset({s})) for q, s in zip(quotes, scen)]
    res = optimize(legs, scen, capital=capital, fx=FX())
    mx = max_profitable_size(legs, scen)
    risks = [
        "Цены могут измениться до исполнения всех позиций (риск «одной ноги»).",
        "Ордера на биржах/prediction markets могут исполниться частично.",
        "Правила расчёта (отмена, retirement, ничья) на площадках могут различаться.",
    ]
    if any(q.kind == PriceKind.DECIMAL_ODDS for q in quotes):
        risks.append("Букмекер может порезать лимит или отклонить ставку.")
    if res and res.limited_by == "liquidity":
        risks.append("Размер ограничен доступным объёмом.")
    return CalcOutput(res, mx, risks, [q.outcome_label for q in quotes])
