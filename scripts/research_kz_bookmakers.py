"""Technical research of Kazakhstan bookmaker sites (one normal page view each).

For every site: robots.txt, a single headless page load like a regular visitor,
a list of JSON / WebSocket traffic the page itself makes, and challenge detection.
No CAPTCHA solving, no login, no stealth/evasion: if a site blocks us, we record it.

    python scripts/research_kz_bookmakers.py  ->  data/research_kz.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

SITES = {
    "1xbet": "https://1xbet.kz/ru/line/tennis",
    "olimpbet": "https://olimpbet.kz/",
    "fonbet": "https://fonbet.kz/sports/tennis",
    "parimatch": "https://parimatch.kz/ru/",
    "winline": "https://winline.kz/",
    "tennisi": "https://tennisi.kz/",
    "ubet": "https://ubet.kz/",
    "oinabet": "https://oinabet.kz/",
}
CHALLENGE = ("just a moment", "captcha", "cf-chl", "access denied", "attention required", "ddos-guard",
             "checking your browser", "verify you are human")
OUT = Path(__file__).resolve().parents[1] / "data" / "research_kz.json"


def robots_disallows(robots: str, path: str) -> bool:
    agent_all, rules = False, []
    for line in robots.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            agent_all = v == "*"
        elif agent_all and k == "disallow" and v:
            rules.append(v)
    for r in rules:
        pat = r.replace("*", "")
        if r == "/" or (pat and path.startswith(pat)):
            return True
    return False


async def research(name: str, url: str, browser) -> dict:
    host = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    res: dict = {"site": name, "url": url}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        try:
            r = await c.get(f"{host}/robots.txt")
            res["robots_status"] = r.status_code
            res["robots"] = r.text[:3000] if r.status_code == 200 else ""
        except httpx.HTTPError as e:
            res["robots_error"] = str(e)
    if res.get("robots") and robots_disallows(res["robots"], "/"):
        res["skipped"] = "robots.txt: Disallow: / — автоматический доступ запрещён, страница не загружалась"
        return res
    ctx = await browser.new_context(locale="ru-RU")
    page = await ctx.new_page()
    json_hits, ws = [], []

    async def on_response(resp):
        ct = resp.headers.get("content-type", "")
        if "json" in ct and len(json_hits) < 80:
            try:
                body = await resp.body()
            except Exception:  # noqa: BLE001
                body = b""
            json_hits.append({"url": resp.url[:300], "status": resp.status, "bytes": len(body),
                              "sample": body[:300].decode("utf-8", "replace")})

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
    page.on("websocket", lambda w: ws.append(w.url[:300]))
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        res["status"] = resp.status if resp else None
        res["final_url"] = page.url
        await page.wait_for_timeout(15000)
        title = (await page.title()) or ""
        text = (await page.inner_text("body"))[:4000]
        res["title"] = title
        low = (title + " " + text).lower()
        res["challenge"] = [w for w in CHALLENGE if w in low]
        res["text_sample"] = text[:800]
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"[:400]
    await asyncio.sleep(1)
    res["json_responses"] = json_hits
    res["websockets"] = ws
    await ctx.close()
    return res


async def main(only: list[str]) -> None:
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for name, url in SITES.items():
            if only and name not in only:
                continue
            r = await research(name, url, browser)
            out.append(r)
            print(f"{name:10} status={r.get('status')} challenge={r.get('challenge')} json={len(r.get('json_responses', []))} "
                  f"ws={len(r.get('websockets', []))} {r.get('skipped') or r.get('error') or r.get('title', '')[:60]}")
        await browser.close()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(sys.argv[1:]))
