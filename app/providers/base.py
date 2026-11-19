"""Provider base class: HTTP, rate limiting, caching, health, logging."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.config import get_settings, provider_config
from app.domain import Market, PlatformType, utcnow

USER_AGENT = "cross-arb-scanner/1.0 (+read-only market data)"


class ProviderState(str, enum.Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    NEEDS_KEY = "needs_key"
    UNAVAILABLE = "unavailable"  # no legal/technical access path
    DISABLED = "disabled"


@dataclass
class ProviderStatus:
    name: str
    platform_type: PlatformType
    state: ProviderState = ProviderState.DOWN
    message: str = ""
    last_success: datetime | None = None
    last_error: str = ""
    latency_ms: float | None = None
    events: int = 0
    markets: int = 0
    errors: int = 0
    requests: int = 0
    rate_limited: int = 0
    coverage: list[str] = field(default_factory=list)
    docs: str = ""
    requires_key: bool = False
    region_note: str = ""


class ProviderError(Exception):
    pass


class RateLimiter:
    """Simple async token bucket."""

    def __init__(self, rate_per_sec: float, burst: int | None = None):
        self.rate = rate_per_sec
        self.capacity = burst or max(1, int(rate_per_sec))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        v = self._d.get(key)
        if not v:
            return None
        if time.monotonic() - v[0] > self.ttl:
            self._d.pop(key, None)
            return None
        return v[1]

    def set(self, key: str, value: Any) -> None:
        self._d[key] = (time.monotonic(), value)


class BaseProvider:
    name: str = "base"
    platform_type: PlatformType = PlatformType.PREDICTION_MARKET
    docs_url: str = ""
    default_rate: float = 5.0
    requires_key: bool = False

    def __init__(self, client: httpx.AsyncClient | None = None, config: dict | None = None):
        self.cfg = {**self.default_config(), **(config if config is not None else provider_config(self.name))}
        self.log = logging.getLogger(f"provider.{self.name}")
        self.limiter = RateLimiter(float(self.cfg.get("rate_per_sec", self.default_rate)))
        self.cache = TTLCache(float(self.cfg.get("cache_ttl_sec", 30)))
        self._client = client
        self.last_headers: dict = {}
        self._own_client = client is None
        self.status = ProviderStatus(
            self.name, self.platform_type, docs=self.docs_url, requires_key=self.requires_key,
            coverage=list(self.cfg.get("coverage", [])), region_note=self.cfg.get("region_note", ""),
        )

    # ---- config -------------------------------------------------------
    def default_config(self) -> dict:
        return {}

    admin_disabled: bool = False  # set from the admin panel (persisted in providers.admin_disabled)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True)) and not self.admin_disabled

    # ---- http ---------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            s = get_settings()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                proxy=s.http_proxy or None,
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(self, method: str, url: str, *, retries: int = 2, cache_key: str | None = None, **kw) -> Any:
        if cache_key:
            hit = self.cache.get(cache_key)
            if hit is not None:
                return hit
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            await self.limiter.acquire()
            t0 = time.monotonic()
            self.status.requests += 1
            try:
                r = await self.client.request(method, url, **kw)
            except httpx.HTTPError as e:
                last_exc = e
                self.status.errors += 1
                self.status.last_error = f"{type(e).__name__}: {e}"
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            self.status.latency_ms = (time.monotonic() - t0) * 1000
            self.last_headers = r.headers
            if r.status_code == 429:
                self.status.rate_limited += 1
                wait = float(r.headers.get("Retry-After", 2 * (attempt + 1)))
                self.log.warning("rate limited, sleeping %.1fs", wait)
                await asyncio.sleep(min(wait, 30))
                last_exc = ProviderError("HTTP 429")
                continue
            if r.status_code >= 500:
                self.status.errors += 1
                last_exc = ProviderError(f"HTTP {r.status_code}")
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if r.status_code >= 400:
                self.status.errors += 1
                self.status.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                raise ProviderError(self.status.last_error)
            try:
                data = r.json()
            except ValueError as e:
                self.status.errors += 1
                raise ProviderError(f"не JSON ответ: {e}") from e
            if cache_key:
                self.cache.set(cache_key, data)
            return data
        self.status.last_error = str(last_exc)
        raise ProviderError(str(last_exc))

    # ---- interface ----------------------------------------------------
    async def health_check(self) -> ProviderStatus:
        raise NotImplementedError

    async def fetch_markets(self) -> list[Market]:
        """Return markets with executable quotes (top of book at least)."""
        raise NotImplementedError

    async def refresh_market(self, market: Market) -> Market:
        """Re-fetch full order books / prices for one market (used by validator)."""
        raise NotImplementedError

    def mark_ok(self, events: int, markets: int) -> None:
        self.status.state = ProviderState.OK
        self.status.last_success = utcnow()
        self.status.events = events
        self.status.markets = markets
        self.status.message = ""

    def mark_down(self, err: Exception | str) -> None:
        self.status.state = ProviderState.DOWN
        self.status.last_error = str(err)
        self.status.message = str(err)[:300]
