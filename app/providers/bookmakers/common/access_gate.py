"""Public-page access check through Zendriver, shared by adapters whose data comes from elsewhere.

Before an adapter calls its data source (1xBet JSON line, Oinabet SignalR), the site's public page
is opened once like a visitor. If the site refuses us — HTTP 403, Cloudflare/anti-bot page, or a
redirect to its geo-block page (1xbet.kz → ``/ru/block``) — the verdict is kept for
``ttl_minutes`` and the adapter makes no further requests: no retries, no other IPs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.providers.browser.zendriver_client import ZendriverClient

GEO_BLOCK_PATHS = ("/block", "/blocked", "/restricted", "/geo")


@dataclass
class GateVerdict:
    ok: bool
    http_status: int | None
    reason: str
    checked_at: float


class AccessGate:
    def __init__(self, provider: str, url: str, *, ttl_minutes: float = 30, browser: ZendriverClient | None = None):
        self.provider, self.url, self.ttl = provider, url, ttl_minutes * 60
        self.browser = browser
        self.verdict: GateVerdict | None = None

    async def check(self) -> GateVerdict:
        if self.verdict and time.monotonic() - self.verdict.checked_at < self.ttl:
            return self.verdict
        b = self.browser or ZendriverClient.shared()
        # settle: some sites redirect to their block page from JS a few seconds after load (1xbet.kz)
        r = await b.fetch(self.url, timeout=40, settle=6.0, scroll=0)
        path = urlsplit(r.final_url or "").path.rstrip("/").lower()
        if not r.error and any(path.endswith(p) or f"{p}/" in path for p in GEO_BLOCK_PATHS):
            r.error, r.blocked = f"сайт перенаправил на страницу блокировки {r.final_url} (ограничение по региону)", "geo"
        ok = not r.error
        reason = "" if ok else f"HTTP {r.http_status}: {r.error}" if r.http_status else r.error
        self.verdict = GateVerdict(ok, r.http_status, reason, time.monotonic())
        if not ok and r.blocked:
            from app.proxy_manager import shared as proxy_manager

            proxy_manager().mark_blocked(self.provider, reason)  # never proxied around a block
        return self.verdict
