"""One scan cycle: fetch → normalise/match → find → validate."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.arbitrage.finder import Finder, Opportunity
from app.config import get_settings
from app.domain import Market, OppStatus, utcnow
from app.matching.engine import REJECTED, group_markets
from app.monitoring.fx import load_fx
from app.providers.base import BaseProvider, ProviderState
from app.providers.registry import DATA_PROVIDERS, KZ_BOOKMAKERS
from app.validation.engine import ValidationSettings, Validator

log = logging.getLogger("scanner")


KZ_SET = set(KZ_BOOKMAKERS)


@dataclass
class ScanReport:
    started: datetime
    duration_s: float = 0.0
    markets_by_provider: dict[str, int] = field(default_factory=dict)
    events_by_provider: dict[str, int] = field(default_factory=dict)
    groups_cross: int = 0
    candidates: int = 0
    verified: list[Opportunity] = field(default_factory=list)
    rejected: list[Opportunity] = field(default_factory=list)
    finder_stats: dict = field(default_factory=dict)
    provider_errors: dict[str, str] = field(default_factory=dict)
    categories: dict[str, int] = field(default_factory=dict)
    cross_groups_sample: list[str] = field(default_factory=list)
    closest: list[tuple[Decimal, str]] = field(default_factory=list)
    rejected_matches: dict[str, int] = field(default_factory=dict)  # filtered false/uncertain matches
    kz_groups: int = 0  # matched events offered by 2+ Kazakhstan bookmakers

    @property
    def total_markets(self) -> int:
        return sum(self.markets_by_provider.values())


class Scanner:
    def __init__(self, providers: dict[str, BaseProvider], vsettings: ValidationSettings,
                 enabled: set[str] | None = None, categories: set[str] | None = None,
                 max_validate: int = 30):
        self.providers = providers
        self.vs = vsettings
        self.enabled = enabled
        self.categories = categories
        self.max_validate = max_validate
        self.last_report: ScanReport | None = None
        self.last_markets: list[Market] = []  # used by manual odds entry
        self._lock = asyncio.Lock()

    def active_providers(self) -> list[BaseProvider]:
        out = []
        for name in DATA_PROVIDERS:
            p = self.providers.get(name)
            if p and p.enabled and (self.enabled is None or name in self.enabled):
                out.append(p)
        return out

    async def _fetch(self, p: BaseProvider) -> list[Market]:
        await p.health_check()
        if p.status.state in (ProviderState.NEEDS_KEY, ProviderState.UNAVAILABLE):
            return []
        return await p.fetch_markets()

    def _analyze(self, markets: list[Market], fx):
        t0 = time.monotonic()
        finder = Finder(capital_usd=self.vs.capital_usd, fx=fx)
        candidates: list[Opportunity] = []
        for m in markets:
            candidates += finder.from_market(m)
        h2h = [m for m in markets if len(m.participants) == 2]
        REJECTED.clear()
        # below the configured confidence (default 0.9) an event never takes part in arbitrage
        min_conf = float(get_settings().min_match_confidence)
        groups = [g for g in group_markets(h2h, min_conf=min_conf) if len(g.markets) > 1]
        self.last_rejected = dict(REJECTED)
        for g in groups:
            candidates += finder.from_group(g)
        log.info("analysis: %d markets, %d groups in %.1fs", len(markets), len(groups), time.monotonic() - t0)
        return candidates, groups, finder

    async def run(self) -> ScanReport:
        async with self._lock:
            return await self._run()

    async def _run(self) -> ScanReport:
        t0 = time.monotonic()
        rep = ScanReport(started=utcnow())
        fx = await load_fx()
        provs = self.active_providers()
        results = await asyncio.gather(*(self._fetch(p) for p in provs), return_exceptions=True)
        markets: list[Market] = []
        for p, res in zip(provs, results):
            if isinstance(res, Exception):
                rep.provider_errors[p.name] = str(res)
                p.mark_down(res)
                continue
            if p.status.state == ProviderState.DOWN:
                rep.provider_errors[p.name] = p.status.last_error
            rep.markets_by_provider[p.name] = len(res)
            rep.events_by_provider[p.name] = p.status.events
            markets.extend(res)
        if self.categories:
            markets = [m for m in markets if m.category in self.categories or
                       (m.market_type.value in ("binary_event", "multi_outcome") and "prediction" in self.categories)]
        self.last_markets = markets
        for m in markets:
            rep.categories[m.category] = rep.categories.get(m.category, 0) + 1

        # CPU-heavy matching runs in a worker thread so the Telegram bot stays responsive
        candidates, groups, finder = await asyncio.to_thread(self._analyze, markets, fx)
        rep.groups_cross = len(groups)
        self.last_groups = groups
        rep.rejected_matches = getattr(self, "last_rejected", {})
        rep.kz_groups = sum(1 for g in groups if len({m.platform for m in g.markets} & KZ_SET) >= 2)
        rep.cross_groups_sample = [
            " ⇄ ".join(f"{m.platform}:{m.title[:50]}" for m in g.markets) + f" (conf {g.confidence:.2f})"
            for g in groups[:15]
        ]
        rep.finder_stats = finder.stats
        seen: set[str] = set()
        for s_val, desc in sorted(finder.closest):
            key = desc.split(" | ")[0]
            if key not in seen:
                seen.add(key)
                rep.closest.append((s_val, desc))
            if len(rep.closest) >= 10:
                break

        # dedupe by signature, keep best
        best: dict[str, Opportunity] = {}
        for c in candidates:
            if c.signature not in best or (c.result and best[c.signature].result
                                           and c.result.min_profit > best[c.signature].result.min_profit):
                best[c.signature] = c
        cands = sorted(best.values(), key=lambda o: o.result.roi_pct if o.result else 0, reverse=True)
        rep.candidates = len(cands)

        validator = Validator(self.providers, fx, self.vs)
        for opp in cands[: self.max_validate]:
            try:
                await validator.validate(opp)
            except Exception as e:  # noqa: BLE001
                log.exception("validation failed")
                opp.set(OppStatus.UNAVAILABLE, f"ошибка проверки: {e}")
            (rep.verified if opp.status == OppStatus.VERIFIED else rep.rejected).append(opp)
        for opp in cands[self.max_validate:]:
            opp.set(OppStatus.REJECTED, "не проверено: превышен лимит проверок за цикл")
            rep.rejected.append(opp)
        rep.duration_s = time.monotonic() - t0
        self.last_report = rep
        log.info("scan: %d markets, %d cross groups, %d candidates, %d verified (%.1fs)",
                 rep.total_markets, rep.groups_cross, rep.candidates, len(rep.verified), rep.duration_s)
        return rep


def default_vsettings(capital: Decimal, min_roi: Decimal, max_age: float) -> ValidationSettings:
    return ValidationSettings(capital_usd=capital, min_roi_pct=min_roi, max_quote_age_s=max_age)
