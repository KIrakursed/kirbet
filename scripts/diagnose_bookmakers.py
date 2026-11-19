"""Diagnose every KZ bookmaker from *this* machine with a normal Zendriver page view.

    python scripts/diagnose_bookmakers.py [--save DIR]

For each site: robots.txt verdict, main-document HTTP status, final URL, title,
visible-text sample, number of odds-like numbers in the DOM, and the HTTP
statuses of the JSON / XHR responses the page itself made (to tell a geo block
from an empty line). Nothing is retried through other IPs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.browser.zendriver_client import ZendriverClient  # noqa: E402

SITES = {
    "fonbet": "https://fonbet.kz/sports/football",
    "1xbet": "https://1xbet.kz/ru/line/football",
    "olimpbet": "https://olimpbet.kz/",
    "parimatch": "https://parimatch.kz/ru/football/",
    "winline": "https://winline.kz/",
    "tennisi": "https://tennisi.kz/",
    "oinabet": "https://oinabet.kz/",
    "ubet": "https://ubet.kz/",
}
TEXT_JS = "(document.body && document.body.innerText || '').slice(0, 1500)"
ODDS = re.compile(r"\b\d{1,2}\.\d{2}\b")


async def main(save: Path | None) -> None:
    c = ZendriverClient(respect_robots=False)  # report the robots verdict, but still look once
    out = {}
    for name, url in SITES.items():
        allowed, rule = (await c.robots(url)).allowed(url)
        r = await c.fetch(url, timeout=45, settle=6, scroll=1)
        text = re.sub(r"<[^>]+>", " ", r.html)
        xhr = [x for x in r.responses if x[1] in ("XHR", "Fetch", "WebSocket")]
        bad = [x for x in xhr if x[0] >= 400]
        out[name] = {
            "url": url, "robots_allowed": allowed, "robots_rule": rule, "http": r.http_status,
            "final_url": r.final_url, "title": r.title[:100], "error": r.error, "load_ms": round(r.load_ms or 0),
            "html_bytes": len(r.html), "odds_like_numbers": len(ODDS.findall(text)),
            "xhr_total": len(xhr), "xhr_errors": [(s, u[:120]) for s, _, u in bad[:8]],
        }
        if save:
            save.mkdir(parents=True, exist_ok=True)
            (save / f"{name}.html").write_text(r.html, encoding="utf-8")
        print(json.dumps({name: out[name]}, ensure_ascii=False), flush=True)
    await c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", type=Path, default=None)
    a = ap.parse_args()
    asyncio.run(main(a.save))
