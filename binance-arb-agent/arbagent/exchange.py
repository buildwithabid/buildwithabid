"""Minimal async Binance spot REST client (HMAC-SHA256 signed)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger(__name__)


class BinanceAPIError(Exception):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"HTTP {status} code={code}: {msg}")
        self.status = status
        self.code = code
        self.msg = msg


class RateLimited(BinanceAPIError):
    def __init__(self, status: int, retry_after: float, msg: str):
        super().__init__(status, None, msg)
        self.retry_after = retry_after


class IPBanned(RateLimited):
    pass


def sign_query(secret: bytes, params: dict[str, Any]) -> str:
    """URL-encode `params` and append Binance's HMAC-SHA256 signature."""
    query = urlencode(params)
    sig = hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={sig}"


class BinanceREST:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        recv_window_ms: int = 5000,
        timeout_s: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._secret = api_secret.encode() if api_secret else None
        self.recv_window_ms = recv_window_ms
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.time_offset_ms = 0.0
        self.used_weight_1m = 0
        self.order_count_10s = 0
        self._session: aiohttp.ClientSession | None = None
        self._banned_until = 0.0

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self._secret)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, keepalive_timeout=60)
            # trust_env lets HTTPS_PROXY work for people behind a corporate proxy.
            self._session = aiohttp.ClientSession(connector=connector, trust_env=True)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "BinanceREST":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _sign(self, params: dict[str, Any]) -> str:
        assert self._secret is not None
        params["timestamp"] = int(time.time() * 1000 + self.time_offset_ms)
        params["recvWindow"] = self.recv_window_ms
        return sign_query(self._secret, params)

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                      signed: bool = False) -> Any:
        if time.time() < self._banned_until:
            raise RateLimited(429, self._banned_until - time.time(), "backing off after rate limit")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            if not self.has_keys:
                raise RuntimeError("this endpoint needs BINANCE_API_KEY and BINANCE_API_SECRET")
            query = self._sign(params)
        else:
            query = urlencode(params)
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
        session = await self._get_session()
        async with session.request(method, url, headers=headers, timeout=self.timeout) as resp:
            self._track(resp.headers)
            text = await resp.text()
            if resp.status in (418, 429):
                retry = float(resp.headers.get("Retry-After", "60"))
                self._banned_until = time.time() + retry
                cls = IPBanned if resp.status == 418 else RateLimited
                raise cls(resp.status, retry, text[:200])
            try:
                data = json.loads(text) if text else None
            except json.JSONDecodeError:
                data = None
            if resp.status >= 400:
                code = data.get("code") if isinstance(data, dict) else None
                msg = data.get("msg", text[:200]) if isinstance(data, dict) else text[:200]
                raise BinanceAPIError(resp.status, code, msg)
            return data

    def _track(self, headers: Any) -> None:
        w = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        if w:
            self.used_weight_1m = int(w)
        o = headers.get("X-MBX-ORDER-COUNT-10S") or headers.get("x-mbx-order-count-10s")
        if o:
            self.order_count_10s = int(o)

    # -- public ----------------------------------------------------------------
    async def server_time(self) -> int:
        return int((await self.request("GET", "/api/v3/time"))["serverTime"])

    async def sync_time(self) -> float:
        """Align request timestamps with Binance. Returns the round trip in ms."""
        t0 = time.time()
        server = await self.server_time()
        t1 = time.time()
        self.time_offset_ms = server - (t0 + t1) / 2 * 1000
        return (t1 - t0) * 1000

    async def exchange_info(self) -> dict[str, Any]:
        return await self.request("GET", "/api/v3/exchangeInfo", {"permissions": "SPOT"})

    async def ticker_24hr(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/v3/ticker/24hr")

    async def book_tickers(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/v3/ticker/bookTicker")

    # -- account ---------------------------------------------------------------
    async def account(self) -> dict[str, Any]:
        return await self.request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)

    async def api_restrictions(self) -> dict[str, Any]:
        return await self.request("GET", "/sapi/v1/account/apiRestrictions", signed=True)

    async def new_order(self, **params: Any) -> dict[str, Any]:
        params.setdefault("newOrderRespType", "FULL")
        return await self.request("POST", "/api/v3/order", params, signed=True)

    async def get_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return await self.request(
            "GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True
        )

    async def my_trades(self, symbol: str, order_id: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id}, signed=True)


async def measure_rtt(client: BinanceREST, samples: int = 10) -> list[float]:
    out = []
    for _ in range(samples):
        t0 = time.perf_counter()
        await client.request("GET", "/api/v3/ping")
        out.append((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(0.2)
    return out
