"""Currency rates (USD base). Free, keyless source: open.er-api.com."""

from __future__ import annotations

import logging
import time
from decimal import Decimal

import httpx

from app.arbitrage.optimizer import FX
from app.config import get_settings

log = logging.getLogger("fx")
_cache: tuple[float, FX] | None = None


async def load_fx(client: httpx.AsyncClient | None = None) -> FX:
    """1 unit of currency X expressed in USD."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < 3600:
        return _cache[1]
    s = get_settings()
    rates = {"KZT": Decimal(1) / s.kzt_per_usd_fallback, "USDC": Decimal(1)}
    try:
        own = client is None
        client = client or httpx.AsyncClient(timeout=15)
        r = await client.get("https://open.er-api.com/v6/latest/USD")
        data = r.json()
        if own:
            await client.aclose()
        for cur in ("KZT", "EUR", "GBP", "RUB"):
            v = data.get("rates", {}).get(cur)
            if v:
                rates[cur] = Decimal(1) / Decimal(str(v))
        rates["_source"] = Decimal(1)
    except Exception as e:  # noqa: BLE001
        log.warning("FX fetch failed, using fallback KZT rate: %s", e)
    rates.pop("_source", None)
    fx = FX("USD", rates)
    _cache = (time.monotonic(), fx)
    return fx


def usd_to_kzt(fx: FX, usd: Decimal) -> Decimal:
    return fx.from_base(usd, "KZT")
