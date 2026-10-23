"""Real-time best bid/ask for many symbols over Binance combined streams."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import aiohttp

from .book import BookStore

log = logging.getLogger(__name__)

try:  # optional speed-up
    import orjson

    _loads: Callable[[str | bytes], Any] = orjson.loads
except ImportError:  # pragma: no cover
    _loads = json.loads


class BookStream:
    def __init__(self, base_url: str, symbols: list[str], books: BookStore, clock: Any,
                 on_update: Callable[[str], None], per_connection: int = 200,
                 on_raw: Callable[[float, dict[str, Any]], None] | None = None):
        self.base_url = base_url.rstrip("/")
        self.books = books
        self.clock = clock
        self.on_update = on_update
        self.on_raw = on_raw
        self.chunks = [symbols[i:i + per_connection] for i in range(0, len(symbols), per_connection)]
        self.connected = [False] * len(self.chunks)
        self.last_msg = [0.0] * len(self.chunks)
        self.messages = 0
        self.reconnects = 0
        self._stop = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None

    def stop(self) -> None:
        self._stop.set()

    def healthy(self, stale_s: float) -> bool:
        now = self.clock.now()
        return all(self.connected) and all(now - t < stale_s for t in self.last_msg)

    def ready_fraction(self) -> float:
        valid = sum(1 for q in self.books.quotes.values() if q.valid)
        return valid / max(1, len(self.books.quotes))

    async def run(self) -> None:
        self._session = aiohttp.ClientSession(trust_env=True)
        try:
            tasks = []
            for i, chunk in enumerate(self.chunks):
                tasks.append(asyncio.create_task(self._connection(i, chunk)))
                await asyncio.sleep(0.25)   # stay well inside the connection-rate limit
            await self._stop.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._session.close()

    async def _connection(self, idx: int, chunk: list[str]) -> None:
        url = f"{self.base_url}/stream?streams=" + "/".join(f"{s.lower()}@bookTicker" for s in chunk)
        backoff = 1.0
        assert self._session is not None
        while not self._stop.is_set():
            try:
                async with self._session.ws_connect(url, autoping=True, heartbeat=None,
                                                    max_msg_size=1 << 22) as ws:
                    self.connected[idx] = True
                    self.last_msg[idx] = self.clock.now()
                    backoff = 1.0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            if not self._handle(idx, msg.data):
                                break       # server is shutting down: reconnect now
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("stream %d error: %r", idx, e)
            finally:
                self.connected[idx] = False
                # Quotes we can no longer see must not be traded on.
                self.books.invalidate(chunk)
            if self._stop.is_set():
                break
            self.reconnects += 1
            log.info("stream %d reconnecting in %.0fs", idx, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _handle(self, idx: int, raw: str) -> bool:
        msg = _loads(raw)
        data = msg.get("data", msg)
        now = self.clock.now()
        self.last_msg[idx] = now
        if data.get("e") == "serverShutdown":
            return False
        sym = data.get("s")
        if sym is None:
            return True
        self.messages += 1
        if self.on_raw is not None:
            self.on_raw(now, data)
        if self.books.update(sym, data["u"], float(data["b"]), float(data["B"]),
                             float(data["a"]), float(data["A"]), now):
            self.on_update(sym)
        return True
