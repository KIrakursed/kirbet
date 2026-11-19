"""Operational diagnostics for the CLI: status / health / stats (read-only, safe to run any time).

    python -m app.cli status      # every source: public status, events, markets, last update (from the DB)
    python -m app.cli health      # DB, migrations, Telegram API, Chrome, disk, RAM, the bot service
    python -m app.cli stats       # users, subscriptions, payments, scans, matches, opportunities
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import timedelta

from sqlalchemy import func, select, text

import app.database.repo_ext  # noqa: F401
from app.config import ROOT, get_settings
from app.database import models as M
from app.database.repo import Repo
from app.domain import utcnow
from app.timeutil import fmt_almaty, utc


def _ok(flag: bool) -> str:
    return "OK  " if flag else "FAIL"


async def status() -> None:
    repo = Repo()
    async with repo.session() as s:
        rows = (await s.execute(select(M.ProviderStatusRow).order_by(M.ProviderStatusRow.id.desc()).limit(400))) \
            .scalars().all()
        disabled = await repo.disabled_sources()
    latest = {}
    for r in rows:
        latest.setdefault(r.provider, r)
    state_map = {"ok": "WORKING", "degraded": "DEGRADED", "unavailable": "BLOCKED", "blocked": "BLOCKED",
                 "needs_key": "UNAVAILABLE", "down": "UNAVAILABLE", "error": "UNAVAILABLE", "disabled": "DISABLED"}
    print(f"{'источник':14} {'статус':12} {'событий':>8} {'рынков':>8}  проверено (Алматы)  причина")
    for name, r in sorted(latest.items()):
        st = "DISABLED" if name in disabled else state_map.get(r.state, r.state.upper())
        if st == "WORKING" and (r.message or "").startswith("часть страниц"):
            st = "PARTIAL"
        age = utcnow() - utc(r.checked_at)
        stale = " (устарело)" if age > timedelta(minutes=30) else ""
        msg = "" if st == "WORKING" else (r.message or "")[:110]
        print(f"{name:14} {st:12} {r.events:>8} {r.markets:>8}  {fmt_almaty(r.checked_at, '%d.%m %H:%M:%S')}{stale}  {msg}")
    await repo.close()


async def health() -> int:
    s = get_settings()
    bad = 0
    repo = Repo()
    try:
        async with repo.engine.connect() as c:
            await c.execute(text("select 1"))
            rev = (await c.execute(text("select version_num from alembic_version"))).scalar()
        print(f"{_ok(True)} база данных: {repo.url.split('://')[0]}, миграция {rev}")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"{_ok(False)} база данных: {e}")
    async with repo.session() as ss:
        last = (await ss.execute(select(func.max(M.ScanStatistics.started_at)))).scalar()
    fresh = last is not None and utcnow() - utc(last) < timedelta(minutes=10)
    bad += not fresh
    print(f"{_ok(fresh)} последний скан: {fmt_almaty(last)} (Алматы)")
    await repo.close()
    if s.telegram_bot_token:
        try:
            from aiogram import Bot

            b = Bot(s.telegram_bot_token)
            me = await b.get_me()
            await b.session.close()
            print(f"{_ok(True)} Telegram API: @{me.username}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"{_ok(False)} Telegram API: {type(e).__name__}")  # never print the token
    else:
        bad += 1
        print(f"{_ok(False)} Telegram: TELEGRAM_BOT_TOKEN не задан")
    chrome = shutil.which("google-chrome-stable") or shutil.which("chromium") or s.browser_path
    print(f"{_ok(bool(chrome))} Chrome: {chrome or 'не найден'}")
    bad += not chrome
    du = shutil.disk_usage(ROOT)
    disk_ok = du.free / du.total > 0.1
    bad += not disk_ok
    print(f"{_ok(disk_ok)} диск: свободно {du.free // 2**30} из {du.total // 2**30} ГБ")
    try:
        import psutil

        vm = psutil.virtual_memory()
        print(f"{_ok(vm.percent < 90)} RAM: занято {vm.used // 2**20} из {vm.total // 2**20} МБ ({vm.percent}%)")
        bad += vm.percent >= 90
    except ImportError:
        pass
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "is-active", "arb-bot"], capture_output=True, text=True, check=False)
        active = r.stdout.strip() == "active"
        print(f"{_ok(active)} служба arb-bot: {r.stdout.strip() or r.stderr.strip()}")
        bad += not active
    print(f"платежи: {'ВКЛЮЧЕНЫ' if s.payments_enabled else 'выключены'} · админов: "
          f"{len([x for x in s.bot_admin_ids.split(',') if x.strip()])} · PIN: {'задан' if s.admin_pin else 'не задан'}")
    return bad


async def stats() -> None:
    repo = Repo()
    now = utcnow()
    async with repo.session() as s:
        q = lambda model: select(func.count(model.id))  # noqa: E731
        users = (await s.execute(q(M.User))).scalar_one()
        new24 = (await s.execute(q(M.User).where(M.User.created_at >= now - timedelta(hours=24)))).scalar_one()
        active_subs = dict((await s.execute(select(M.Subscription.plan_code, func.count(M.Subscription.id))
                                            .where(M.Subscription.status == "active", M.Subscription.ends_at > now)
                                            .group_by(M.Subscription.plan_code))).all())
        pays = dict((await s.execute(select(M.Payment.status, func.count(M.Payment.id)).group_by(M.Payment.status))).all())
        scans = (await s.execute(q(M.ScanStatistics).where(M.ScanStatistics.started_at >= now - timedelta(hours=24)))) \
            .scalar_one()
        avg = (await s.execute(select(func.avg(M.ScanStatistics.duration_s))
                               .where(M.ScanStatistics.started_at >= now - timedelta(hours=24)))).scalar()
        matched = (await s.execute(q(M.MatchedEvent).where(M.MatchedEvent.last_seen >= now - timedelta(hours=1)))) \
            .scalar_one()
        opps = dict((await s.execute(select(M.Opportunity.status, func.count(M.Opportunity.id))
                                     .where(M.Opportunity.created_at >= now - timedelta(hours=24))
                                     .group_by(M.Opportunity.status))).all())
        snaps = (await s.execute(q(M.OddsSnapshot).where(M.OddsSnapshot.fetched_at >= now - timedelta(hours=24)))) \
            .scalar_one()
    print(f"пользователей: {users} (новых за 24 ч: {new24})")
    print(f"активные подписки: {active_subs or '—'} · платежи: {pays or '—'}")
    print(f"сканов за 24 ч: {scans}, средняя длительность {avg or 0:.0f} с")
    print(f"сопоставленных событий (за час): {matched} · снимков коэффициентов за 24 ч: {snaps}")
    print(f"возможности за 24 ч по статусам: {opps or '—'}")
    await repo.close()


def run(cmd: str) -> int:
    if cmd == "status":
        asyncio.run(status())
    elif cmd == "health":
        return asyncio.run(health())
    elif cmd == "stats":
        asyncio.run(stats())
    return 0
