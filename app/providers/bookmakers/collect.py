"""Run the Zendriver/DOM bookmaker adapters once and summarise the real result.

    python -m app.cli bookmakers [--only winline,fonbet] [--persist] [--json out.json]
"""

from __future__ import annotations

import json
from pathlib import Path

from app.providers.bookmakers.common.dom_provider import DomBookmakerProvider
from app.providers.bookmakers.common.models import AdapterReport, ParsedEvent
from app.providers.browser.zendriver_client import ZendriverClient

COLUMNS = ("Букмекер", "Статус", "HTTP", "Уник. события", "Рынки", "Коэффициенты", "Пример",
           "Последнее обновление (Алматы)", "Причина")


LAST_METRICS: dict[str, dict] = {}


async def run_all(only: set[str] | None = None) -> list[tuple[AdapterReport, list[ParsedEvent]]]:
    from app.providers.registry import build_dom_bookmakers

    provs = build_dom_bookmakers()
    out = []
    try:
        for name, p in provs.items():
            if only and name not in only:
                continue
            assert isinstance(p, DomBookmakerProvider)
            events = await p.refresh(force=True)
            out.append((p.report, events))
            LAST_METRICS[name] = p.metrics()
    finally:
        await ZendriverClient.shared().close()
    return out


def table(reports: list[AdapterReport], metrics: dict[str, dict] | None = None) -> str:
    from app.timeutil import fmt_almaty

    metrics = metrics or {}
    rows = []
    for r in reports:
        m = metrics.get(r.bookmaker.lower(), {})
        rows.append([r.bookmaker, r.status, str(r.http_status or "—"), str(m.get("unique_events", r.events)),
                     str(r.markets), str(m.get("odds", "—")), r.sample or "—",
                     fmt_almaty(r.timestamp, "%d.%m %H:%M:%S") if r.status == "OK" else "—", r.reason or "—"])
    widths = [min(max(len(c[i]) for c in [list(COLUMNS)] + rows), 60) for i in range(len(COLUMNS))]

    def fmt(cells):
        return " | ".join((c if len(c) <= w else c[: w - 1] + "…").ljust(w) for c, w in zip(cells, widths))

    lines = [fmt(COLUMNS), "-+-".join("-" * w for w in widths)] + [fmt(r) for r in rows]
    full = [f"\n{r.bookmaker}: {r.reason}" for r in reports if r.reason and len(r.reason) > 60]
    return "\n".join(lines) + ("\n\nПолные причины:" + "".join(full) if full else "")


def dump_json(results: list[tuple[AdapterReport, list[ParsedEvent]]], path: str | Path) -> None:
    data = [{"report": r.model_dump(mode="json"), "events": [e.model_dump(mode="json") for e in evs]}
            for r, evs in results]
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
