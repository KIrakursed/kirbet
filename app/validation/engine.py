"""Validation pipeline:  NEW → MATCHING → VALIDATING → RECHECKING → VERIFIED.

Rejections: REJECTED, EXPIRED, STALE, INSUFFICIENT_LIQUIDITY,
SETTLEMENT_MISMATCH, UNAVAILABLE.
Before a notification every involved market is re-fetched (full order books)
and the allocation is re-optimised on the fresh prices.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.arbitrage.finder import Opportunity
from app.arbitrage.optimizer import FX, optimize
from app.domain import Leg, OppStatus, utcnow
from app.providers.base import BaseProvider, ProviderState

log = logging.getLogger("validation")


@dataclass
class ValidationSettings:
    min_roi_pct: Decimal = Decimal("0.5")
    min_profit_usd: Decimal = Decimal("0.5")
    max_quote_age_s: float = 20.0
    min_confidence: float = 0.9
    capital_usd: Decimal = Decimal("1000")
    min_liquidity_usd: Decimal = Decimal("20")
    allow_live: bool = True
    allow_restricted_regions: bool = False
    max_roi_sanity_pct: Decimal = Decimal("1000")  # above this → rejected as a data error
    anomaly_roi_pct: Decimal = Decimal("25")  # above this → shown, but flagged as a risk


class Validator:
    def __init__(self, providers: dict[str, BaseProvider], fx: FX, settings: ValidationSettings):
        self.providers = providers
        self.fx = fx
        self.s = settings

    def _provider_for(self, platform: str) -> BaseProvider | None:
        if platform in self.providers:
            return self.providers[platform]
        return self.providers.get("the_odds_api")  # bookmakers that came via the aggregator

    def static_checks(self, opp: Opportunity) -> bool:
        opp.set(OppStatus.MATCHING, f"confidence={opp.confidence:.2f}")
        if opp.confidence < self.s.min_confidence:
            opp.set(OppStatus.REJECTED, f"низкая уверенность сопоставления {opp.confidence:.2f}")
            return False
        opp.set(OppStatus.VALIDATING)
        if opp.blocking:
            opp.set(OppStatus.SETTLEMENT_MISMATCH, "; ".join(opp.blocking))
            return False
        now = utcnow()
        if opp.start_time and opp.start_time < now and not self.s.allow_live:
            opp.set(OppStatus.EXPIRED, "событие уже началось (live отключён)")
            return False
        if opp.start_time and opp.start_time < now - timedelta(hours=8) and opp.market_type.value != "multi_outcome":
            opp.set(OppStatus.EXPIRED, "событие слишком давно началось")
            return False
        for p in opp.platforms:
            prov = self._provider_for(p)
            if prov is None:
                continue
            if prov.status.state in (ProviderState.DOWN, ProviderState.UNAVAILABLE):
                opp.set(OppStatus.UNAVAILABLE, f"{p}: источник недоступен")
                return False
            if "ЗАБЛОКИРОВАНА" in (prov.status.region_note or "") and not self.s.allow_restricted_regions:
                opp.set(OppStatus.UNAVAILABLE, f"{p}: торговля недоступна в вашем регионе")
                return False
        r = opp.result
        if r is None or r.min_profit <= 0:
            opp.set(OppStatus.REJECTED, "нет положительной прибыли во всех сценариях после комиссий")
            return False
        return True

    async def recheck(self, opp: Opportunity) -> Opportunity:
        """Re-fetch prices for every market and recompute."""
        opp.set(OppStatus.RECHECKING)
        refreshed: dict[int, object] = {}
        for m in opp.markets:
            prov = self._provider_for(m.platform)
            if prov is None:
                continue
            try:
                await prov.refresh_market(m)
                refreshed[id(m)] = m
            except Exception as e:  # noqa: BLE001
                opp.set(OppStatus.UNAVAILABLE, f"{m.platform}: не удалось обновить цены ({e})")
                return opp
        # rebuild legs from fresh quotes by instrument id
        index = {}
        for m in opp.markets:
            for o in m.outcomes:
                for q in o.quotes:
                    index[q.instrument_id] = q
        new_legs = []
        for lg in opp.legs:
            q = index.get(lg.quote.instrument_id)
            if q is None:
                opp.set(OppStatus.EXPIRED, f"{lg.quote.platform}: котировка {lg.quote.outcome_label} исчезла")
                return opp
            new_legs.append(Leg(q, lg.covers, lg.market))
        old = opp.result
        opp.legs = new_legs
        try:
            res = optimize(new_legs, opp.scenarios, capital=self.s.capital_usd, fx=self.fx)
        except ValueError as e:
            opp.set(OppStatus.REJECTED, str(e))
            return opp
        if res is None:
            opp.set(OppStatus.INSUFFICIENT_LIQUIDITY, "после обновления объём недостаточен даже для минимального ордера")
            return opp
        opp.result = res
        if old and (old.roi_pct != res.roi_pct):
            opp.log.append(("RECHECKING", f"ROI изменился {old.roi_pct}% → {res.roi_pct}%"))
        age = opp.max_quote_age
        if age > self.s.max_quote_age_s:
            opp.set(OppStatus.STALE, f"котировки старше {self.s.max_quote_age_s:.0f}с ({age:.0f}с)")
            return opp
        if res.min_profit <= 0:
            opp.set(OppStatus.REJECTED, f"после повторной проверки прибыль {res.min_profit} ≤ 0")
            return opp
        if res.roi_pct < self.s.min_roi_pct or res.min_profit < self.s.min_profit_usd:
            opp.set(OppStatus.REJECTED, f"ROI {res.roi_pct}% / прибыль ${res.min_profit} ниже порога")
            return opp
        if res.total_cost < self.s.min_liquidity_usd:
            opp.set(OppStatus.INSUFFICIENT_LIQUIDITY,
                    f"исполнимый объём ${res.total_cost} меньше минимума ${self.s.min_liquidity_usd}")
            return opp
        if opp.start_time and opp.start_time < utcnow():
            blind = [f.leg.quote.platform for f in res.legs if not f.leg.quote.depth_known]
            if blind:
                opp.set(OppStatus.REJECTED,
                        f"live-событие: у {', '.join(blind)} нет реального стакана (котировки агрегатора "
                        "отстают от рынка) — вилка не подтверждена")
                return opp
        if self.s.anomaly_roi_pct < res.roi_pct <= self.s.max_roi_sanity_pct:
            opp.warnings.append(f"РИСК: необычно высокая доходность {res.roi_pct.quantize(Decimal('0.1'))}% — "
                                "так бывает при ошибке в линии или устаревшей цене. Сверьте коэффициенты на сайтах")
        if res.roi_pct > self.s.max_roi_sanity_pct:
            opp.set(OppStatus.REJECTED, f"аномальный ROI {res.roi_pct}% > {self.s.max_roi_sanity_pct}% — "
                                        "вероятна ошибка данных/сопоставления, нужна ручная проверка")
            return opp
        opp.verified_at = utcnow()
        opp.set(OppStatus.VERIFIED, f"ROI {res.roi_pct}% прибыль ${res.min_profit}")
        return opp

    async def validate(self, opp: Opportunity) -> Opportunity:
        if not self.static_checks(opp):
            return opp
        return await self.recheck(opp)
