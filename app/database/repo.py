"""Persistence helpers (async SQLAlchemy 2)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import models as M
from app.domain import utcnow


class Repo:
    def __init__(self, url: str | None = None):
        self.url = url or get_settings().database_url
        if self.url.startswith("sqlite"):
            Path(self.url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(self.url, pool_pre_ping=True)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        """Create tables if missing (dev convenience; production runs `alembic upgrade head`)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(M.Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # ---------------- users / settings -------------------------------
    async def get_or_create_user(self, telegram_id: int, username: str | None = None) -> M.User:
        async with self.session() as s:
            u = (await s.execute(select(M.User).where(M.User.telegram_id == telegram_id))).unique().scalar_one_or_none()
            if u is None:
                u = M.User(telegram_id=telegram_id, username=username)
                s.add(u)
                await s.flush()
                st = M.UserSettings(user_id=u.id)
                st.capital_usd = get_settings().default_capital_usd
                st.min_roi_pct = get_settings().default_min_roi_pct
                st.max_quote_age_s = get_settings().default_max_quote_age_seconds
                s.add(st)
                await s.commit()
                u = (await s.execute(select(M.User).where(M.User.id == u.id))).unique().scalar_one()
            return u

    async def update_settings(self, telegram_id: int, **fields) -> M.UserSettings:
        async with self.session() as s:
            u = (await s.execute(select(M.User).where(M.User.telegram_id == telegram_id))).unique().scalar_one()
            st = (await s.execute(select(M.UserSettings).where(M.UserSettings.user_id == u.id))).scalar_one()
            for k, v in fields.items():
                setattr(st, k, v)
            await s.commit()
            return st

    async def user_count(self) -> int:
        async with self.session() as s:
            return (await s.execute(select(func.count(M.User.id)))).scalar_one()

    async def subscribers(self) -> list[M.User]:
        async with self.session() as s:
            rows = (await s.execute(select(M.User))).unique().scalars().all()
            return [u for u in rows if u.settings and u.settings.notifications]

    # ---------------- scan persistence --------------------------------
    async def save_scan(self, rep, providers: dict) -> None:
        async with self.session() as s:
            for p in providers.values():
                st = p.status
                s.add(M.ProviderStatusRow(
                    provider=p.name, state=st.state.value, message=(st.message or st.last_error or "")[:2000],
                    latency_ms=st.latency_ms, events=st.events, markets=st.markets, errors=st.errors,
                    rate_limited=st.rate_limited,
                ))
            s.add(M.ScanStatistics(
                started_at=rep.started, duration_s=rep.duration_s, markets=rep.total_markets,
                cross_groups=rep.groups_cross, candidates=rep.candidates, verified=len(rep.verified),
                rejected=len(rep.rejected), by_provider=rep.markets_by_provider, errors=rep.provider_errors,
            ))
            await s.commit()
        for opp in rep.verified + rep.rejected:
            await self.save_opportunity(opp)

    async def save_opportunity(self, opp, text: str = "") -> int:
        r = opp.result
        async with self.session() as s:
            row = M.Opportunity(
                signature=opp.signature, kind=opp.kind, title=opp.title[:500], category=opp.category,
                market_type=opp.market_type.value, period=opp.period, platforms=opp.platforms,
                status=opp.status.value, roi_pct=r.roi_pct if r else None,
                min_profit_usd=r.min_profit if r else None, total_cost_usd=r.total_cost if r else None,
                confidence=opp.confidence, scenarios={k: str(v) for k, v in (r.scenarios if r else {}).items()},
                warnings=list(dict.fromkeys(opp.warnings))[:20], start_time=opp.start_time,
                verified_at=opp.verified_at, text=text,
            )
            if r:
                for f in r.legs:
                    q = f.leg.quote
                    row.legs.append(M.ArbitrageLeg(
                        platform=q.platform, instrument_id=q.instrument_id[:250], kind=q.kind.value, side=q.side,
                        outcome_label=q.outcome_label[:250], best_price=q.best_price, avg_price=f.avg_price,
                        units=f.units, stake=f.stake, fee=f.fee, payout=f.payout_if_win, currency=q.currency,
                        url=q.url[:500],
                    ))
                    s.add(M.OddsSnapshot(
                        platform=q.platform, instrument_id=q.instrument_id[:250], outcome_label=q.outcome_label[:250],
                        kind=q.kind.value, side=q.side, price=q.best_price, bid=q.bid_price,
                        size=q.levels[0].size if q.levels else None, fetched_at=q.fetched_at,
                    ))
                    if q.levels:
                        s.add(M.OrderbookRow(platform=q.platform, instrument_id=q.instrument_id[:250],
                                             levels=[[str(lv.price), str(lv.size)] for lv in q.levels[:20]],
                                             fetched_at=q.fetched_at))
            s.add(row)
            await s.flush()
            for st, msg in opp.log:
                s.add(M.ValidationLog(opportunity_id=row.id, status=st, message=msg[:2000]))
            await s.commit()
            opp.db_id = row.id
            return row.id

    # ---------------- notifications -----------------------------------
    async def should_notify(self, user_id: int, signature: str, roi: Decimal, *,
                            cooldown_min: int = 30, roi_jump: Decimal = Decimal("0.5")) -> bool:
        """No duplicate notification for the same leg set within cooldown unless ROI grew notably."""
        since = utcnow() - timedelta(minutes=cooldown_min)
        async with self.session() as s:
            last = (await s.execute(
                select(M.Notification).where(M.Notification.user_id == user_id, M.Notification.signature == signature,
                                             M.Notification.sent_at >= since)
                .order_by(M.Notification.sent_at.desc()).limit(1)
            )).scalar_one_or_none()
            if last is None:
                return True
            return last.roi_pct is not None and roi - last.roi_pct >= roi_jump

    async def notifications_since(self, user_id: int, minutes: int = 60) -> int:
        since = utcnow() - timedelta(minutes=minutes)
        async with self.session() as s:
            return (await s.execute(select(func.count(M.Notification.id)).where(
                M.Notification.user_id == user_id, M.Notification.sent_at >= since))).scalar_one()

    async def record_notification(self, user_id: int, signature: str, roi: Decimal | None,
                                  opportunity_id: int | None, kind: str = "arb") -> None:
        async with self.session() as s:
            s.add(M.Notification(user_id=user_id, signature=signature, roi_pct=roi,
                                 opportunity_id=opportunity_id, kind=kind, sent_at=utcnow()))
            await s.commit()

    # ---------------- queries for the bot -----------------------------
    async def recent_opportunities(self, *, statuses: tuple[str, ...] | None = None, limit: int = 10,
                                   since_hours: int = 24) -> list[M.Opportunity]:
        since = utcnow() - timedelta(hours=since_hours)
        async with self.session() as s:
            q = select(M.Opportunity).options(selectinload(M.Opportunity.legs)).where(M.Opportunity.created_at >= since)
            if statuses:
                q = q.where(M.Opportunity.status.in_(statuses))
            q = q.order_by(M.Opportunity.created_at.desc()).limit(limit)
            return list((await s.execute(q)).scalars().all())

    async def stats(self, hours: int = 24) -> dict:
        since: datetime = utcnow() - timedelta(hours=hours)
        async with self.session() as s:
            scans = (await s.execute(select(func.count(), func.avg(M.ScanStatistics.duration_s),
                                            func.avg(M.ScanStatistics.markets))
                                     .where(M.ScanStatistics.started_at >= since))).one()
            by_status = dict((await s.execute(
                select(M.Opportunity.status, func.count()).where(M.Opportunity.created_at >= since)
                .group_by(M.Opportunity.status))).all())
            best = (await s.execute(select(func.max(M.Opportunity.roi_pct)).where(
                M.Opportunity.created_at >= since, M.Opportunity.status.in_(("VERIFIED", "NOTIFIED"))))).scalar()
            notes = (await s.execute(select(func.count()).select_from(M.Notification)
                                     .where(M.Notification.sent_at >= since))).scalar()
        return {"scans": scans[0], "avg_duration": scans[1], "avg_markets": scans[2],
                "by_status": by_status, "best_roi": best, "notifications": notes}


# ---------------------------------------------------------------- v2: simple UI
PLATFORM_GROUPS = {
    "kz": "🇰🇿 Букмекеры Казахстана",
    "intl": "🌐 Международные букмекеры",
    "polymarket": "🔮 Polymarket",
    "other_pm": "📈 Другие prediction markets",
    "exchanges": "⚖️ Биржи ставок",
}
CATEGORY_GROUPS = {
    "tennis": "🎾 Теннис", "volleyball": "🏐 Волейбол", "esports": "🎮 Киберспорт", "football": "⚽ Футбол",
    "basketball": "🏀 Баскетбол", "politics": "🏛 Политика", "economics": "📈 Экономика",
    "crypto": "🪙 Криптовалюты", "other": "✨ Другие события",
}
KZ_PLATFORMS = {"1xbet", "ubet", "olimpbet", "fonbet", "parimatch", "winline", "tennisi", "oinabet"}


def platform_group(p: str) -> str:
    if p in KZ_PLATFORMS:
        return "kz"
    if p == "polymarket":
        return "polymarket"
    if p == "kalshi":
        return "other_pm"
    if p == "betfair" or p.startswith("betfair_ex") or p == "matchbook":
        return "exchanges"
    return "intl"


def category_group(c: str) -> str:
    if c in ("cs2", "dota2", "lol", "valorant", "sc2", "esports"):
        return "esports"
    if c in ("politics", "geopolitics"):
        return "politics"
    if c in ("economics", "finance"):
        return "economics"
    if c in ("tennis", "volleyball", "football", "basketball", "crypto"):
        return c
    return "other"


def user_groups(st) -> tuple[list[str], list[str]]:
    return (st.platform_groups if st.platform_groups is not None else list(PLATFORM_GROUPS),
            st.categories if st.categories is not None else list(CATEGORY_GROUPS))


async def _paper_add(self, user_id: int, opp, plan, opportunity_id: int | None) -> int:
    from app.presentation.plan import tenge

    theo = tenge(plan.total_kzt * plan.theoretical_roi_pct / 100)
    async with self.session() as s:
        row = M.PaperTrade(
            user_id=user_id, opportunity_id=opportunity_id, signature=opp.signature, title=opp.title[:500],
            capital_kzt=plan.total_kzt, theoretical_profit_kzt=theo, simulated_profit_kzt=plan.profit_kzt,
            roi_pct=plan.roi_pct.quantize(Decimal("0.0001")),
            legs=[{"platform": lp.platform, "outcome": lp.outcome, "price": str(lp.price),
                   "stake_kzt": str(lp.stake_kzt), "payout_kzt": str(lp.payout_kzt)} for lp in plan.legs],
        )
        s.add(row)
        await s.commit()
        return row.id


async def _paper_set(self, user_id: int, trade_id: int, status: str) -> M.PaperTrade | None:
    async with self.session() as s:
        row = (await s.execute(select(M.PaperTrade).where(M.PaperTrade.id == trade_id,
                                                          M.PaperTrade.user_id == user_id))).scalar_one_or_none()
        if row is None:
            return None
        row.status = status
        if status == "confirmed":
            row.confirmed_profit_kzt = row.simulated_profit_kzt
            row.confirmed_at = utcnow()
        await s.commit()
        return row


async def _paper_list(self, user_id: int, limit: int = 15) -> list[M.PaperTrade]:
    async with self.session() as s:
        return list((await s.execute(select(M.PaperTrade).where(M.PaperTrade.user_id == user_id)
                                     .order_by(M.PaperTrade.created_at.desc()).limit(limit))).scalars().all())


async def _paper_summary(self, user_id: int) -> dict:
    rows = await self.paper_list(user_id, limit=100000)
    active = [r for r in rows if r.status != "cancelled"]
    conf = [r for r in active if r.status == "confirmed"]
    return {
        "count": len(active), "confirmed": len(conf),
        "theoretical": sum((r.theoretical_profit_kzt for r in active), Decimal(0)),
        "simulated": sum((r.simulated_profit_kzt for r in active), Decimal(0)),
        "confirmed_profit": sum((r.confirmed_profit_kzt or Decimal(0) for r in conf), Decimal(0)),
        "invested": sum((r.capital_kzt for r in active), Decimal(0)),
    }


async def _overview(self, hours: int = 24) -> dict:
    """Numbers for the dashboard, grouped by signature (one real opportunity = one signature)."""
    since = utcnow() - timedelta(hours=hours)
    async with self.session() as s:
        rows = list((await s.execute(
            select(M.Opportunity).where(M.Opportunity.created_at >= since, M.Opportunity.roi_pct.is_not(None))
            .order_by(M.Opportunity.created_at))).scalars().all())
    by_sig: dict[str, list] = {}
    for r in rows:
        by_sig.setdefault(r.signature, []).append(r)
    found = [v for v in by_sig.values() if any((x.roi_pct or 0) > 0 for x in v)]
    ok = ("VERIFIED", "NOTIFIED")
    verified = [v for v in found if any(x.status in ok for x in v)]
    disappeared = [v for v in verified if v[-1].status not in ok]
    current = [v[-1] for v in verified if v[-1].status in ok]
    rois = [x.roi_pct for v in verified for x in v if x.status in ok and x.roi_pct is not None]
    return {
        "found": len(found), "verified": len(verified), "expired": len(found) - len([v for v in found if v[-1].status in ok]),
        "disappeared": [v[-1] for v in disappeared][-10:],
        "current": current,
        "best_roi": max(rois) if rois else None,
        "avg_roi": (sum(rois) / len(rois)) if rois else None,
    }


Repo.paper_add = _paper_add
Repo.paper_set = _paper_set
Repo.paper_list = _paper_list
Repo.paper_summary = _paper_summary
Repo.overview = _overview


async def _save_bookmaker_events(self, events, report=None) -> int:
    """Persist DOM-collected bookmaker data (events → markets → outcomes → odds snapshots).

    ``events`` are ``ParsedEvent`` objects (app.providers.bookmakers.common.models).
    Events / markets are upserted by (platform, external_id); every call appends
    new odds snapshots with the real ``fetched_at``. Returns the snapshot count.
    """
    n = 0
    async with self.session() as s:
        for ev in events:
            row = (await s.execute(select(M.Event).where(
                M.Event.platform == ev.bookmaker, M.Event.external_id == ev.external_id))).scalar_one_or_none()
            if row is None:
                row = M.Event(platform=ev.bookmaker, external_id=ev.external_id[:128])
                s.add(row)
            row.title, row.category, row.start_time = ev.name[:512], ev.sport[:64], ev.start_time
            await s.flush()
            for pm in ev.markets:
                ext = f"{ev.external_id}:{pm.name}"[:128]
                mrow = (await s.execute(select(M.MarketRow).where(
                    M.MarketRow.platform == ev.bookmaker, M.MarketRow.external_id == ext))).scalar_one_or_none()
                if mrow is None:
                    mrow = M.MarketRow(platform=ev.bookmaker, external_id=ext, event_id=row.id,
                                       market_type=pm.kind, period="match", title=f"{ev.name} — {pm.name}"[:512],
                                       settlement={"source": "dom", "live": ev.is_live})
                    s.add(mrow)
                    await s.flush()
                    for o in pm.outcomes:
                        s.add(M.OutcomeRow(market_id=mrow.id, key=o.name[:255], label=o.name[:255]))
                for o in pm.outcomes:
                    s.add(M.OddsSnapshot(platform=ev.bookmaker, instrument_id=f"{ext}:{o.name}"[:255],
                                         outcome_label=o.name[:255], kind="decimal_odds", side="BACK",
                                         price=o.odds, bid=None, size=None, fetched_at=ev.fetched_at))
                    n += 1
        if report is not None:
            s.add(M.ProviderStatusRow(
                provider=report.bookmaker.lower(), state=report.status.lower(),
                message=(report.reason or report.sample or "")[:2000], latency_ms=report.load_ms,
                events=report.events, markets=report.markets, errors=0 if report.status == "OK" else 1,
                rate_limited=0,
            ))
        await s.commit()
    return n


Repo.save_bookmaker_events = _save_bookmaker_events
