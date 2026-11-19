"""Universal browser provider built on Zendriver (Chrome via CDP).

What it does:
  * launches one Chrome/Chromium per process and reuses it for every page view
    (each view gets its own tab which is closed afterwards);
  * opens *public* pages only, after checking the site's robots.txt;
  * waits for dynamic content (JS predicate polled until true or timeout);
  * returns the rendered DOM (HTML) plus the main document HTTP status, the
    final URL, the load time and a precise reason when the page is not usable.

What it deliberately does NOT do: solve or skip CAPTCHA / Cloudflare / anti-bot
challenges, log in, spoof geolocation, rotate proxies, or call private JSON
APIs directly. If a site blocks us, the block is reported as the result.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("browser.zendriver")

# Markers of pages that ask a human to prove something. We stop on them.
CHALLENGE_MARKERS = (
    ("captcha", re.compile(r"g-recaptcha|h-captcha|hcaptcha|smartcaptcha|captcha-container|turnstile", re.I)),
    ("cloudflare", re.compile(r"Just a moment\.\.\.|cf-browser-verification|challenge-platform|cf-chl-", re.I)),
    ("antibot", re.compile(r"If you are not a bot|Access denied|Доступ запрещ|DDoS-Guard|qrator", re.I)),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- robots
class RobotsRules:
    """robots.txt matcher with Google-style wildcards (``*``, ``$``).

    Longest matching rule wins; on equal length Allow wins (RFC 9309).
    """

    def __init__(self, text: str, agent: str = "*"):
        self.rules: list[tuple[bool, str]] = []
        self._parse(text or "", agent.lower())

    def _parse(self, text: str, agent: str) -> None:
        groups: list[tuple[set[str], list[tuple[bool, str]]]] = []
        agents: set[str] = set()
        rules: list[tuple[bool, str]] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (s.strip() for s in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules))
                    agents, rules = set(), []
                agents.add(v.lower())
                last_was_agent = True
            elif k in ("allow", "disallow"):
                last_was_agent = False
                if v:
                    rules.append((k == "allow", v))
            else:
                last_was_agent = False
        if agents or rules:
            groups.append((agents, rules))
        specific = [r for a, r in groups if agent != "*" and agent in a]
        chosen = specific or [r for a, r in groups if "*" in a]
        for r in chosen:
            self.rules.extend(r)

    @staticmethod
    def _match(pattern: str, path: str) -> bool:
        rx = re.escape(pattern).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        return re.match(rx, path) is not None

    def allowed(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool, str] | None = None
        for allow, pat in self.rules:
            if self._match(pat, path):
                cand = (len(pat), allow, pat)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        if best is None:
            return True, ""
        return best[1], ("Allow: " if best[1] else "Disallow: ") + best[2]


# --------------------------------------------------------------------------- result
@dataclass
class PageResult:
    url: str
    final_url: str = ""
    http_status: int | None = None
    load_ms: float | None = None
    html: str = ""
    title: str = ""
    ready: bool = False  # the wait predicate became true
    error: str = ""  # empty = page is usable
    blocked: str = ""  # robots / captcha / cloudflare / antibot / http_403 ...
    fetched_at: datetime = field(default_factory=utcnow)
    responses: list[tuple[int, str, str]] = field(default_factory=list)  # (status, type, url)
    snapshots: dict[str, str] = field(default_factory=dict)  # DOM after each UI action (see fetch)
    captured: dict[str, list[str]] = field(default_factory=dict)  # bodies of responses the page itself got
    page_timezone: str | None = None  # IANA zone the page's JS actually runs in (verified, not assumed)

    @property
    def ok(self) -> bool:
        return not self.error and self.http_status is not None and self.http_status < 400


# --------------------------------------------------------------------------- client
class ZendriverClient:
    """Shared Chrome session. Safe to use from many adapters concurrently."""

    _instance: ZendriverClient | None = None

    def __init__(self, *, headless: bool = True, lang: str = "ru-RU", max_tabs: int = 2,
                 browser_path: str | None = None, respect_robots: bool = True,
                 timezone_id: str | None = "Asia/Almaty", sandbox: bool = True):
        self.headless = headless
        self.sandbox = sandbox
        self.timezone = timezone_id
        self.lang = lang
        self.browser_path = browser_path
        self.respect_robots = respect_robots
        self._browser = None
        self._start_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_tabs)
        self._robots: dict[str, RobotsRules] = {}
        self.max_rss_mb = 1500  # restart Chrome above this (all Chrome processes together)
        self.restart_every = 300  # page views — long-lived Chrome slowly leaks memory
        self.pages = 0
        self.restarts = 0
        self.open_tabs = 0
        self.last_rss_mb: float | None = None
        self.last_cpu: float | None = None

    @classmethod
    def shared(cls) -> ZendriverClient:
        if cls._instance is None:
            from app.config import get_settings

            s = get_settings()
            cls._instance = cls(headless=s.browser_headless, browser_path=s.browser_path or None,
                                sandbox=not s.browser_no_sandbox)
        return cls._instance

    # ---- lifecycle ----------------------------------------------------------
    async def start(self):
        async with self._start_lock:
            if self._browser is not None and not getattr(self._browser, "stopped", False):
                return self._browser
            import zendriver as zd

            t0 = time.monotonic()
            self._browser = await zd.start(headless=self.headless, lang=self.lang, sandbox=self.sandbox,
                                           browser_executable_path=self.browser_path)
            log.info("chrome started in %.0f ms", (time.monotonic() - t0) * 1000)
            return self._browser

    # ---- health ------------------------------------------------------------------
    def _tree(self):
        pid = getattr(self._browser, "_process_pid", None) if self._browser is not None else None
        if not pid:
            return []
        try:
            import psutil

            root = psutil.Process(pid)
            return [root, *root.children(recursive=True)]
        except Exception:  # noqa: BLE001
            return []

    def health(self) -> dict:
        procs = self._tree()
        rss = cpu = 0.0
        for pr in procs:
            try:
                rss += pr.memory_info().rss
                cpu += pr.cpu_percent(interval=None)
            except Exception:  # noqa: BLE001
                pass
        self.last_rss_mb = round(rss / 2**20, 1) if procs else None
        self.last_cpu = round(cpu, 1) if procs else None
        return {"running": bool(procs), "processes": len(procs), "rss_mb": self.last_rss_mb,
                "cpu_percent": self.last_cpu, "open_tabs": self.open_tabs, "pages": self.pages,
                "restarts": self.restarts}

    async def _maybe_recycle(self) -> None:
        """Restart Chrome when it grows too big or after many page views (only when no tab is open)."""
        if self._browser is None or self.open_tabs:
            return
        h = self.health()
        too_big = h["rss_mb"] is not None and h["rss_mb"] > self.max_rss_mb
        if too_big or (self.pages and self.pages % self.restart_every == 0):
            log.info("recycling Chrome (rss=%s MB, pages=%d)", h["rss_mb"], self.pages)
            await self.close_browser()
            self.restarts += 1

    async def close_browser(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("browser stop: %s", e)
            self._browser = None

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.stop()
            except Exception as e:  # noqa: BLE001
                log.debug("browser stop: %s", e)
            self._browser = None
        if ZendriverClient._instance is self:
            ZendriverClient._instance = None

    # ---- robots -------------------------------------------------------------
    async def robots(self, url: str) -> RobotsRules:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            text = ""
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                    r = await c.get(origin + "/robots.txt")
                    if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                        text = r.text
            except httpx.HTTPError as e:
                log.warning("robots.txt %s: %s", origin, e)
            self._robots[origin] = RobotsRules(text)
        return self._robots[origin]

    async def check_robots(self, url: str) -> str:
        """Return '' if allowed, otherwise the blocking rule."""
        if not self.respect_robots:
            return ""
        ok, rule = (await self.robots(url)).allowed(url)
        return "" if ok else rule

    # ---- page view ----------------------------------------------------------
    async def fetch(self, url: str, *, ready_js: str | None = None, timeout: float = 30.0,
                    settle: float = 1.0, scroll: int = 0,
                    actions: list[tuple[str, str]] | None = None,
                    capture: list[str] | None = None) -> PageResult:
        """Open ``url`` in a fresh tab like a normal visitor and return the rendered DOM.

        ``ready_js`` — JS expression that returns truthy once dynamic content is present.
        ``scroll`` — how many times to scroll the page down (lazy lists render on scroll).
        ``actions`` — optional ``[(label, js)]`` ordinary UI actions (e.g. clicking a sport tab);
        after each one the DOM is captured into ``result.snapshots[label]``.
        ``capture`` — URL substrings: bodies of HTTP 200 responses *the page itself loaded* are kept in
        ``result.captured[substring]`` (read from the browser, the endpoint is never called by us);
        the page counts as ready once every substring was captured.
        """
        res = PageResult(url=url)
        rule = await self.check_robots(url)
        if rule:
            res.blocked, res.error = "robots", f"robots.txt запрещает URL ({rule})"
            log.info("%s blocked by robots: %s", url, rule)
            return res

        from zendriver import cdp

        async with self._sem:
            t0 = time.monotonic()
            tab = None
            try:
                browser = await self.start()
                tab = await browser.get("about:blank", new_tab=True)
                self.open_tabs += 1
                doc_status: dict = {}

                def on_response(ev: cdp.network.ResponseReceived) -> None:
                    r = ev.response
                    typ = str(getattr(ev.type_, "value", ev.type_))
                    res.responses.append((r.status, typ, r.url[:200]))
                    if typ == "Document" and ev.frame_id is not None and "main" not in doc_status:
                        # the first Document response after redirects is the main frame
                        if r.status not in (301, 302, 303, 307, 308):
                            doc_status["main"] = r.status
                            doc_status["url"] = r.url

                pending: dict = {}

                async def on_capture(ev: cdp.network.ResponseReceived) -> None:
                    for sub in capture or []:
                        if sub in ev.response.url and ev.response.status == 200:
                            pending[ev.request_id] = sub

                async def on_finished(ev: cdp.network.LoadingFinished) -> None:
                    sub = pending.pop(ev.request_id, None)
                    if sub is None:
                        return
                    try:
                        body, b64 = await tab.send(cdp.network.get_response_body(ev.request_id))
                        if b64:
                            body = base64.b64decode(body).decode("utf-8", "replace")
                        res.captured.setdefault(sub, []).append(body)
                    except Exception as e:  # noqa: BLE001
                        log.debug("response body %s: %s", sub, e)

                tab.add_handler(cdp.network.ResponseReceived, on_response)
                if capture:
                    tab.add_handler(cdp.network.ResponseReceived, on_capture)
                    tab.add_handler(cdp.network.LoadingFinished, on_finished)
                await tab.send(cdp.network.enable())
                if self.timezone:  # deterministic rendering of match times (display only)
                    try:
                        await tab.send(cdp.emulation.set_timezone_override(self.timezone))
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.wait_for(tab.get(url), timeout=timeout)
                res.http_status = doc_status.get("main")

                async def wait_ready(limit: float) -> bool:
                    if not ready_js and not capture:
                        return True
                    end = time.monotonic() + limit
                    while time.monotonic() < end:
                        if capture and all(c in res.captured for c in capture):
                            return True
                        if not ready_js:
                            await asyncio.sleep(0.5)
                            continue
                        try:
                            if await tab.evaluate(f"!!({ready_js})"):
                                return True
                        except Exception:  # noqa: BLE001  (page still navigating)
                            pass
                        await asyncio.sleep(0.5)
                    return False

                async def scroll_down() -> None:
                    for _ in range(scroll):
                        await tab.evaluate("(document.scrollingElement||document.documentElement)"
                                           " && window.scrollBy(0, (document.scrollingElement||document.documentElement).scrollHeight)")
                        await asyncio.sleep(0.8)

                res.ready = await wait_ready(timeout)
                await scroll_down()
                await asyncio.sleep(settle)
                res.html = await tab.get_content()
                for label, js in actions or []:
                    try:
                        await tab.evaluate(js)
                        await asyncio.sleep(1.0)
                        await wait_ready(min(timeout, 15))
                        await scroll_down()
                        await asyncio.sleep(settle)
                        res.snapshots[label] = await tab.get_content()
                    except Exception as e:  # noqa: BLE001
                        log.warning("%s: action %s failed: %s", url, label, e)
                res.title = (await tab.evaluate("document.title")) or ""
                try:  # the zone the page renders local times in; parsers rely on it being Asia/Almaty
                    res.page_timezone = await tab.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
                except Exception:  # noqa: BLE001
                    res.page_timezone = None
                res.final_url = (await tab.evaluate("location.href")) or doc_status.get("url", url)
                if res.http_status is None:
                    res.http_status = doc_status.get("main")
                # a redirect may land on a URL robots.txt forbids — then the content is not used
                rule = await self.check_robots(res.final_url) if res.final_url != url else ""
                if rule:
                    res.html, res.blocked, res.snapshots, res.captured = "", "robots", {}, {}
                    res.error = f"сайт перенаправляет на {res.final_url}, robots.txt запрещает его ({rule})"
            except TimeoutError:
                res.error = f"тайм-аут загрузки страницы ({timeout:.0f} c)"
            except Exception as e:  # noqa: BLE001
                res.error = f"ошибка браузера: {type(e).__name__}: {e}"[:300]
                # a broken CDP connection → restart Chrome on next fetch
                if self._browser is not None and getattr(self._browser, "stopped", False):
                    self._browser = None
            finally:
                res.load_ms = (time.monotonic() - t0) * 1000
                if tab is not None:
                    self.open_tabs -= 1
                    try:
                        await tab.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.pages += 1
                if self.open_tabs == 0:
                    await self._maybe_recycle()

        self._classify(res)
        log.info("GET %s → HTTP %s, %.0f ms, %d bytes%s", url, res.http_status, res.load_ms or 0,
                 len(res.html), f" [{res.blocked or res.error}]" if res.error else "")
        return res

    @staticmethod
    def _classify(res: PageResult) -> None:
        if res.error:
            return
        if res.http_status is not None and res.http_status >= 400:
            res.blocked = f"http_{res.http_status}"
            res.error = f"HTTP {res.http_status}" + (" (доступ запрещён / антибот)" if res.http_status == 403 else "")
        head = res.html[:200_000]
        for name, rx in CHALLENGE_MARKERS:
            if rx.search(head) and (res.error or len(re.sub(r"<[^>]+>", "", head)) < 20_000):
                res.blocked = res.blocked or name
                res.error = res.error or f"страница требует прохождения проверки ({name}) — не обходим"
                break
        if res.final_url and urlsplit(res.final_url).path.rstrip("/").endswith(("/login", "/auth")):
            res.blocked, res.error = "auth", "страница требует авторизации"
        if not res.error and not res.ready and res.html and res.http_status:
            # page opened but the awaited content never appeared — parser decides what that means
            pass
