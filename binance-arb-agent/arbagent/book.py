"""Top-of-book state per symbol, plus asset valuation across markets."""
from __future__ import annotations

from typing import Iterable

from .market import SymbolRules


class Quote:
    __slots__ = ("symbol", "bid", "bid_qty", "ask", "ask_qty", "update_id", "ts", "valid")

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bid = 0.0
        self.bid_qty = 0.0
        self.ask = 0.0
        self.ask_qty = 0.0
        self.update_id = -1
        self.ts = 0.0
        self.valid = False

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return float("inf")
        return (self.ask - self.bid) / self.ask * 1e4

    def __repr__(self) -> str:
        return f"Quote({self.symbol} {self.bid}x{self.bid_qty} / {self.ask}x{self.ask_qty})"


class BookStore:
    def __init__(self, symbols: Iterable[str] = ()) -> None:
        self.quotes: dict[str, Quote] = {s: Quote(s) for s in symbols}
        self.updates = 0

    def get(self, symbol: str) -> Quote | None:
        q = self.quotes.get(symbol)
        return q if q is not None and q.valid else None

    def update(
        self, symbol: str, update_id: int, bid: float, bid_qty: float, ask: float, ask_qty: float, ts: float
    ) -> bool:
        q = self.quotes.get(symbol)
        if q is None:
            q = self.quotes[symbol] = Quote(symbol)
        if q.valid and update_id <= q.update_id:
            return False  # out-of-order or duplicate (e.g. overlap after a reconnect)
        q.update_id = update_id
        q.ts = ts
        # A one-sided or empty book can't be traded through.
        q.valid = bid > 0 and ask > 0 and ask >= bid
        changed = (bid, bid_qty, ask, ask_qty) != (q.bid, q.bid_qty, q.ask, q.ask_qty)
        q.bid, q.bid_qty, q.ask, q.ask_qty = bid, bid_qty, ask, ask_qty
        self.updates += 1
        return changed and q.valid

    def invalidate(self, symbols: Iterable[str]) -> None:
        for s in symbols:
            q = self.quotes.get(s)
            if q is not None:
                q.valid = False


BRIDGES = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB")


class Valuer:
    """Converts an amount of one asset into another using live mid prices."""

    def __init__(self, rules: dict[str, SymbolRules], books: BookStore) -> None:
        self.books = books
        self.pairs: dict[tuple[str, str], str] = {(r.base, r.quote): r.symbol for r in rules.values()}

    def _direct(self, asset: str, target: str) -> float | None:
        sym = self.pairs.get((asset, target))
        if sym:
            q = self.books.get(sym)
            return q.mid if q else None
        sym = self.pairs.get((target, asset))
        if sym:
            q = self.books.get(sym)
            return 1.0 / q.mid if q and q.mid > 0 else None
        return None

    def rate(self, asset: str, target: str) -> float | None:
        if asset == target:
            return 1.0
        r = self._direct(asset, target)
        if r is not None:
            return r
        for bridge in BRIDGES:
            if bridge in (asset, target):
                continue
            a = self._direct(asset, bridge)
            if a is None:
                continue
            b = self._direct(bridge, target)
            if b is not None:
                return a * b
        return None

    def value(self, asset: str, amount: float, target: str) -> float | None:
        r = self.rate(asset, target)
        return None if r is None else amount * r
