"""Proxy manager data model."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Protocol = Literal["socks5", "https", "http", "socks4"]
SUPPORTED: tuple[str, ...] = ("socks5", "https")  # task: only SOCKS5 / HTTPS CONNECT, nothing unknown


class ProxyCandidate(BaseModel):
    host: str
    port: int = Field(ge=1, le=65535)
    protocol: Protocol
    country: str = "KZ"
    source: str = ""
    source_stability: float | None = None  # % reported by the list, if any
    source_response_s: float | None = None

    @property
    def key(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        scheme = "socks5" if self.protocol == "socks5" else "http"  # HTTPS proxies speak CONNECT over http://
        return f"{scheme}://{self.host}:{self.port}"


class ProxyStats(BaseModel):
    key: str
    ok: int = 0
    fail: int = 0
    last_latency_ms: float | None = None
    best_latency_ms: float | None = None
    last_speed_kbps: float | None = None
    last_error: str = ""
    anonymity: str = "unknown"  # transparent (shows our IP) / anonymous (hides it)
    last_checked: datetime | None = None
    active: bool = False

    @property
    def stability(self) -> float:
        n = self.ok + self.fail
        return 100.0 * self.ok / n if n else 0.0


class ProxyManagerStatus(BaseModel):
    enabled: bool
    source: str
    source_state: str  # "ok" / "needs_key" / "error: ..."
    candidates: int = 0
    active: int = 0
    best: str | None = None
    best_latency_ms: float | None = None
    best_speed_kbps: float | None = None
    allowed_providers: list[str] = Field(default_factory=list)
    refreshed_at: datetime | None = None
