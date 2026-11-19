"""Stage-2 connectivity check: prints what each real source returns right now.

    python scripts/check_sources.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.registry import build_providers  # noqa: E402


async def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    provs = build_providers()
    for name, p in provs.items():
        st = await p.health_check()
        n = ""
        if st.state.value in ("ok", "degraded"):
            ms = await p.fetch_markets()
            quotes = sum(len(o.quotes) for m in ms for o in m.outcomes)
            cats = sorted({m.category for m in ms})
            n = f"events={p.status.events} markets={len(ms)} quotes={quotes} categories={cats[:12]}"
        print(f"[{st.state.value:11}] {name:13} {n or st.message[:150]}")
        await p.close()


asyncio.run(main())
