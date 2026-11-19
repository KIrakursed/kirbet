"""Why were there no surebets? Read-only analysis of one Asia/Almaty day.

    python scripts/why_no_arbs.py [YYYY-MM-DD]     (default: yesterday in Almaty)

Prints: candidates by status, those involving KZ bookmakers, the most common rejection reasons,
and the best (highest ROI) unique candidates with the reason they were not confirmed.
"""

from __future__ import annotations

import asyncio
import collections
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import app.database.repo_ext  # noqa: E402,F401
from app.database import models as M  # noqa: E402
from app.database.repo import Repo  # noqa: E402
from app.timeutil import ALMATY, now_almaty, utc  # noqa: E402

KZ = {"fonbet", "olimpbet", "winline", "ubet", "1xbet", "parimatch", "oinabet", "tennisi"}


async def main(day: date) -> None:
    start = datetime(day.year, day.month, day.day, tzinfo=ALMATY)
    end = start + timedelta(days=1)
    repo = Repo()
    async with repo.session() as s:
        rows = (await s.execute(select(M.Opportunity).where(M.Opportunity.created_at >= utc(start),
                                                             M.Opportunity.created_at < utc(end)))).scalars().all()
        logs = (await s.execute(select(M.ValidationLog).where(M.ValidationLog.created_at >= utc(start),
                                                               M.ValidationLog.created_at < utc(end)))).scalars().all()
    print(f"day {day} (Asia/Almaty): candidates {len(rows)}")
    print("by status:", dict(collections.Counter(o.status for o in rows)))
    kz = [o for o in rows if set(o.platforms) & KZ]
    print(f"with a KZ bookmaker: {len(kz)} {dict(collections.Counter(o.status for o in kz))}")
    kz_only = [o for o in rows if set(o.platforms) <= KZ]
    print(f"KZ bookmakers only: {len(kz_only)} {dict(collections.Counter(o.status for o in kz_only))}")
    last_msg = {}
    for lg in logs:
        last_msg[lg.opportunity_id] = lg.message
    reasons = collections.Counter((o.status, (last_msg.get(o.id) or "")[:110]) for o in rows)
    print("\nmost common reasons:")
    for (st, msg), n in reasons.most_common(12):
        print(f"  {n:4} {st:20} {msg}")
    uniq: dict = {}
    for o in rows:
        k = (o.title, tuple(sorted(o.platforms)))
        if k not in uniq or (o.roi_pct or 0) > (uniq[k].roi_pct or 0):
            uniq[k] = o
    print(f"\nunique candidates: {len(uniq)} — best by calculated ROI:")
    for (t, p), o in sorted(uniq.items(), key=lambda kv: -float(kv[1].roi_pct or 0))[:15]:
        print(f"  {float(o.roi_pct or 0):7.2f}% {o.status:20} {t[:42]:42} {','.join(p)[:40]:40} "
              f"{(last_msg.get(o.id) or '')[:70]}")
    await repo.close()


if __name__ == "__main__":
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else (now_almaty() - timedelta(days=1)).date()
    asyncio.run(main(d))
