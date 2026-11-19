"""Minimal SignalR client for Oinabet's public line feed (MessagePack + brotli).

Wire format observed on oinabet.kz (normal page view, 18.11.2026):
  negotiate: POST https://ws.oinabet.kz/signalr/negotiate?platform=pwa&X-Tenant-Id=oinabet.kz&negotiateVersion=1
  socket:    wss://ws.oinabet.kz/signalr?platform=pwa&X-Tenant-Id=oinabet.kz&id=<connectionToken>
  handshake: {"protocol":"messagepack+brotli","version":1}\\x1e
  frames:    varint length prefix + MessagePack array
  invoke:    [1, {}, None, target, 0, args]
  server:    [1, {}, None, "updateState", mode, payload, []]; mode 1 = brotli(MessagePack(payload))
  ping:      [6]
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import brotli
import httpx
import msgpack
import websockets

from app.providers.base import USER_AGENT

log = logging.getLogger("provider.oinabet.signalr")
HOST = "ws.oinabet.kz"
QUERY = "platform=pwa&X-Tenant-Id=oinabet.kz"


def encode_frame(obj) -> bytes:
    body = msgpack.packb(obj, use_bin_type=True)
    n, pre = len(body), bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n:
            pre.append(x | 0x80)
        else:
            pre.append(x)
            break
    return bytes(pre) + body


def decode_frames(raw: bytes) -> list:
    out, i = [], 0
    while i < len(raw):
        n = sh = 0
        while True:
            x = raw[i]
            i += 1
            n |= (x & 0x7F) << sh
            sh += 7
            if x < 0x80:
                break
        out.append(msgpack.unpackb(raw[i:i + n], raw=False, strict_map_key=False))
        i += n
    return out


def decode_update(msg) -> list[dict] | None:
    if not (isinstance(msg, list) and len(msg) >= 6 and msg[0] == 1 and msg[3] == "updateState"):
        return None
    payload = msg[5]
    if msg[4] == 1:
        payload = msgpack.unpackb(brotli.decompress(payload), raw=False, strict_map_key=False)
    return payload if isinstance(payload, list) else [payload]


class SignalRClient:
    def __init__(self, timeout: float = 20):
        self.timeout = timeout
        self.ws = None
        self.updates: dict[str, list[dict]] = {}
        self.other: list = []
        self._reader: asyncio.Task | None = None
        self.last_message_at = 0.0

    @property
    def connected(self) -> bool:
        return self.ws is not None and self._reader is not None and not self._reader.done()

    async def connect(self) -> None:
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.post(f"https://{HOST}/signalr/negotiate?{QUERY}&negotiateVersion=1")
            r.raise_for_status()
            token = r.json()["connectionToken"]
        self.ws = await websockets.connect(
            f"wss://{HOST}/signalr?{QUERY}&id={token}", additional_headers={"User-Agent": USER_AGENT},
            max_size=64 * 1024 * 1024, open_timeout=self.timeout)
        await self.ws.send(json.dumps({"protocol": "messagepack+brotli", "version": 1}) + "\x1e")
        hs = await asyncio.wait_for(self.ws.recv(), self.timeout)
        if (hs if isinstance(hs, str) else hs.decode()).strip("\x1e") not in ("{}", ""):
            raise ConnectionError(f"handshake rejected: {hs!r}")
        await self.invoke("setLanguage", ["ru"])
        self._reader = asyncio.create_task(self._read())

    async def invoke(self, target: str, args: list) -> None:
        await self.ws.send(encode_frame([1, {}, None, target, 0, args]))

    async def subscribe(self, key: str, flt: dict, mode: int = 1) -> None:
        self.updates.setdefault(key, [])
        await self.invoke("subscribe-ext", [key, [mode, flt]])

    async def unsubscribe(self, key: str) -> None:
        try:
            await self.invoke("unsubscribe", [key])
        except Exception:  # noqa: BLE001
            pass

    async def _read(self) -> None:
        try:
            async for m in self.ws:
                self.last_message_at = time.monotonic()
                if isinstance(m, str):
                    continue
                for msg in decode_frames(m):
                    if msg and msg[0] == 6:  # ping -> pong
                        await self.ws.send(encode_frame([6]))
                        continue
                    parts = decode_update(msg)
                    if parts is None:
                        self.other.append(msg)
                        continue
                    for p in parts:
                        self.updates.setdefault(p.get("t", "?"), []).append(p)
        except websockets.ConnectionClosed as e:
            log.info("signalr closed: %s", e)

    async def collect(self, key: str, seconds: float, *, until_full: bool = True) -> list[dict]:
        """Wait for updates of ``key``; stops early after a full snapshot (r=True) and a quiet second."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            got = self.updates.get(key) or []
            if until_full and any(p.get("d", {}).get("r") for p in got) and time.monotonic() - self.last_message_at > 1:
                break
            await asyncio.sleep(0.25)
        return self.updates.get(key) or []

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws is not None:
            await self.ws.close()
        self.ws = None
