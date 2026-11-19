"""Health checks for proxy candidates: protocol (SOCKS5 / HTTPS CONNECT), latency, speed.

Checks go only to neutral public endpoints and carry no credentials. A transparent
proxy (one that forwards our real IP) is rejected: it would not even do its job.
"""

from __future__ import annotations

import time

import httpx

from app.domain import utcnow
from app.proxy_manager.models import ProxyCandidate, ProxyStats

LATENCY_URL = "https://www.gstatic.com/generate_204"
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=200000"
IP_URL = "https://api.ipify.org?format=json"


async def direct_ip() -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        return (await c.get(IP_URL)).json()["ip"]


async def check(p: ProxyCandidate, stats: ProxyStats, *, own_ip: str | None, timeout: float = 8.0,
                speed_test: bool = True) -> ProxyStats:
    stats.last_checked = utcnow()
    try:
        async with httpx.AsyncClient(proxy=p.url, timeout=timeout, follow_redirects=False) as c:
            t0 = time.monotonic()
            r = await c.get(LATENCY_URL)  # TLS through the proxy = CONNECT / SOCKS5 tunnel works
            lat = (time.monotonic() - t0) * 1000
            if r.status_code != 204:
                raise RuntimeError(f"unexpected HTTP {r.status_code}")
            seen = (await c.get(IP_URL)).json().get("ip")
            if own_ip and seen == own_ip:
                stats.anonymity = "transparent"
                raise RuntimeError("transparent: proxy forwards our own IP")
            # through a CONNECT/SOCKS tunnel the proxy cannot add X-Forwarded-For to HTTPS requests,
            # so an exit IP different from ours means the proxy hides it
            stats.anonymity = "anonymous" if seen else "unknown"
            kbps = None
            if speed_test:
                t1 = time.monotonic()
                body = (await c.get(SPEED_URL)).content
                kbps = len(body) * 8 / 1000 / max(time.monotonic() - t1, 1e-3)
        stats.ok += 1
        stats.last_latency_ms = round(lat, 1)
        stats.best_latency_ms = min(lat, stats.best_latency_ms or lat)
        stats.last_speed_kbps = round(kbps, 1) if kbps else stats.last_speed_kbps
        stats.last_error = ""
    except (TimeoutError, httpx.HTTPError, RuntimeError, ValueError, KeyError) as e:
        stats.fail += 1
        stats.last_error = f"{type(e).__name__}: {e}"[:200]
    return stats
