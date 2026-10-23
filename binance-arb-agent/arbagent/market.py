"""Exchange trading rules (filters) and exact Decimal quantization.

Every order the agent sends passes through these helpers, so an order that
Binance would reject for tick size, step size or notional is never sent.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

ZERO = Decimal(0)
BUY = "BUY"
SELL = "SELL"


def D(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def fmt(value: Decimal) -> str:
    """Decimal to a plain string Binance accepts (no exponent, no trailing zeros)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base: str
    quote: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal
    apply_min_to_market: bool
    apply_max_to_market: bool
    quote_order_qty_market_allowed: bool
    quote_precision: int = 8

    def floor_quote(self, amount: Decimal) -> Decimal:
        """Round a quote amount (e.g. quoteOrderQty) down to the asset's precision."""
        return floor_to_step(amount, Decimal(1).scaleb(-self.quote_precision))

    # -- price -------------------------------------------------------------
    def round_price(self, price: Decimal, side: str) -> Decimal:
        """Round a limit price onto the tick grid, never in our favour.

        A BUY limit is rounded down and a SELL limit up, so the order is never
        more aggressive than the price the caller asked for.
        """
        if side == BUY:
            return floor_to_step(price, self.tick_size)
        return ceil_to_step(price, self.tick_size)

    # -- quantity ----------------------------------------------------------
    def _qty_limits(self, market: bool) -> tuple[Decimal, Decimal, Decimal]:
        """(step, min, max) for an order. MARKET orders must satisfy both
        LOT_SIZE and MARKET_LOT_SIZE; a zero field in either means disabled."""
        step, lo, hi = self.step_size, self.min_qty, self.max_qty
        if market:
            if self.market_step_size > step:
                step = self.market_step_size
            lo = max(lo, self.market_min_qty)
            if self.market_max_qty > 0:
                hi = self.market_max_qty if hi <= 0 else min(hi, self.market_max_qty)
        return step, lo, hi

    def floor_qty(self, qty: Decimal, market: bool = False) -> Decimal:
        step, _, hi = self._qty_limits(market)
        q = floor_to_step(qty, step)
        if hi > 0 and q > hi:
            q = floor_to_step(hi, step)
        return q

    def check(self, qty: Decimal, price: Decimal, market: bool = False) -> str | None:
        """Return why an order would be rejected, or None if it passes."""
        if qty <= 0:
            return "quantity is zero after rounding"
        _, min_qty, _ = self._qty_limits(market)
        if qty < min_qty:
            return f"quantity {fmt(qty)} below minQty {fmt(min_qty)}"
        notional = qty * price
        if (not market or self.apply_min_to_market) and notional < self.min_notional:
            return f"notional {fmt(notional)} below minNotional {fmt(self.min_notional)}"
        if self.max_notional > 0 and (not market or self.apply_max_to_market) and notional > self.max_notional:
            return f"notional {fmt(notional)} above maxNotional {fmt(self.max_notional)}"
        if not market:
            if self.min_price > 0 and price < self.min_price:
                return "price below minPrice"
            if self.max_price > 0 and price > self.max_price:
                return "price above maxPrice"
        return None


def _filters(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["filterType"]: f for f in raw.get("filters", [])}


def parse_symbol(raw: dict[str, Any]) -> SymbolRules | None:
    if raw.get("status") != "TRADING" or not raw.get("isSpotTradingAllowed", True):
        return None
    order_types = set(raw.get("orderTypes", []))
    if not {"LIMIT", "MARKET"} <= order_types:
        return None
    f = _filters(raw)
    price = f.get("PRICE_FILTER", {})
    lot = f.get("LOT_SIZE", {})
    mlot = f.get("MARKET_LOT_SIZE", {})
    # Older symbols may still carry MIN_NOTIONAL instead of NOTIONAL.
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    if not lot:
        return None
    return SymbolRules(
        symbol=raw["symbol"],
        base=raw["baseAsset"],
        quote=raw["quoteAsset"],
        tick_size=D(price.get("tickSize", "0")),
        min_price=D(price.get("minPrice", "0")),
        max_price=D(price.get("maxPrice", "0")),
        step_size=D(lot.get("stepSize", "0")),
        min_qty=D(lot.get("minQty", "0")),
        max_qty=D(lot.get("maxQty", "0")),
        market_step_size=D(mlot.get("stepSize", "0")),
        market_min_qty=D(mlot.get("minQty", "0")),
        market_max_qty=D(mlot.get("maxQty", "0")),
        min_notional=D(notional.get("minNotional", "0")),
        max_notional=D(notional.get("maxNotional", "0")),
        apply_min_to_market=bool(notional.get("applyMinToMarket", notional.get("applyToMarket", True))),
        apply_max_to_market=bool(notional.get("applyMaxToMarket", False)),
        quote_order_qty_market_allowed=bool(raw.get("quoteOrderQtyMarketAllowed", False)),
        quote_precision=int(raw.get("quoteAssetPrecision", raw.get("quotePrecision", 8))),
    )


def parse_exchange_info(data: dict[str, Any]) -> dict[str, SymbolRules]:
    rules: dict[str, SymbolRules] = {}
    for raw in data.get("symbols", []):
        parsed = parse_symbol(raw)
        if parsed is not None:
            rules[parsed.symbol] = parsed
    return rules
