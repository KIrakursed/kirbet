"""Core domain model shared by all providers and engines.

Everything money-related uses Decimal. Prices of binary contracts are in
dollars per share (0..1), a share pays exactly 1 unit of the platform currency
if its outcome happens.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

D = Decimal
ZERO = D("0")
ONE = D("1")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlatformType(str, enum.Enum):
    BOOKMAKER = "bookmaker"
    PREDICTION_MARKET = "prediction_market"
    EXCHANGE = "exchange"
    AGGREGATOR = "aggregator"


class PriceKind(str, enum.Enum):
    """How a quote must be interpreted. Never mix these without conversion."""

    DECIMAL_ODDS = "decimal_odds"  # bookmaker back odds, stake * odds returned
    BINARY_CONTRACT = "binary_contract"  # YES/NO share price in 0..1, pays 1
    EXCHANGE_BACK = "exchange_back"  # exchange back odds (commission on winnings)
    EXCHANGE_LAY = "exchange_lay"  # exchange lay odds (liability = stake*(odds-1))


class MarketType(str, enum.Enum):
    MATCH_WINNER = "match_winner"  # whole match / series winner
    SET_WINNER = "set_winner"
    MAP_WINNER = "map_winner"
    GAME_WINNER = "game_winner"
    TOTAL = "total"
    HANDICAP = "handicap"
    BINARY_EVENT = "binary_event"  # generic YES/NO (politics, crypto ...)
    MULTI_OUTCOME = "multi_outcome"  # mutually exclusive set (who will win ...)
    OTHER = "other"


class OppStatus(str, enum.Enum):
    NEW = "NEW"
    MATCHING = "MATCHING"
    VALIDATING = "VALIDATING"
    RECHECKING = "RECHECKING"
    VERIFIED = "VERIFIED"
    NOTIFIED = "NOTIFIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    SETTLEMENT_MISMATCH = "SETTLEMENT_MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


class VoidRule(str, enum.Enum):
    """What happens to a position if the event is not decided normally."""

    REFUND = "refund"  # stake returned (bookmakers)
    FIFTY_FIFTY = "50_50"  # each share pays 0.5 (Polymarket)
    FAIR_PRICE = "fair_price"  # settles at an exchange-determined price (Kalshi)
    UNKNOWN = "unknown"


class RetirementRule(str, enum.Enum):
    ADVANCING_PLAYER = "advancing_player"  # player who advances wins
    ADVANCING_AFTER_FIRST_SET = "advancing_after_set1"  # stands only if the 1st set was completed
    VOID = "void"  # bet is void if the match is not completed
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SettlementProfile:
    walkover: VoidRule = VoidRule.UNKNOWN  # cancelled before start / walkover
    retirement: RetirementRule = RetirementRule.UNKNOWN
    draw_possible: bool = False  # e.g. BO2 esports, football 1X2
    notes: str = ""


@dataclass(frozen=True)
class Level:
    price: Decimal  # binary: $/share ; odds kinds: decimal odds
    size: Decimal  # binary: shares ; odds kinds: max stake in platform currency


@dataclass
class Quote:
    """Executable price to *take* a position on a specific outcome."""

    platform: str
    kind: PriceKind
    outcome_label: str
    instrument_id: str  # token id / ticker+side / selection id
    best_price: Decimal
    levels: list[Level] = field(default_factory=list)  # asks sorted best-first
    fetched_at: datetime = field(default_factory=utcnow)
    min_size: Decimal = ZERO  # binary: shares ; odds: min stake
    max_stake: Decimal | None = None  # bookmaker limit if known
    fee_model: str = "none"
    fee_params: dict = field(default_factory=dict)
    currency: str = "USD"
    side: str = "YES"  # YES / NO / BACK / LAY
    url: str = ""
    bid_price: Decimal | None = None  # best price to sell (display only)
    depth_known: bool = True

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.fetched_at).total_seconds()


@dataclass
class Outcome:
    key: str  # canonical key inside the market, e.g. normalized participant
    label: str
    quotes: list[Quote] = field(default_factory=list)


@dataclass
class Market:
    """One market on one platform (e.g. Polymarket moneyline for a match)."""

    platform: str
    platform_type: PlatformType
    market_id: str
    event_id: str
    title: str
    category: str  # tennis / cs2 / politics / crypto ...
    market_type: MarketType
    outcomes: list[Outcome]
    start_time: datetime | None = None
    participants: tuple[str, ...] = ()
    tournament: str = ""
    exhaustive: bool = True  # outcomes cover every possible result
    settlement: SettlementProfile = field(default_factory=SettlementProfile)
    period: str = "match"  # match / set1 / map2 ...
    best_of: int | None = None
    url: str = ""
    rules: str = ""
    restricted_regions: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)


@dataclass
class Leg:
    """One position in an arbitrage combination."""

    quote: Quote
    covers: frozenset[str]  # scenario keys in which this leg pays out
    market: Market | None = None


@dataclass
class LegFill:
    leg: Leg
    units: Decimal  # binary: shares ; odds: payout target / stake (see model)
    stake: Decimal  # money put at risk on this leg (incl. fees) in leg currency
    fee: Decimal
    avg_price: Decimal
    payout_if_win: Decimal
    worst_price: Decimal
    partial: bool = False


@dataclass
class ArbResult:
    legs: list[LegFill]
    scenarios: dict[str, Decimal]  # scenario -> net profit (base currency)
    total_cost: Decimal
    min_profit: Decimal
    roi_pct: Decimal
    payout_units: Decimal
    limited_by: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def is_arb(self) -> bool:
        return self.min_profit > ZERO
