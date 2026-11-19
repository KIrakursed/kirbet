from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NUM = Numeric(20, 8)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings: Mapped[UserSettings] = relationship(back_populates="user", uselist=False, lazy="joined")


class UserSettings(Base):
    __tablename__ = "user_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    min_roi_pct: Mapped[Decimal] = mapped_column(NUM, default=Decimal("5"))
    capital_usd: Mapped[Decimal] = mapped_column(NUM, default=Decimal("1000"))
    min_liquidity_usd: Mapped[Decimal] = mapped_column(NUM, default=Decimal("20"))
    max_quote_age_s: Mapped[int] = mapped_column(Integer, default=20)
    platforms: Mapped[list] = mapped_column(JSON, default=lambda: ["polymarket", "kalshi", "the_odds_api", "betfair"])
    sports: Mapped[list] = mapped_column(JSON, default=lambda: ["tennis", "cs2", "dota2", "lol", "valorant", "volleyball", "sc2"])
    pm_categories: Mapped[list] = mapped_column(JSON, default=lambda: ["politics", "crypto", "economics", "finance", "tech", "culture", "science"])
    # v2 simple-UI settings (amounts in tenge, grouped filters)
    capital_kzt: Mapped[Decimal] = mapped_column(NUM, default=Decimal("100000"), server_default="100000")
    min_liquidity_kzt: Mapped[Decimal] = mapped_column(NUM, default=Decimal("0"), server_default="0")
    platform_groups: Mapped[list] = mapped_column(
        JSON, nullable=True, default=lambda: ["kz", "intl", "polymarket", "other_pm", "exchanges"])
    categories: Mapped[list] = mapped_column(
        JSON, nullable=True, default=lambda: ["tennis", "volleyball", "esports", "football", "basketball", "politics",
                               "economics", "crypto", "other"])
    user: Mapped[User] = relationship(back_populates="settings")


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    platform_type: Mapped[str] = mapped_column(String(32))
    docs_url: Mapped[str] = mapped_column(String(255), default="")
    requires_key: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # DISABLED by admin


class ProviderStatusRow(Base):
    __tablename__ = "provider_status"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[float | None] = mapped_column(Float)
    events: Mapped[int] = mapped_column(Integer, default=0)
    markets: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    rate_limited: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class CanonicalEvent(Base):
    __tablename__ = "canonical_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), unique=True)
    sport: Mapped[str] = mapped_column(String(32))
    tournament: Mapped[str] = mapped_column(String(255), default="")
    participant_a: Mapped[str] = mapped_column(String(255), default="")
    participant_b: Mapped[str] = mapped_column(String(255), default="")
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    category: Mapped[str] = mapped_column(String(64))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canonical_event_id: Mapped[int | None] = mapped_column(ForeignKey("canonical_events.id"))
    match_confidence: Mapped[float | None] = mapped_column(Float)


class MarketRow(Base):
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("platform", "external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    market_type: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(16), default="match")
    title: Mapped[str] = mapped_column(String(512))
    rules: Mapped[str] = mapped_column(Text, default="")
    settlement: Mapped[dict] = mapped_column(JSON, default=dict)


class OutcomeRow(Base):
    __tablename__ = "outcomes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(255))


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (Index("ix_odds_snapshots_platform_fetched", "platform", "fetched_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), index=True)
    instrument_id: Mapped[str] = mapped_column(String(255), index=True)
    outcome_label: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[Decimal] = mapped_column(NUM)
    bid: Mapped[Decimal | None] = mapped_column(NUM)
    size: Mapped[Decimal | None] = mapped_column(NUM)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OrderbookRow(Base):
    __tablename__ = "orderbooks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[str] = mapped_column(String(255), index=True)
    levels: Mapped[list] = mapped_column(JSON)  # [[price, size], ...] asks best-first
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Opportunity(Base):
    __tablename__ = "arbitrage_opportunities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signature: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512))
    category: Mapped[str] = mapped_column(String(64), index=True)
    market_type: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(64))  # e.g. "regular_time|total=2.5"
    platforms: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True)
    roi_pct: Mapped[Decimal | None] = mapped_column(NUM)
    min_profit_usd: Mapped[Decimal | None] = mapped_column(NUM)
    total_cost_usd: Mapped[Decimal | None] = mapped_column(NUM)
    confidence: Mapped[float] = mapped_column(Float)
    scenarios: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text, default="")
    legs: Mapped[list[ArbitrageLeg]] = relationship(back_populates="opportunity", cascade="all, delete-orphan")


