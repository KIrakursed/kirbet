"""Public source status taxonomy (what users and the admin see).

WORKING     real data is being read (events with odds and timestamps)
PARTIAL     real data, but part of the source's pages failed
DEGRADED    unstable: recent errors, data may be incomplete
BLOCKED     the source refuses us (403, Cloudflare, geo-block page, robots.txt) — not bypassed
UNAVAILABLE technically unreachable (timeout, connection refused) or a required key is missing
DISABLED    switched off by the admin
"""

from __future__ import annotations

import enum

from app.providers.base import BaseProvider, ProviderState


class SourceStatus(str, enum.Enum):
    WORKING = "WORKING"
    PARTIAL = "PARTIAL"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"

    @property
    def icon(self) -> str:
        return {"WORKING": "🟢", "PARTIAL": "🟡", "DEGRADED": "🟠", "BLOCKED": "⛔", "UNAVAILABLE": "⚫",
                "DISABLED": "⏸"}[self.value]


BLOCK_WORDS = ("403", "антибот", "cloudflare", "блокировк", "robots.txt", "captcha", "проверки")


def public_status(p: BaseProvider) -> tuple[SourceStatus, str]:
    st = p.status
    msg = st.message or st.last_error or ""
    if getattr(p, "admin_disabled", False):
        return SourceStatus.DISABLED, "отключён администратором"
    if not p.enabled:
        return SourceStatus.DISABLED, msg
    rep = getattr(p, "report", None)
    if st.state == ProviderState.OK:
        if rep is not None and rep.status == "OK" and rep.reason:
            return SourceStatus.PARTIAL, rep.reason
        return SourceStatus.WORKING, ""
    if st.state == ProviderState.DEGRADED:
        return SourceStatus.DEGRADED, msg
    if st.state == ProviderState.NEEDS_KEY:
        return SourceStatus.UNAVAILABLE, msg
    low = msg.lower()
    if st.state == ProviderState.UNAVAILABLE or (rep is not None and rep.status == "BLOCKED") \
            or any(w in low for w in BLOCK_WORDS):
        return SourceStatus.BLOCKED, msg
    return SourceStatus.UNAVAILABLE, msg
