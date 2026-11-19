"""Kazakhstan time helpers. The bot never relies on the server's system time zone.

* DB and domain objects keep timezone-aware UTC;
* everything shown to users (Telegram, reports) is converted to Asia/Almaty;
* naive datetimes coming from the DB (SQLite drops tzinfo) are treated as UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["ALMATY", "TZ_NAME", "UTC", "utc", "almaty", "fmt_almaty", "now_almaty", "aware_or_none",
           "next_aligned_run", "almaty_day_bounds"]

TZ_NAME = "Asia/Almaty"
try:
    ALMATY = ZoneInfo(TZ_NAME)  # IANA rules via zoneinfo; never a hand-written "+5"
except ZoneInfoNotFoundError as e:  # pragma: no cover - deployment error
    raise RuntimeError("нет базы часовых поясов: установите пакет tzdata (pip install tzdata)") from e
UTC = timezone.utc


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def almaty(dt: datetime) -> datetime:
    return utc(dt).astimezone(ALMATY)


def fmt_almaty(dt: datetime | None, pattern: str = "%d.%m %H:%M:%S") -> str:
    return almaty(dt).strftime(pattern) if dt else "—"


def now_almaty() -> datetime:
    return datetime.now(timezone.utc).astimezone(ALMATY)


def aware_or_none(dt: datetime | None) -> datetime | None:
    """Source timestamps: keep them only if they carry a time zone. A naive value is NOT guessed
    (callers mark the event ``timezone_unknown``)."""
    if dt is None or dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC)


def next_aligned_run(now: datetime, interval_s: int) -> datetime:
    """Next scheduled scan time in UTC: runs sit on an Asia/Almaty grid (00:00, +interval, ... of the
    Almaty day), so the schedule follows Kazakhstan's clock whatever the server's time zone is."""
    local = almaty(now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (local - midnight).total_seconds()
    k = int(elapsed // interval_s) + 1
    nxt = midnight + timedelta(seconds=k * interval_s)
    day_end = (midnight + timedelta(days=1)).replace(hour=0)
    if nxt > day_end:  # the grid restarts at Almaty midnight
        nxt = day_end
    return nxt.astimezone(UTC)


def almaty_day_bounds(day_of: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the Almaty calendar day containing ``day_of``, in UTC (for statistics / history)."""
    local = almaty(day_of)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=1)).replace(tzinfo=ALMATY)
    return start.astimezone(UTC), end.astimezone(UTC)
