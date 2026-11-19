"""Probe Oinabet SignalR subscriptions (research helper).

    python scripts/oinabet_probe.py '<json filter>' [seconds]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.bookmakers.oinabet_signalr import SignalRClient  # noqa: E402


async def main(flt: dict, seconds: float) -> None:
    c = SignalRClient()
    await c.connect()
    key = "probe"
    await c.subscribe(key, flt)
    got = await c.collect(key, seconds)
    for part in got:
        d = part.get("d", {})
        ms = d.get("m") or {}
        print("update", part.get("t"), "r=", d.get("r"), "matches", len(ms),
              "sports", {k: v.get("n") for k, v in (d.get("s") or {}).items()})
        for x in [x for x in ms.values() if x.get("b")][:3]:
            print("   ", x.get("n"), x.get("d"), [(b.get("k"), b.get("p1"), b.get("p2")) for b in x["b"].values()][:4])
    print("other:", [str(o)[:200] for o in c.other[:5]])
    await c.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(json.loads(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 10))
