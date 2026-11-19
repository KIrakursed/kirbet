"""Shared helpers for adapter tests (fixtures are real DOM fragments saved on 2026-11-19)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

FETCHED = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)  # 17:00 in Almaty


def fixture(adapter: str) -> str:
    return (Path(__file__).resolve().parents[1] / adapter / "tests" / "fixtures" / f"{adapter}_page.html").read_text(
        encoding="utf-8")
