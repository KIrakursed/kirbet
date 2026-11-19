"""Kazakhstan proxy manager: refresh → check → rank → hand out, with persistent stats.

Usage policy (enforced here, not left to callers):
  * a proxy is handed out only for a provider listed in ``PROXY_ALLOWED_PROVIDERS``
    (empty by default) — i.e. a connection the user knows proxying is allowed for;
  * never for Telegram, The Odds API or anything carrying a token or key;
  * a provider recorded as *blocked* (403 / CAPTCHA / anti-bot / geo block) never gets a proxy:
    proxies are not a way around a site's access decision, so there is no "try another IP".
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.config import ROOT, get_settings
from app.domain import utcnow
from app.proxy_manager.checker import check, direct_ip
from app.proxy_manager.models import SUPPORTED, ProxyCandidate, ProxyManagerStatus, ProxyStats
from app.proxy_manager.sources import ProxyToolsSource, SourceUnavailable, StaticSource

log = logging.getLogger("proxy.manager")
STATS_FILE = ROOT / "data" / "proxy_stats.json"
NEVER_PROXIED = {"telegram", "the_odds_api", "betfair", "kalshi", "polymarket"}  # tokens / keys / no need


class ProxyManager:
    def __init__(self, *, enabled: bool | None = None, api_key: str | None = None, static: str | None = None,
                 allowed: set[str] | None = None, min_stability: float = 80.0, max_latency_ms: float = 1000.0,
                 stats_file: Path = STATS_FILE):
        s = get_settings()
        self.enabled = s.proxy_manager_enabled if enabled is None else enabled
        key = s.proxy_tools_api_key if api_key is None else api_key
        static = s.proxy_list if static is None else static
        self.source = StaticSource(static) if static else ProxyToolsSource(key)
        self.allowed = (allowed if allowed is not None else
                        {x.strip() for x in s.proxy_allowed_providers.split(",") if x.strip()}) - NEVER_PROXIED
        self.min_stability = min_stability
        self.max_latency_ms = max_latency_ms
        self.stats_file = stats_file
        self.candidates: dict[str, ProxyCandidate] = {}
        self.stats: dict[str, ProxyStats] = self._load()
        self.blocked: dict[str, str] = {}  # provider -> reason (sticky)
        self.source_state = "not refreshed"
        self.refreshed_at = None
        self._lock = asyncio.Lock()

    # ---- persistence -----------------------------------------------------------
    def _load(self) -> dict[str, ProxyStats]:
        try:
            raw = json.loads(self.stats_file.read_text(encoding="utf-8"))
            return {k: ProxyStats(**v) for k, v in raw.items()}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.stats_file.parent.mkdir(parents=True, exist_ok=True)
            self.stats_file.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in self.stats.items()},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("proxy stats not saved: %s", e)

    # ---- refresh / check -------------------------------------------------------------
    async def refresh(self, *, speed_test: bool = True) -> ProxyManagerStatus:
        async with self._lock:
            if not self.enabled:
                self.source_state = "disabled (PROXY_MANAGER_ENABLED=false)"
                return self.status()
            try:
                found = [c for c in await self.source.fetch() if c.protocol in SUPPORTED and c.country == "KZ"]
                self.source_state = "ok"
            except SourceUnavailable as e:
                self.source_state = str(e)
                found = []
            except Exception as e:  # noqa: BLE001
                self.source_state = f"error: {type(e).__name__}: {e}"[:200]
                found = []
            self.candidates.update({c.key: c for c in found})
            own = None
            if self.candidates:
                try:
                    own = await direct_ip()
                except Exception:  # noqa: BLE001
                    own = None
            sem = asyncio.Semaphore(10)

            async def one(c: ProxyCandidate):
                async with sem:
                    st = self.stats.setdefault(c.key, ProxyStats(key=c.key))
                    await check(c, st, own_ip=own, speed_test=speed_test)
                    st.active = self._qualifies(st)

            await asyncio.gather(*(one(c) for c in self.candidates.values()))
            # drop candidates that keep failing from the active list (stats are kept)
            for k in [k for k, st in self.stats.items() if k in self.candidates and st.fail >= 5 and st.stability < 50]:
                self.candidates.pop(k, None)
                self.stats[k].active = False
            self.refreshed_at = utcnow()
            self._save()
            return self.status()

    def _qualifies(self, st: ProxyStats) -> bool:
        return (not st.last_error and st.anonymity == "anonymous" and st.stability >= self.min_stability
                and st.last_latency_ms is not None and st.last_latency_ms <= self.max_latency_ms)

    def ranked(self) -> list[tuple[ProxyCandidate, ProxyStats]]:
        rows = [(c, self.stats[k]) for k, c in self.candidates.items() if k in self.stats and self.stats[k].active]
        # SOCKS5 first, then lowest latency, then highest speed
        return sorted(rows, key=lambda r: (r[0].protocol != "socks5", r[1].last_latency_ms or 1e9,
                                           -(r[1].last_speed_kbps or 0)))

    # ---- policy ---------------------------------------------------------------------
    def mark_blocked(self, provider: str, reason: str) -> None:
        """Called when a site refuses us. From now on that provider is never proxied."""
        if provider not in self.blocked:
            log.info("provider %s blocked (%s): no proxy will be used for it", provider, reason)
        self.blocked[provider] = reason

    def proxy_for(self, provider: str) -> str | None:
        if not self.enabled or provider in NEVER_PROXIED or provider not in self.allowed or provider in self.blocked:
            return None
        best = self.ranked()
        return best[0][0].url if best else None

    def report_result(self, proxy_url: str, ok: bool, error: str = "") -> None:
        for k, c in self.candidates.items():
            if c.url == proxy_url:
                st = self.stats.setdefault(k, ProxyStats(key=k))
                if ok:
                    st.ok += 1
                else:
                    st.fail += 1
                    st.last_error = error[:200]
                st.active = self._qualifies(st) if ok else False
        self._save()

    def status(self) -> ProxyManagerStatus:
        best = self.ranked()
        b = best[0] if best else None
        return ProxyManagerStatus(
            enabled=self.enabled, source=self.source.name, source_state=self.source_state,
            candidates=len(self.candidates), active=len(best), best=b[0].key if b else None,
            best_latency_ms=b[1].last_latency_ms if b else None, best_speed_kbps=b[1].last_speed_kbps if b else None,
            allowed_providers=sorted(self.allowed), refreshed_at=self.refreshed_at,
        )


_shared: ProxyManager | None = None


def shared() -> ProxyManager:
    global _shared
    if _shared is None:
        _shared = ProxyManager()
    return _shared
