"""Oinabet (oinabet.kz) — public prematch line via the site's SignalR feed.

Research (18.11.2026):
  * robots.txt: ``User-agent: * / Allow: /`` (Disallow only for named AI crawlers);
  * the site (Flutter web app) receives its whole line over SignalR at
    ws.oinabet.kz; negotiate + socket work for a plain client without login,
    cookies, CAPTCHA or browser headers (we send our own honest User-Agent);
  * subscription used by the site itself:  subscribe-ext(key, [0, {"Type":"Match",
    "OnlyCustomStakeNames":true, "Sports":[id], "StakesFilter":"MainStake"}]);
  * coefficient encoding: p1 = tenths, p2 = hundredths, p3 = thousandths
    (verified against Ubet for the same match: 1.47/4.55/6.8 vs 1.48/4.62/7.03).
Settlement rules for tennis retirement are not published in a readable form
(Flutter app) → treated as UNKNOWN, so tennis vs Polymarket/Kalshi is blocked.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain import (
    Market,
    MarketType,
    Outcome,
    PlatformType,
    PriceKind,
    Quote,
    RetirementRule,
    SettlementProfile,
    VoidRule,
    utcnow,
)
from app.normalization.names import basic
from app.providers.base import BaseProvider, ProviderState
from app.providers.bookmakers.common.access_gate import AccessGate
from app.providers.bookmakers.oinabet_signalr import SignalRClient
from app.timeutil import aware_or_none

SPORTS = {1: "football", 2: "tennis", 18: "volleyball", 28: "dota2", 201: "cs2"}
MAIN_STAKE = {1: 206138, 2: 949748, 18: 930065, 28: 7075340, 201: 7074488}
THREE_WAY = {206138}
STALE_AFTER_S = 120


def decode_price(bet: dict) -> Decimal | None:
    if bet.get("p1") is not None:
        return Decimal(bet["p1"]) / 10
    if bet.get("p2") is not None:
        return Decimal(bet["p2"]) / 100
    if bet.get("p3") is not None:
        return Decimal(bet["p3"]) / 1000
    return None


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return aware_or_none(d)  # a value without a time zone is not guessed (timezone_unknown)


class LineState:
    """Merged view of all SignalR updates (snapshots + incremental changes)."""

    def __init__(self) -> None:
        self.matches: dict[int, dict] = {}
        self.objects: dict[int, str] = {}
        self.tournaments: dict[int, str] = {}

    def apply(self, part: dict) -> None:
        d = part.get("d") or {}
        for k, v in (d.get("o") or {}).items():
            if v and v.get("n"):
                self.objects[int(k)] = v["n"]
        for k, v in (d.get("to") or {}).items():
            if v and v.get("n"):
                self.tournaments[int(k)] = v["n"]
        for mid in d.get("rm") or []:
            self.matches.pop(int(mid), None)
        for k, rec in (d.get("m") or {}).items():
            mid = int(k)
            cur = self.matches.setdefault(mid, {})
            for f, v in rec.items():
                if v is None:
                    continue
                if f in ("b", "st") and isinstance(v, dict):
                    tgt = cur.setdefault(f, {})
                    for bid, b in v.items():
                        if b is None:
                            tgt.pop(bid, None)
                            continue
                        tb = tgt.setdefault(bid, {})
                        tb.update({x: y for x, y in b.items() if y is not None})
                elif f == "rb" and isinstance(v, list):
                    for bid in v:
                        cur.get("b", {}).pop(str(bid), None)
                        cur.get("b", {}).pop(bid, None)
                else:
                    cur[f] = v


def build_market(m: dict, state: LineState, fetched: datetime) -> Market | None:
    sport = m.get("s")
    cat = SPORTS.get(sport)
    if cat is None or m.get("tm") or m.get("it"):  # tm != null → match is live; skip
        return None
    main = MAIN_STAKE[sport]
    stakes = {str(sid): st for sid, st in (m.get("st") or {}).items()
              if st.get("t") == main and st.get("o", True)
              and (st.get("p") or {}).get("outerMarkerNumber", 0) in (0, 0.0)}
    if not stakes:
        return None
    sid = next(iter(stakes))
    bets = {int(b["k"]): b for b in (m.get("b") or {}).values()
            if str(b.get("s")) == sid and b.get("o", True) and b.get("k") is not None}
    names = [state.objects.get(int(x)) for x in ((m.get("o") or {}).get("1"), (m.get("o") or {}).get("2")) if x]
    if len(names) != 2 or not all(names):
        parts = (m.get("n") or "").split(" - ")
        if len(parts) != 2:
            return None
        names = [p.strip() for p in parts]
    a, b = names
    three = main in THREE_WAY
    layout = [(1, a), (2, "Draw"), (3, b)] if three else [(1, a), (2, b)]
    outs = []
    for k, label in layout:
        bet = bets.get(k)
        price = decode_price(bet) if bet else None
        if price is None or price <= 1:
            return None
        q = Quote(platform="oinabet", kind=PriceKind.DECIMAL_ODDS, outcome_label=label,
                  instrument_id=f"oinabet:{m['i']}:{k}", best_price=price, levels=[], depth_known=False,
                  fetched_at=fetched, fee_params={"unit_step": "1"}, currency="KZT", side="BACK",
                  url="https://oinabet.kz/")
        outs.append(Outcome(basic(label), label, [q]))
    settle = SettlementProfile(
        walkover=VoidRule.REFUND,
        retirement=RetirementRule.UNKNOWN if cat == "tennis" else RetirementRule.ADVANCING_PLAYER,
        draw_possible=three, notes="Oinabet: правила расчёта при отказе игрока не опубликованы в читаемом виде")
    return Market(
        platform="oinabet", platform_type=PlatformType.BOOKMAKER, market_id=f"oinabet:{m['i']}",
        event_id=str(m["i"]), title=f"{a} — {b}", category=cat, market_type=MarketType.MATCH_WINNER,
        outcomes=outs, start_time=_dt(m.get("d")), participants=(a, b),
        tournament=state.tournaments.get(m.get("to"), ""), settlement=settle,
        period="regular_time" if cat == "football" else "match", url="https://oinabet.kz/",
        extra={"match_id": m["i"], "lang": "ru"},
    )


class OinabetKzProvider(BaseProvider):
    name = "oinabet"
    platform_type = PlatformType.BOOKMAKER
    docs_url = "https://oinabet.kz"

    def default_config(self) -> dict:
        return {"sports": list(SPORTS), "max_hours_ahead": 336, "snapshot_wait_s": 20,
                "coverage": ["футбол 1X2", "теннис", "волейбол", "Dota 2", "CS2"],
                "region_note": "Лицензированный оператор РК; публичный поток линии сайта (SignalR)"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stream: SignalRClient | None = None
        self.state = LineState()
        self._lock = asyncio.Lock()
        self._last_msg_wall: datetime | None = None
        self.reconnects = 0

    async def _ensure(self) -> None:
        """Connect + subscribe; reconnects automatically if the stream died or went silent."""
        async with self._lock:
            silent = self.stream and self.stream.last_message_at and \
                time.monotonic() - self.stream.last_message_at > STALE_AFTER_S * 3
            if self.stream is not None and self.stream.connected and not silent:
                return
            if self.stream is not None:
                self.reconnects += 1
                await self.stream.close()
            self.stream = SignalRClient()
            t0 = time.monotonic()
            await self.stream.connect()
            self.status.latency_ms = (time.monotonic() - t0) * 1000
            self.state = LineState()
            for sid in self.cfg["sports"]:
                await self.stream.subscribe(
                    f"sport-{sid}", {"Type": "Match", "OnlyCustomStakeNames": True, "Sports": [sid],
                                     "StakesFilter": "MainStake"}, mode=0)
            deadline = time.monotonic() + float(self.cfg["snapshot_wait_s"])
            keys = [f"sport-{s}" for s in self.cfg["sports"]]
            while time.monotonic() < deadline:
                if all(any((p.get("d") or {}).get("r") for p in self.stream.updates.get(k, [])) for k in keys) \
                        and time.monotonic() - self.stream.last_message_at > 1.5:
                    break
                await asyncio.sleep(0.3)

    def _drain(self) -> None:
        if self.stream is None:
            return
        for key in list(self.stream.updates):
            parts, self.stream.updates[key] = self.stream.updates[key], []
            for p in parts:
                self.state.apply(p)
        if self.stream.last_message_at:
            age = time.monotonic() - self.stream.last_message_at
            self._last_msg_wall = utcnow() - timedelta(seconds=age)

    def _freshness(self) -> datetime:
        # The feed pushes every change; while the stream is alive, prices are current.
        if self.stream and self.stream.connected and self._last_msg_wall and \
                (utcnow() - self._last_msg_wall).total_seconds() < STALE_AFTER_S:
            return utcnow()
        return self._last_msg_wall or utcnow() - timedelta(hours=1)

    _gate: AccessGate | None = None

    async def _page_gate(self) -> bool:
        """Open the public page through Zendriver first; a refused site is not contacted further."""
        self._gate = self._gate or AccessGate("oinabet", "https://oinabet.kz/")
        v = await self._gate.check()
        if not v.ok:
            self.status.state = ProviderState.UNAVAILABLE
            self.status.message = f"публичная страница недоступна — {v.reason}. Не обходим; повтор через 30 мин"
            self.status.last_error = v.reason
        return v.ok

    async def health_check(self):
        if not await self._page_gate():
            return self.status
        try:
            await self._ensure()
            self.status.state = ProviderState.OK
            self.status.message = f"поток активен; переподключений: {self.reconnects}"
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
        return self.status

    def _markets(self) -> list[Market]:
        fetched = self._freshness()
        horizon = utcnow() + timedelta(hours=float(self.cfg["max_hours_ahead"]))
        out = []
        for m in self.state.matches.values():
            mk = build_market(m, self.state, fetched)
            if mk and mk.start_time and utcnow() < mk.start_time <= horizon:
                out.append(mk)
        return out

    async def fetch_markets(self) -> list[Market]:
        try:
            await self._ensure()
        except Exception as e:  # noqa: BLE001
            self.mark_down(e)
            return []
        self._drain()
        markets = self._markets()
        self.mark_ok(len(self.state.matches), len(markets))
        self.status.message = f"поток активен; переподключений: {self.reconnects}"
        return markets

    async def refresh_market(self, market: Market) -> Market:
        await self._ensure()
        await asyncio.sleep(1.0)  # let pending pushes arrive
        self._drain()
        m = self.state.matches.get(int(market.extra["match_id"]))
        fresh = build_market(m, self.state, self._freshness()) if m else None
        if fresh:
            market.outcomes = fresh.outcomes
        else:
            for o in market.outcomes:
                o.quotes = []
        return market

    async def close(self) -> None:
        if self.stream is not None:
            await self.stream.close()
            self.stream = None
        await super().close()
