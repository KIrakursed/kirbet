"""Copy every row from the SQLite database to an (already migrated, empty) PostgreSQL database.

    python scripts/migrate_sqlite_to_postgres.py SQLITE_URL POSTGRES_URL [--force]

* the target must be at the same Alembic revision (run `alembic upgrade head` against it first);
* tables are copied parent → child (FK order), in batches, inside one transaction per table;
* naive SQLite datetimes are written as UTC; id sequences are moved past the copied ids;
* refuses to write into a non-empty target unless --force; the SQLite file is never modified.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import DateTime, func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.database.models import Base  # noqa: E402

BATCH = 2000


def fix(row: dict, table) -> dict:
    out = dict(row)
    for c in table.columns:
        v = out.get(c.name)
        if isinstance(c.type, DateTime) and isinstance(v, datetime) and v.tzinfo is None:
            out[c.name] = v.replace(tzinfo=timezone.utc)
    return out


async def main(src_url: str, dst_url: str, force: bool) -> None:
    src = create_async_engine(src_url)
    dst = create_async_engine(dst_url)
    async with src.connect() as a, dst.connect() as b:
        rev_a = (await a.execute(text("select version_num from alembic_version"))).scalar()
        rev_b = (await b.execute(text("select version_num from alembic_version"))).scalar()
        if rev_a != rev_b:
            raise SystemExit(f"revision differs: sqlite {rev_a} vs postgres {rev_b} — run alembic upgrade head")
        if not force:
            for t in Base.metadata.sorted_tables:
                if (await b.execute(select(func.count()).select_from(t))).scalar():
                    raise SystemExit(f"target table {t.name} is not empty (use --force to append)")
    total = {}
    for t in Base.metadata.sorted_tables:
        async with src.connect() as a:
            rows = [fix(dict(r._mapping), t) for r in (await a.execute(select(t))).all()]
        async with dst.begin() as b:
            for i in range(0, len(rows), BATCH):
                await b.execute(t.insert(), rows[i:i + BATCH])
            if "id" in t.c and rows:
                await b.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}', 'id'), "
                                     f"(SELECT MAX(id) FROM {t.name}))"))
        total[t.name] = len(rows)
        print(f"{t.name}: {len(rows)}", flush=True)
    async with src.connect() as a, dst.connect() as b:
        for t in Base.metadata.sorted_tables:
            na = (await a.execute(select(func.count()).select_from(t))).scalar()
            nb = (await b.execute(select(func.count()).select_from(t))).scalar()
            if nb < na:
                raise SystemExit(f"verification failed for {t.name}: sqlite {na}, postgres {nb}")
    print("verified: every table has at least as many rows in PostgreSQL as in SQLite")
    await src.dispose()
    await dst.dispose()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(main(args[0], args[1], "--force" in sys.argv))