class ArbitrageLeg(Base):
    __tablename__ = "arbitrage_legs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("arbitrage_opportunities.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    outcome_label: Mapped[str] = mapped_column(String(255))
    best_price: Mapped[Decimal] = mapped_column(NUM)
    avg_price: Mapped[Decimal] = mapped_column(NUM)
    units: Mapped[Decimal] = mapped_column(NUM)
    stake: Mapped[Decimal] = mapped_column(NUM)
    fee: Mapped[Decimal] = mapped_column(NUM)
    payout: Mapped[Decimal] = mapped_column(NUM)
    currency: Mapped[str] = mapped_column(String(8))
    url: Mapped[str] = mapped_column(String(512), default="")
    opportunity: Mapped[Opportunity] = relationship(back_populates="legs")


class ValidationLog(Base):
    __tablename__ = "validation_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("arbitrage_opportunities.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("arbitrage_opportunities.id", ondelete="SET NULL"))
    signature: Mapped[str] = mapped_column(String(32), index=True)
    roi_pct: Mapped[Decimal | None] = mapped_column(NUM)
    kind: Mapped[str] = mapped_column(String(16), default="arb")  # arb / provider_down
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ScanStatistics(Base):
    __tablename__ = "scan_statistics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_s: Mapped[float] = mapped_column(Float)
    markets: Mapped[int] = mapped_column(Integer)
    cross_groups: Mapped[int] = mapped_column(Integer)
    candidates: Mapped[int] = mapped_column(Integer)
    verified: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[int] = mapped_column(Integer)
    by_provider: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[dict] = mapped_column(JSON, default=dict)


class PaperTrade(Base):
    """Simulated bet set. Never real money: status simulated / confirmed (user says both bets were placed)."""

    __tablename__ = "paper_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("arbitrage_opportunities.id", ondelete="SET NULL"))
    signature: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    capital_kzt: Mapped[Decimal] = mapped_column(NUM)
    theoretical_profit_kzt: Mapped[Decimal] = mapped_column(NUM)  # best prices, no fees, no limits
    simulated_profit_kzt: Mapped[Decimal] = mapped_column(NUM)  # after fees, depth, rounding (worst case)
    confirmed_profit_kzt: Mapped[Decimal | None] = mapped_column(NUM)  # entered/confirmed by the user
    roi_pct: Mapped[Decimal] = mapped_column(NUM)
    legs: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="simulated")  # simulated / confirmed / cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



# ======================================================================= commercial layer
class Plan(Base):
    """Tariff. Prices and limits are data (editable by the admin), not code."""

    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)  # free / basic / pro / vip
    title: Mapped[str] = mapped_column(String(64))
    price_stars: Mapped[int] = mapped_column(Integer, default=0)  # Telegram Stars per period (0 = free)
    price_kzt: Mapped[int] = mapped_column(Integer, default=0)  # shown for reference / future KZT gateway
    period_days: Mapped[int] = mapped_column(Integer, default=30)
    scans_per_day: Mapped[int] = mapped_column(Integer, default=5)
    notifications_per_day: Mapped[int] = mapped_column(Integer, default=3)
    history_days: Mapped[int] = mapped_column(Integer, default=1)
    filters: Mapped[bool] = mapped_column(Boolean, default=False)  # own bookmaker / sport filters
    all_markets: Mapped[bool] = mapped_column(Boolean, default=False)  # beyond 1X2 / winner
    details: Mapped[bool] = mapped_column(Boolean, default=False)  # detailed card & statistics
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_code: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)  # active / expired / cancelled / refunded
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(16))  # payment / admin / trial
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"))
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("provider", "provider_charge_id", name="uq_payment_provider_charge"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_code: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))  # telegram_stars / test (tests only)
    invoice_payload: Mapped[str] = mapped_column(String(128), unique=True)  # our id sent with the invoice
    provider_charge_id: Mapped[str | None] = mapped_column(String(128))  # set only by the provider's callback
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))  # XTR (Stars) / KZT
    status: Mapped[str] = mapped_column(String(16), index=True)  # created / paid / refunded / failed / expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class UsageEvent(Base):
    """One counted action (scan request, notification) — the source of truth for plan limits."""

    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_user_kind_at", "user_id", "kind", "at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # scan / notify
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class MatchedEvent(Base):
    """A cross-bookmaker match the engine accepted, with its confidence and the reasons."""

    __tablename__ = "matched_events"
    __table_args__ = (Index("ix_matched_events_seen", "last_seen"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    sport: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    market_type: Mapped[str] = mapped_column(String(32))
    period: Mapped[str] = mapped_column(String(64))
    platforms: Mapped[list] = mapped_column(JSON)
    market_ids: Mapped[list] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
