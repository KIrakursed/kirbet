"""Repo helpers for the commercial layer, admin panel, matching log and retention."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import delete, or_, select, update

from app.database import models as M
from app.database.repo import Repo
from app.domain import utcnow


async def _touch_user(self, user_id: int) -> None:
    """last_seen_at, written at most every 5 minutes per user (cheap on a busy public bot)."""
    now = utcnow()
    async with self.session() as s:
        await s.execute(update(M.User).where(M.User.id == user_id, or_(
            M.User.last_seen_at.is_(None), M.User.last_seen_at < now - timedelta(minutes=5))).values(last_seen_at=now))
        await s.commit()


async def _audit(self, actor: int | None, action: str, target: str = "", **details) -> None:
    async with self.session() as s:
        s.add(M.AuditLog(actor_telegram_id=actor, action=action, target=target, details=details))
        await s.commit()


async def _list_users(self, limit: int = 30) -> list[M.User]:
    async with self.session() as s:
        return list((await s.execute(select(M.User).order_by(M.User.id.desc()).limit(limit))).unique().scalars())


async def _get_user(self, telegram_id: int) -> M.User | None:
    async with self.session() as s:
        return (await s.execute(select(M.User).where(M.User.telegram_id == telegram_id))).unique().scalar_one_or_none()


async def _get_user_by_id(self, user_id: int) -> M.User | None:
    async with self.session() as s:
        return (await s.execute(select(M.User).where(M.User.id == user_id))).unique().scalar_one_or_none()


async def _recent_scans(self, limit: int = 10) -> list[M.ScanStatistics]:
    async with self.session() as s:
        return list((await s.execute(select(M.ScanStatistics).order_by(M.ScanStatistics.id.desc()).limit(limit)))
                    .scalars())


async def _disabled_sources(self) -> set[str]:
    async with self.session() as s:
        return {p.name for p in (await s.execute(select(M.Provider).where(M.Provider.admin_disabled.is_(True))))
                .scalars()}


async def _set_source_disabled(self, name: str, disabled: bool, platform_type: str = "") -> None:
    async with self.session() as s:
        row = (await s.execute(select(M.Provider).where(M.Provider.name == name))).scalar_one_or_none()
        if row is None:
            row = M.Provider(name=name, platform_type=platform_type or "bookmaker")
            s.add(row)
        row.admin_disabled = disabled
        await s.commit()


async def _save_matches(self, groups, seen_at) -> int:
    """Upsert accepted cross-platform matches with their confidence and reasons (matched_events)."""
    n = 0
    async with self.session() as s:
        for g in groups:
            ids = sorted(m.market_id for m in g.markets)
            key = hashlib.sha1("|".join(ids).encode()).hexdigest()[:40]
            row = (await s.execute(select(M.MatchedEvent).where(M.MatchedEvent.key == key))).scalar_one_or_none()
            m0 = g.markets[0]
            if row is None:
                s.add(M.MatchedEvent(key=key, sport=m0.category[:32], title=m0.title[:512],
                                     market_type=m0.market_type.value, period=m0.period[:64],
                                     platforms=sorted({m.platform for m in g.markets}), market_ids=ids,
                                     confidence=g.confidence, reasons=list(g.reasons)[:10], start_time=m0.start_time,
                                     first_seen=seen_at, last_seen=seen_at))
            else:
                row.last_seen, row.confidence, row.reasons = seen_at, g.confidence, list(g.reasons)[:10]
            n += 1
        await s.commit()
    return n


async def _cleanup(self, *, odds_days: int = 7, status_days: int = 14, opp_days: int = 60,
                   usage_days: int = 120, match_days: int = 14) -> dict[str, int]:
    """Retention for high-volume rows. Users, payments, subscriptions and audit logs are never deleted."""
    now = utcnow()
    out: dict[str, int] = {}
    async with self.session() as s:
        for name, model, col, days in (
                ("odds_snapshots", M.OddsSnapshot, M.OddsSnapshot.fetched_at, odds_days),
                ("provider_status", M.ProviderStatusRow, M.ProviderStatusRow.checked_at, status_days),
                ("usage_events", M.UsageEvent, M.UsageEvent.at, usage_days),
                ("matched_events", M.MatchedEvent, M.MatchedEvent.last_seen, match_days)):
            r = await s.execute(delete(model).where(col < now - timedelta(days=days)))
            out[name] = r.rowcount or 0
        old = select(M.Opportunity.id).where(M.Opportunity.created_at < now - timedelta(days=opp_days),
                                             M.Opportunity.status.notin_(("VERIFIED", "NOTIFIED")))
        r = await s.execute(delete(M.Opportunity).where(M.Opportunity.id.in_(old)))
        out["arbitrage_opportunities"] = r.rowcount or 0
        await s.commit()
    return out


Repo.touch_user = _touch_user
Repo.audit = _audit
Repo.list_users = _list_users
Repo.get_user = _get_user
Repo.get_user_by_id = _get_user_by_id
Repo.recent_scans = _recent_scans
Repo.disabled_sources = _disabled_sources
Repo.set_source_disabled = _set_source_disabled
Repo.save_matches = _save_matches
Repo.cleanup = _cleanup
