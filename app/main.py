"""Application service: periodic scanner + Telegram notifications."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

import app.database.repo_ext  # noqa: F401  (registers Repo helpers)
from app.arbitrage.finder import Opportunity
from app.billing import Billing
from app.config import get_settings
from app.database.repo import Repo, category_group, platform_group, user_groups
from app.domain import OppStatus, utcnow
from app.monitoring.fx import load_fx
from app.presentation.plan import build_plan
from app.providers.base import ProviderState
from app.providers.registry import DATA_PROVIDERS, build_providers
from app.scheduler.scanner import Scanner
from app.timeutil import next_aligned_run
from app.validation.engine import ValidationSettings

log = logging.getLogger("service")
NOTIFY_PER_HOUR = 10
OK = (OppStatus.VERIFIED, OppStatus.NOTIFIED)


class Service:
    def __init__(self, repo: Repo | None = None):
        s = get_settings()
        self.repo = repo or Repo()
        self.billing = Billing(self.repo)
        self.providers = build_providers()
        self.vs = ValidationSettings(capital_usd=s.default_capital_usd, min_roi_pct=Decimal("0"),
                                     max_quote_age_s=float(s.default_max_quote_age_seconds))
        self.scanner = Scanner(self.providers, self.vs)
        self.fx = None
        self.bot = None
        self.live: dict[str, Opportunity] = {}  # signature -> latest opportunity (for buttons)
        self.gone: list[tuple[datetime, str, Decimal, str]] = []
        self._prev_state: dict[str, ProviderState] = {}
        self._scan_lock = asyncio.Lock()
        self._last_rep = None
        self._last_rep_at = 0.0
        self.last_success_at = None
        self.last_error = ""
        self.counters = {"scans": 0, "cache_hits": 0, "waited": 0, "errors": 0, "notified": 0}
        self.started_at = utcnow()
        self.user_count: int | None = None

    # ------------------------------------------------------------ scanning
    def _fresh(self, max_age: float):
        if max_age > 0 and self._last_rep is not None and time.monotonic() - self._last_rep_at < max_age:
            return self._last_rep
        return None

    async def scan_now(self, max_age: float = 0.0):
        return (await self.scan_shared(max_age))[0]

    async def scan_shared(self, max_age: float = 0.0):
        """Run a scan → ``(report, from_cache)``.

        With ``max_age`` (bot button) a *successful* scan finished less than ``max_age`` seconds ago is
        shared instead — the bot is public, so many users must not each trigger a full scan (bookmaker
        page views, paid Odds API credits). Concurrent callers wait for the one running scan. A failed
        scan raises and is never replaced by older data presented as fresh."""
        if (rep := self._fresh(max_age)) is not None:
            self.counters["cache_hits"] += 1
            return rep, True
        if self._scan_lock.locked():
            self.counters["waited"] += 1
        async with self._scan_lock:
            if (rep := self._fresh(max_age)) is not None:  # another user's scan just finished
                self.counters["cache_hits"] += 1
                return rep, True
            self.counters["scans"] += 1
            try:
                self.fx = await load_fx()
                rep = await self.scanner.run()
            except Exception as e:
                self.counters["errors"] += 1
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                raise
            await self.repo.save_scan(rep, self.providers)
            await self._save_bookmaker_snapshots()
            try:
                await self.repo.save_matches(self.scanner.last_groups, utcnow())  # confidence + reasons
            except Exception as e:  # noqa: BLE001
                log.warning("matched_events not saved: %s", e)
            self._update_live(rep)
            self._last_rep, self._last_rep_at = rep, time.monotonic()
            self.last_success_at = utcnow()
            return rep, False

    async def set_source_enabled(self, name: str, enabled: bool, *, actor: int | None = None) -> None:
        p = self.providers[name]
        p.cfg["enabled"] = enabled
        p.admin_disabled = not enabled
        await self.repo.set_source_disabled(name, not enabled, p.platform_type.value)
        await self.repo.audit(actor, "source.enable" if enabled else "source.disable", name)

    async def load_admin_state(self) -> None:
        await self.billing.ensure_plans()
        for name in await self.repo.disabled_sources():
            if name in self.providers:
                self.providers[name].cfg["enabled"] = False
                self.providers[name].admin_disabled = True

    async def daily_jobs(self) -> None:
        """Once per Asia/Almaty day: expire subscriptions and apply data retention."""
        from app.timeutil import now_almaty

        day = now_almaty().date()
        if getattr(self, "_jobs_day", None) == day:
            return
        self._jobs_day = day
        try:
            n = await self.billing.expire_due()
            removed = await self.repo.cleanup()
            log.info("daily jobs (%s Almaty): expired subscriptions=%d, cleanup=%s", day, n, removed)
        except Exception:  # noqa: BLE001
            log.exception("daily jobs failed")

    async def subscription_reminders(self) -> None:
        if self.bot is None:
            return
        from app.timeutil import fmt_almaty

        for tg, sub in await self.billing.due_reminders(3):
            try:
                await self.bot.send_message(tg, f"⏰ Подписка {sub.plan_code.upper()} заканчивается "
                                                f"{fmt_almaty(sub.ends_at, '%d.%m.%Y %H:%M')} (Алматы). Продлить: /subscribe")
            except Exception as e:  # noqa: BLE001
                log.info("reminder to %s failed: %s", tg, e)

    async def _save_bookmaker_snapshots(self) -> None:
        for p in self.providers.values():
            if callable(getattr(p, "pop_unsaved", None)):  # DOM adapters and Ubet
                fresh = p.pop_unsaved()
                if fresh:
                    try:
                        await self.repo.save_bookmaker_events(*fresh)
                    except Exception as e:  # noqa: BLE001
                        log.warning("не удалось сохранить снимки %s: %s", p.name, e)

    def _update_live(self, rep) -> None:
        now_ok = {o.signature: o for o in rep.verified}
        for sig, old in list(self.live.items()):
            if old.status in OK and sig not in now_ok:
                newer = next((o for o in rep.rejected if o.signature == sig), None)
                reason = newer.log[-1][1] if newer and newer.log else "цены изменились — вилка не найдена"
                roi = old.result.roi_pct if old.result else Decimal(0)
                self.gone.append((utcnow(), old.title, roi, reason))
                self.gone = self.gone[-50:]
                del self.live[sig]
        self.live.update(now_ok)
        if len(self.live) > 500:
            for k in list(self.live)[:-500]:
                del self.live[k]

    def current_opps(self) -> list[Opportunity]:
        return [o for o in self.live.values() if o.status in OK]

    def sources_working(self) -> tuple[int, int]:
        provs = [self.providers[n] for n in DATA_PROVIDERS if n in self.providers]
        ok = sum(1 for p in provs if p.status.state in (ProviderState.OK, ProviderState.DEGRADED))
        return ok, len(provs)

    def available_platforms(self) -> set[str]:
        plats = set()
        rep = self.scanner.last_report
        for p in self.providers.values():
            if p.status.state in (ProviderState.OK, ProviderState.DEGRADED) and p.name != "the_odds_api":
                plats.add(p.name)
        if rep:
            for o in rep.verified + rep.rejected:
                plats.update(o.platforms)
        return plats

    def max_volume_kzt(self, opp: Opportunity) -> Decimal:
        plan = build_plan(opp, self.fx, Decimal("1000"), with_max_volume=True)
        return plan.max_volume_kzt if plan and plan.max_volume_kzt else Decimal(0)

    # ------------------------------------------------------------ per-user
    def filter_for_user(self, opps: list[Opportunity], st) -> list[Opportunity]:
        plats, cats = user_groups(st)
        out = []
        for o in opps:
            if o.status not in OK or o.result is None:
                continue
            if any(platform_group(p) not in plats for p in o.platforms):
                continue
            if category_group(o.category) not in cats:
                continue
            if o.max_quote_age > float(st.max_quote_age_s) + 120:  # verified prices, small grace for menus
                continue
            plan = build_plan(o, self.fx, st.capital_kzt) if self.fx else None
            if plan is None or plan.profit_kzt <= 0:
                continue
            if plan.roi_pct < Decimal(st.min_roi_pct):
                continue
            if st.min_liquidity_kzt and self.max_volume_kzt(o) < Decimal(st.min_liquidity_kzt):
                continue
            out.append(o)
        return out

    # ------------------------------------------------------------ notifications
    async def notify(self, rep) -> int:
        if self.bot is None:
            return 0
        from app.presentation.messages import simple_card
        from app.telegram.bot import card_kb

        sent = 0
        for u in await self.repo.subscribers():
            st = u.settings
            if u.is_blocked:
                continue
            budget = NOTIFY_PER_HOUR - await self.repo.notifications_since(u.id, 60)
            for o in self.filter_for_user(rep.verified, st):
                if budget <= 0:  # public bot: at most NOTIFY_PER_HOUR alerts per user per hour
                    break
                plan = build_plan(o, self.fx, st.capital_kzt)
                if plan is None or plan.profit_kzt <= 0:
                    continue
                if not await self.repo.should_notify(u.id, o.signature, plan.roi_pct):
                    continue
                if not (await self.billing.consume(u.id, "notify")).allowed:
                    break  # daily notification quota of the user's plan
                text = simple_card(o, plan)
                try:
                    await self.bot.send_message(u.telegram_id, text, reply_markup=card_kb(o.signature, plan, st.capital_kzt))
                except TelegramForbiddenError:  # the user blocked the bot: stop sending to them
                    log.info("user %s blocked the bot, notifications off", u.telegram_id)
                    await self.repo.update_settings(u.telegram_id, notifications=False)
                    break
                except TelegramRetryAfter as e:  # Telegram flood control
                    await asyncio.sleep(min(float(e.retry_after), 30))
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("send failed to %s: %s", u.telegram_id, e)
                    continue
                budget -= 1
                await asyncio.sleep(0.05)  # stay far below Telegram's ~30 msg/s global limit
                o.set(OppStatus.NOTIFIED, f"user {u.telegram_id}")
                oid = await self.repo.save_opportunity(o, text)
                await self.repo.record_notification(u.id, o.signature, plan.roi_pct, oid)
                sent += 1
        return sent

    async def provider_alerts(self) -> None:
        if self.bot is None:
            return
        msgs = []
        for name in DATA_PROVIDERS:
            p = self.providers[name]
            prev, cur = self._prev_state.get(name), p.status.state
            self._prev_state[name] = cur
            if prev == ProviderState.OK and cur == ProviderState.DOWN:
                msgs.append(f"🔴 Площадка <b>{name}</b> перестала отвечать. Её цены не используются, пока она не восстановится.")
            elif prev == ProviderState.DOWN and cur == ProviderState.OK:
                msgs.append(f"🟢 Площадка <b>{name}</b> снова работает.")
        for t in msgs:  # public bot: source up/down goes to the admins only (users see /bookmakers)
            await self.alert_admins(t)

    async def alert_admins(self, text: str) -> None:
        """Critical / operational alerts to BOT_ADMIN_IDS (rate-limited: same text at most every 30 min)."""
        if self.bot is None:
            return
        import time as _t

        from app.telegram.commerce import admin_ids

        sent = getattr(self, "_alert_sent", {})
        self._alert_sent = sent
        key = text[:120]
        if _t.monotonic() - sent.get(key, -1e9) < 1800:
            return
        sent[key] = _t.monotonic()
        for aid in admin_ids():
            try:
                await self.bot.send_message(aid, "🛠 " + text)
            except Exception as e:  # noqa: BLE001
                log.warning("admin alert to %s failed: %s", aid, e)

    async def resource_check(self) -> None:
        import psutil

        rss = psutil.Process().memory_info().rss / 2**20
        vm = psutil.virtual_memory()
        if rss > 1500 or vm.percent > 90:
            await self.alert_admins(f"Нагрузка: бот {rss:.0f} МБ, RAM сервера {vm.percent:.0f}%")

    async def loop(self) -> None:
        interval = get_settings().scan_interval_seconds
        while True:
            try:
                rep = await self.scan_now()
                await self.provider_alerts()
                n = await self.notify(rep)
                self.counters["notified"] += n
                await self.daily_jobs()
                await self.subscription_reminders()
                self.user_count = await self.repo.user_count()
                log.info("load: users=%s scans=%d cache_hits=%d waited=%d errors=%d", self.user_count,
                         self.counters["scans"], self.counters["cache_hits"], self.counters["waited"],
                         self.counters["errors"])
                log.info("cycle done: groups=%d kz_groups=%d candidates=%d verified=%d rejected_opps=%d notified=%d "
                         "filtered_matches=%s", rep.groups_cross, rep.kz_groups, rep.candidates, len(rep.verified),
                         len(rep.rejected), n, rep.rejected_matches)
            except Exception as e:  # noqa: BLE001
                log.exception("scan cycle failed")
                self._fails = getattr(self, "_fails", 0) + 1
                if self._fails >= 3:
                    await self.alert_admins(f"Сканирование падает {self._fails} раз подряд: {type(e).__name__}: {e}"[:300])
            else:
                self._fails = 0
            try:
                await self.resource_check()
            except Exception:  # noqa: BLE001
                pass
            # next run on the Asia/Almaty grid (not the server's clock / zone)
            wait = (next_aligned_run(utcnow(), interval) - utcnow()).total_seconds()
            await asyncio.sleep(max(1.0, wait))


async def run() -> None:
    from app.telegram.bot import run_bot

    s = get_settings()
    service = Service()
    await service.repo.init()
    await service.load_admin_state()
    from app.providers.browser.zendriver_client import ZendriverClient

    try:
        if not s.telegram_bot_token:
            log.error("TELEGRAM_BOT_TOKEN не задан — запускаю только сканер (без уведомлений)")
            await service.loop()
            return
        await asyncio.gather(run_bot(service), service.loop())
    finally:
        await ZendriverClient.shared().close()  # the Chrome shared by DOM bookmaker adapters
