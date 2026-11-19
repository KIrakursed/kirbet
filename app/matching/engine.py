"""Event / market matching engine.

Two markets are merged only when *structured* data agrees:
  * same sport family,
  * same market type and period (match vs set vs map),
  * compatible best-of format when both are known,
  * both participants match (fuzzy+aliases) in either order,
  * start times within the tolerance window.
A similar title is never enough on its own.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field

from app.domain import Market, RetirementRule, SettlementProfile, VoidRule
from app.normalization.names import basic, name_variants, similarity, sport_family

PERSON_SPORTS = {"tennis"}

# Why candidate pairs were *not* merged — monitoring of filtered false matches (reset per scan).
REJECTED: Counter = Counter()


def _reject(reason: str) -> None:
    REJECTED[reason] += 1


@dataclass
class MatchResult:
    a: Market
    b: Market
    confidence: float
    outcome_map: dict[str, str]  # a.outcome.key -> b.outcome.key
    reasons: list[str] = field(default_factory=list)


@dataclass
class MarketGroup:
    """Same real-world market on several platforms, with unified outcome keys."""

    markets: list[Market]
    confidence: float
    outcome_keys: list[str]
    key_maps: dict[str, dict[str, str]]  # platform market_id -> {local key: unified}
    reasons: list[str] = field(default_factory=list)


def _time_ok(a: Market, b: Market, tol_hours: float) -> tuple[bool, float]:
    if a.start_time is None or b.start_time is None:
        return True, 0.9  # unknown time lowers confidence
    diff = abs((a.start_time - b.start_time).total_seconds()) / 3600
    if diff > tol_hours:
        return False, 0.0
    return True, 1.0 if diff <= 3 else 0.97


def match_markets(a: Market, b: Market, *, tol_hours: float = 30.0) -> MatchResult | None:
    reasons: list[str] = []
    fam_a, fam_b = sport_family(a.category), sport_family(b.category)
    if fam_a != fam_b:
        return None
    if a.market_type != b.market_type:
        return None
    if a.period != b.period:
        return None
    if a.best_of and b.best_of and a.best_of != b.best_of:
        return None
    if len(a.participants) != 2 or len(b.participants) != 2:
        return None
    if a.extra.get("timezone_unknown") or b.extra.get("timezone_unknown"):
        _reject("timezone_unknown")  # an unverified kick-off time must not decide a match
        return None
    ok, tconf = _time_ok(a, b, tol_hours)
    if not ok:
        _reject("start_time")
        return None
    person = fam_a in PERSON_SPORTS
    alt_a = a.extra.get("alt_participants") or (None, None)
    alt_b = b.extra.get("alt_participants") or (None, None)
    va = [name_variants(a.participants[i], alt_a[i]) for i in (0, 1)]
    vb = [name_variants(b.participants[i], alt_b[i]) for i in (0, 1)]

    def sim(xs: list[str], ys: list[str]) -> float:
        return max(similarity(x, y, person=person) for x in xs for y in ys)

    straight = min(sim(va[0], vb[0]), sim(va[1], vb[1]))
    swapped = min(sim(va[0], vb[1]), sim(va[1], vb[0]))
    pconf = max(straight, swapped)
    if pconf < 0.85:
        if max(sim(va[0], vb[0]), sim(va[1], vb[1]), sim(va[0], vb[1]), sim(va[1], vb[0])) >= 0.85:
            _reject("one_participant_only")  # e.g. same home team, different opponent
        return None
    # guard: participants of one side must not match each other ambiguously
    if sim(va[0], va[1]) > 0.85:
        _reject("ambiguous_sides")
        return None
    reasons.append(f"участники {pconf:.2f}, время {tconf:.2f}")
    pairing = {0: 0, 1: 1} if straight >= swapped else {0: 1, 1: 0}

    def side_index(m: Market, label: str) -> int | None:
        """Which participant an outcome label refers to (None = draw/other)."""
        if label in m.participants:
            return m.participants.index(label)
        lb = basic(label)
        for i, p in enumerate(m.participants):
            if basic(p) == lb:
                return i
        return None

    omap: dict[str, str] = {}
    for oa in a.outcomes:
        ia = side_index(a, oa.label)
        target = None
        if ia is not None:
            ib = pairing[ia]
            target = next((ob for ob in b.outcomes if side_index(b, ob.label) == ib), None)
        else:
            target = next((ob for ob in b.outcomes if ob.key == oa.key), None)
        if target is None:
            _reject("outcomes_differ")
            return None
        omap[oa.key] = target.key
    if len(set(omap.values())) != len(omap) or len(omap) != len(b.outcomes):
        _reject("outcomes_differ")  # e.g. 1X2 vs two-way winner: different settlement
        return None
    conf = round(pconf * tconf, 4)
    return MatchResult(a, b, conf, omap, reasons)


def _bucket(m: Market) -> tuple:
    return (sport_family(m.category), m.market_type, m.period)


def group_markets(markets: list[Market], *, min_conf: float = 0.9, tol_hours: float = 30.0) -> list[MarketGroup]:
    """Greedy grouping: every platform contributes at most one market per group.

    Markets are compared only inside the same (sport, market type, period) bucket and
    only with markets starting within ``tol_hours`` (binary search on start time), so the
    cost is close to linear instead of every-market-against-every-market.
    """
    groups: list[MarketGroup] = []
    used: set[int] = set()
    # bucket -> platform -> (sorted start timestamps, indices) ; markets without time kept apart
    index: dict[tuple, dict[str, tuple[list[float], list[int], list[int]]]] = {}
    for i, m in enumerate(markets):
        plats = index.setdefault(_bucket(m), {})
        times, idx, untimed = plats.setdefault(m.platform, ([], [], []))
        if m.start_time is None:
            untimed.append(i)
        else:
            times.append(m.start_time.timestamp())
            idx.append(i)
    for plats in index.values():
        for name, (times, idx, untimed) in list(plats.items()):
            order = sorted(range(len(times)), key=times.__getitem__)
            plats[name] = ([times[k] for k in order], [idx[k] for k in order], untimed)
    tol = tol_hours * 3600

    def candidates(base: Market, other: str) -> list[int]:
        times, idx, untimed = index[_bucket(base)].get(other, ([], [], []))
        if base.start_time is None:
            return idx + untimed
        t = base.start_time.timestamp()
        lo, hi = bisect_left(times, t - tol), bisect_right(times, t + tol)
        return idx[lo:hi] + untimed

    by_platform: dict[str, list[int]] = {}
    for i, m in enumerate(markets):
        by_platform.setdefault(m.platform, []).append(i)
    platforms = sorted(by_platform)
    for pi, plat in enumerate(platforms):
        for i in by_platform[plat]:
            if i in used:
                continue
            base = markets[i]
            members = [base]
            maps = {base.market_id: {o.key: o.key for o in base.outcomes}}
            conf = 1.0
            reasons: list[str] = []
            for other in platforms[pi + 1:]:
                best: tuple[int, MatchResult] | None = None
                for j in candidates(base, other):
                    if j in used:
                        continue
                    r = match_markets(base, markets[j], tol_hours=tol_hours)
                    if r and r.confidence < min_conf:
                        _reject("low_confidence")
                    if r and r.confidence >= min_conf and (best is None or r.confidence > best[1].confidence):
                        best = (j, r)
                if best:
                    j, r = best
                    used.add(j)
                    members.append(markets[j])
                    inv = {v: k for k, v in r.outcome_map.items()}
                    maps[markets[j].market_id] = {o.key: inv[o.key] for o in markets[j].outcomes if o.key in inv}
                    conf = min(conf, r.confidence)
                    reasons.extend(f"{plat}↔{other}: {x}" for x in r.reasons)
            used.add(i)
            groups.append(
                MarketGroup(members, conf, [o.key for o in base.outcomes], maps, reasons)
            )
    return groups


def settlement_conflicts(profiles: list[tuple[str, SettlementProfile]], sport: str, *,
                         soft_unknown: bool = False) -> tuple[list[str], list[str]]:
    """Return (blocking conflicts, warnings) across platform settlement rules.

    ``soft_unknown``: an UNKNOWN rule becomes an explicit risk instead of a block
    (used only for odds the user typed in by hand and takes responsibility for).
    """
    blocking: list[str] = []
    warnings: list[str] = []
    if sport_family(sport) == "tennis":
        rets = {p.retirement for _, p in profiles}
        if RetirementRule.UNKNOWN in rets and soft_unknown:
            warnings.append("РИСК: правила расчёта при отказе теннисиста у одной из контор неизвестны — "
                            "проверьте их в правилах конторы до ставки")
        elif RetirementRule.UNKNOWN in rets:
            blocking.append("правило retirement неизвестно хотя бы на одной площадке")
        elif rets == {RetirementRule.ADVANCING_PLAYER, RetirementRule.ADVANCING_AFTER_FIRST_SET}:
            warnings.append(
                "РИСК: если игрок снимется в 1-м сете, у букмекера ставка вернётся, а на другой площадке "
                "победа будет засчитана прошедшему дальше — в этом редком случае возможен убыток")
        elif len(rets) > 1:
            blocking.append(
                "разные правила при retirement: "
                + ", ".join(f"{n}={p.retirement.value}" for n, p in profiles)
            )
    voids = {p.walkover for _, p in profiles}
    if len(voids) > 1 or VoidRule.UNKNOWN in voids:
        warnings.append(
            "при отмене/walkover правила расчёта различаются ("
            + ", ".join(f"{n}: {p.walkover.value}" for n, p in profiles)
            + ") — в этом сценарии результат не гарантирован"
        )
    return blocking, warnings
