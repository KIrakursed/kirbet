"""Find each bookmaker's rules / terms pages and search them for automated-access clauses.

    python scripts/research_kz_terms.py  ->  data/research_kz_terms.json
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SITES = {
    "1xbet": "https://1xbet.kz/ru",
    "olimpbet": "https://olimpbet.kz/",
    "parimatch": "https://parimatch.kz/ru/",
    "winline": "https://winline.kz/",
    "tennisi": "https://tennisi.kz/",
    "ubet": "https://ubet.kz/",
    "oinabet": "https://oinabet.kz/",
}
LINK_RE = re.compile(r"(правил|соглашен|услови|terms|rules|оферт)", re.I)
CLAUSE_RE = re.compile(
    r"[^.\n]{0,250}(автоматизир|программн\w+ средств|бот\b|ботов|робот|скрипт|парс|scrap|crawl|"
    r"automated|копировать|копирование|извлеч|сбор\w* (данных|информации)|коммерческ\w+ использ)[^.\n]{0,250}",
    re.I,
)
OUT = Path(__file__).resolve().parents[1] / "data" / "research_kz_terms.json"


async def main() -> None:
    res = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        for name, url in SITES.items():
            ctx = await b.new_context(locale="ru-RU")
            page = await ctx.new_page()
            info = {"links": [], "clauses": []}
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(8000)
                links = await page.eval_on_selector_all(
                    "a", "els => els.map(e => [e.innerText.trim().slice(0,80), e.href])")
                cand = []
                for text, href in links:
                    if href and (LINK_RE.search(text or "") or LINK_RE.search(href)) and href not in cand:
                        cand.append(href)
                info["links"] = cand[:8]
                for href in cand[:5]:
                    try:
                        await page.goto(href, wait_until="domcontentloaded", timeout=45000)
                        await page.wait_for_timeout(5000)
                        txt = await page.inner_text("body", timeout=15000)
                    except Exception as e:  # noqa: BLE001
                        info.setdefault("errors", []).append(f"{href}: {e}"[:200])
                        continue
                    for m in CLAUSE_RE.finditer(txt):
                        info["clauses"].append({"page": href, "text": m.group(0).strip()[:500]})
            except Exception as e:  # noqa: BLE001
                info["error"] = str(e)[:300]
            res[name] = info
            print(f"{name:10} links={len(info['links'])} clauses={len(info['clauses'])} {info.get('error', '')[:80]}")
            await ctx.close()
        await b.close()
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
