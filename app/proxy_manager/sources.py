"""Where proxy candidates come from.

proxy-tools.com (researched 19.11.2026):
  * the list API ``GET https://proxy-tools.com/api/v1/proxies`` (params ``geo``, ``type``,
    ``speed``, ``stability``, ``response``, ``limit``, ``page``) needs a personal Bearer key that
    is issued only to accounts with the paid "Pro Status"; 60 requests/minute per account;
  * the public HTML list (/proxy/kz) sits behind a "Подтвердите, что вы не робот" check and the
    site's terms forbid crawling/scraping, so it is NOT scraped here;
  * of 414 KZ entries, 412 were transparent HTTP proxies (they forward the real client IP).
So ``ProxyToolsSource`` works only with ``PROXY_TOOLS_API_KEY`` set by the owner of a Pro account.
``StaticSource`` takes proxies the user controls (e.g. their own server) from ``PROXY_LIST``.
"""

from __future__ import annotations

import logging
import re

import httpx

from app.proxy_manager.models import SUPPORTED, ProxyCandidate

log = logging.getLogger("proxy.sources")

API = "https://proxy-tools.com/api/v1/proxies"
TYPE_PARAM = {"socks5": 2, "https": 3}


class SourceUnavailable(Exception):
    """The source cannot be used (missing key, refused, unexpected format)."""


class StaticSource:
    name = "static"

    def __init__(self, spec: str):
        self.spec = spec or ""

    async def fetch(self) -> list[ProxyCandidate]:
        out = []
        for item in re.split(r"[\s,]+", self.spec.strip()):
            m = re.match(r"^(socks5|https|http)://([\w.-]+):(\d+)$", item)
            if not m:
                if item:
                    log.warning("PROXY_LIST: skipped %r (expected socks5://host:port or https://host:port)", item)
                continue
            proto, host, port = m.groups()
            if proto == "http":
                proto = "https"  # an http:// proxy URL is only accepted if it passes the CONNECT check
            out.append(ProxyCandidate(host=host, port=int(port), protocol=proto, source=self.name))
        return out


class ProxyToolsSource:
    name = "proxy-tools.com"

    def __init__(self, api_key: str, country: str = "KZ", min_stability: int = 80, max_response_s: float = 1.0):
        self.api_key = api_key
        self.country = country
        self.min_stability = min_stability
        self.max_response_s = max_response_s

    async def fetch(self) -> list[ProxyCandidate]:
        if not self.api_key:
            raise SourceUnavailable("needs_key: API proxy-tools.com доступен только с платным Pro Status "
                                    "(ключ PROXY_TOOLS_API_KEY не задан); HTML-список не парсится — "
                                    "условия сайта запрещают scraping")
        out: list[ProxyCandidate] = []
        async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json",
                                                          "Authorization": f"Bearer {self.api_key}"}) as c:
            for proto in SUPPORTED:
                params = {"geo": self.country, "type": TYPE_PARAM[proto], "speed": 3,
                          "stability": self.min_stability, "response": self.max_response_s, "limit": 100}
                r = await c.get(API, params=params)
                if r.status_code in (401, 402, 403):
                    raise SourceUnavailable(f"proxy-tools.com отказал (HTTP {r.status_code}): нужен Pro Status")
                if r.status_code == 429:
                    raise SourceUnavailable("proxy-tools.com: лимит 60 запросов/мин")
                r.raise_for_status()
                out += parse_api(r.json(), proto)
        return out


def parse_api(data, proto: str) -> list[ProxyCandidate]:
    """The response schema is not documented; accept the usual shapes and skip anything unclear."""
    items = data.get("data") or data.get("proxies") or data.get("items") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        host = it.get("ip") or it.get("host") or it.get("address")
        port = it.get("port")
        if not host or not str(port or "").isdigit():
            continue
        p = str(it.get("type") or it.get("protocol") or proto).lower()
        p = "socks5" if "socks5" in p else "https" if "https" in p else p
        if p not in SUPPORTED:
            continue
        out.append(ProxyCandidate(host=str(host), port=int(port), protocol=p, source=ProxyToolsSource.name,
                                  country=str(it.get("country") or it.get("geo") or "KZ").upper(),
                                  source_stability=_num(it.get("stability") or it.get("uptime")),
                                  source_response_s=_num(it.get("response") or it.get("response_time"))))
    return out


def _num(v) -> float | None:
    try:
        return float(str(v).rstrip("%")) if v is not None else None
    except ValueError:
        return None
