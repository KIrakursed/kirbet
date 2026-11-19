"""Command line entry points.

    python -m app.cli providers      # health of every source
    python -m app.cli scan [--capital 1000] [--min-roi 0]   # one live scan, prints results
    python -m app.cli bot            # run Telegram bot + periodic scanner
    python -m app.cli initdb         # create tables (dev; prod uses alembic)
    python -m app.cli status         # every source: WORKING / PARTIAL / BLOCKED / UNAVAILABLE / DISABLED
    python -m app.cli health         # DB, migration, Telegram API, Chrome, disk, RAM, service (exit code = failures)
    python -m app.cli stats          # users, subscriptions, payments, scans, matches, opportunities
    python -m app.cli bookmakers [--only winline,fonbet] [--persist] [--json f.json]
                                     # Zendriver/DOM adapters: real odds check + table
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from app.config import get_settings


class UtcAlmatyFormatter(logging.Formatter):
    """Every log line carries UTC and Asia/Almaty time, independent of the server's time zone."""

    def format(self, record: logging.LogRecord) -> str:
        from datetime import datetime, timezone

        from app.timeutil import ALMATY

        t = datetime.fromtimestamp(record.created, timezone.utc)
        record.utc = t.strftime("%Y-%m-%d %H:%M:%S")
        record.almaty = t.astimezone(ALMATY).strftime("%H:%M:%S")
        return super().format(record)


def _setup_logging() -> None:
    h = logging.StreamHandler()
    h.setFormatter(UtcAlmatyFormatter("%(utc)sZ (ALA %(almaty)s) %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=get_settings().log_level, handlers=[h])
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def cmd_providers() -> None:
    from app.providers.registry import build_providers

    provs = build_providers()
    for p in provs.values():
        await p.health_check()
        s = p.status
        print(f"{p.name:14} {s.state.value:12} {s.platform_type.value:18} "
              f"{(s.latency_ms or 0):6.0f}ms  {s.message or s.region_note}")
        await p.close()


async def cmd_scan(capital: Decimal, min_roi: Decimal, show: int, persist: bool) -> None:
    from app.providers.registry import build_providers
    from app.scheduler.scanner import Scanner
    from app.telegram.formatting import format_opportunity, format_scan_report
    from app.validation.engine import ValidationSettings

    provs = build_providers()
    vs = ValidationSettings(capital_usd=capital, min_roi_pct=min_roi,
                            max_quote_age_s=get_settings().default_max_quote_age_seconds)
    scanner = Scanner(provs, vs)
    rep = await scanner.run()
    from app.monitoring.fx import load_fx

    fx = await load_fx()
    print(format_scan_report(rep, html=False))
    print("\nПримеры сопоставленных кросс-платформенных рынков:")
    for line in rep.cross_groups_sample:
        print("  ", line)
    print("\nБлиже всего к вилке (S = цена 1$ гарантированной выплаты с комиссиями; вилка при S < 1):")
    for s_val, desc in rep.closest:
        print(f"   S={s_val:.4f}  {desc}")
    print("\n=== VERIFIED ===")
    for o in rep.verified[:show]:
        print(format_opportunity(o, fx, html=False))
        print("-" * 60)
    print("\n=== Отклонённые (топ) ===")
    for o in rep.rejected[:show]:
        r = o.result
        print(f"[{o.status.value}] {o.kind} {o.title[:70]} | {', '.join(o.platforms)} | "
              f"ROI {r.roi_pct if r else '-'}% | {o.log[-1][1][:160] if o.log else ''}")
    if persist:
        from app.database.repo import Repo

        repo = Repo()
        await repo.init()
        await repo.save_scan(rep, provs)
    for p in provs.values():
        await p.close()
    from app.providers.browser.zendriver_client import ZendriverClient

    await ZendriverClient.shared().close()


async def cmd_bookmakers(only: set[str] | None, persist: bool, json_path: str | None) -> None:
    from app.providers.bookmakers.collect import LAST_METRICS, dump_json, run_all, table

    results = await run_all(only)
    print(table([r for r, _ in results], LAST_METRICS))
    for name, m in LAST_METRICS.items():
        print(f"{name}: событий {m.get('events')} (уникальных {m.get('unique_events')}, prematch {m.get('prematch')},"
              f" live {m.get('live')}), рынков {m.get('markets')} ({m.get('markets_per_event')}/событие), "
              f"коэффициентов {m.get('odds')}, успешных загрузок {m.get('success_rate')}, "
              f"средняя загрузка {m.get('avg_load_ms')} мс, виды спорта {m.get('sports')}")
    if json_path:
        dump_json(results, json_path)
        print(f"\nJSON: {json_path}")
    if persist:
        from app.database.repo import Repo

        repo = Repo()
        await repo.init()
        n = 0
        for rep, events in results:
            n += await repo.save_bookmaker_events(events, rep)
        await repo.close()
        print(f"\nСохранено снимков коэффициентов: {n} ({repo.url.split('://')[0]})")


def main(argv: list[str] | None = None) -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    _setup_logging()
    ap = argparse.ArgumentParser(prog="arb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("providers")
    sc = sub.add_parser("scan")
    sc.add_argument("--capital", type=Decimal, default=get_settings().default_capital_usd)
    sc.add_argument("--min-roi", type=Decimal, default=get_settings().default_min_roi_pct)
    sc.add_argument("--show", type=int, default=10)
    sc.add_argument("--persist", action="store_true")
    sub.add_parser("bot")
    sub.add_parser("initdb")
    for c in ("status", "health", "stats"):
        sub.add_parser(c)
    bk = sub.add_parser("bookmakers")
    bk.add_argument("--only", default="")
    bk.add_argument("--persist", action="store_true")
    bk.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "providers":
        asyncio.run(cmd_providers())
    elif a.cmd == "scan":
        asyncio.run(cmd_scan(a.capital, a.min_roi, a.show, a.persist))
    elif a.cmd == "bot":
        from app.main import run

        asyncio.run(run())
    elif a.cmd == "bookmakers":
        only = {x.strip().lower() for x in a.only.split(",") if x.strip()} or None
        asyncio.run(cmd_bookmakers(only, a.persist, a.json))
    elif a.cmd in ("status", "health", "stats"):
        from app.ops import run

        sys.exit(run(a.cmd))
    elif a.cmd == "initdb":
        from app.database.repo import Repo

        asyncio.run(Repo().init())
        print("ok")


if __name__ == "__main__":
    main()
