"""Probe candidate public pages of the working bookmakers and count what the parsers extract.

    python scripts/probe_coverage.py
Used to decide which sport pages an adapter should read. robots.txt is respected.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.bookmakers.fonbet import parser as fonbet  # noqa: E402
from app.providers.bookmakers.winline import parser as winline  # noqa: E402
from app.providers.browser.zendriver_client import ZendriverClient  # noqa: E402

CANDIDATES = [
    *[("fonbet", f"https://fonbet.kz/sports/{s}", fonbet.parse) for s in
      ("volleyball", "esports", "table-tennis", "handball", "futsal", "mma", "boxing", "american-football")],
    *[("winline", f"https://winline.kz/sport/{i}", winline.parse) for i in (149, 167, 102, 10, 100248, 6, 92)],
]


async def main() -> None:
    c = ZendriverClient()
    for name, url, parse in CANDIDATES:
        r = await c.fetch(url, timeout=40, settle=3, scroll=3,
                          ready_js=r"/\b\d{1,2}[.,]\d{2}\b/.test(document.body && document.body.innerText || '')")
        evs = parse(r.html, "main", r.fetched_at) if r.html else []
        print(json.dumps({"bk": name, "url": url, "http": r.http_status, "final": r.final_url, "err": r.error,
                          "events": len(evs), "markets": sum(len(e.markets) for e in evs),
                          "sports": dict(Counter(e.sport for e in evs)), "tz": r.page_timezone},
                         ensure_ascii=False), flush=True)
    await c.close()


if __name__ == "__main__":
    asyncio.run(main())
