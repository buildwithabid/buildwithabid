"""Order gateways (live and paper) and the three-leg cycle executor.

The executor is shared by paper and live trading, so a paper run exercises
exactly the decision and recovery logic that would handle real money.

Execution policy
  leg 1   LIMIT IOC at the price the opportunity was seen at. If nobody is
          there any more the order simply expires: a missed opportunity costs
          nothing.
  leg 2   Before sending, re-price the rest of the cycle on the current book
          and compare with unwinding leg 1. Take whichever returns more.
  leg 2/3 LIMIT IOC priced `leg_slippage_bps` through the touch. Whatever
          does not fill is finished with a MARKET order so the agent never
          sits on an unintended position.
  always  Orders are pre-checked against Binance filters. An order whose
          outcome is unknown (timeout, HTTP 5xx) is looked up by client order
          id; if it still can't be resolved the agent halts.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol

import aiohttp

from .book import BookStore, Valuer
from .brain import BROKEN, COMPLETE, MISS
from .config import PaperConfig
from .exchange import BinanceAPIError, BinanceREST, RateLimited
from .graph import Leg, Triangle
from .market import BUY, SELL, ZERO, D, SymbolRules, fmt
from .pricing import CyclePlan, FeeModel, leg_limit_price, plan_leg

log = logging.getLogger(__name__)

FILLED, EXPIRED, REJECTED = "FILLED", "EXPIRED", "REJECTED"


class OrderStateUnknown(Exception):
    """An order may or may not have executed. Trading must stop."""


@dataclass
class Fill:
    symbol: str
    side: str
    kind: str                      # LIMIT_IOC or MARKET
    status: str
    executed_qty: Decimal = ZERO   # base
    quote_qty: Decimal = ZERO      # quote
    commissions: dict[str, Decimal] = field(default_factory=dict)
    order_id: int | None = None
    client_id: str = ""
    latency_ms: float = 0.0
    error: str | None = None

    def spent(self) -> Decimal:
        return self.quote_qty if self.side == BUY else self.executed_qty

    def received(self, r: SymbolRules) -> Decimal:
        if self.side == BUY:
            return self.executed_qty - self.commissions.get(r.base, ZERO)
        return self.quote_qty - self.commissions.get(r.quote, ZERO)

    def external_fees(self, r: SymbolRules) -> dict[str, Decimal]:
        """Commission not taken from the received asset (e.g. paid in BNB)."""
        got = r.base if self.side == BUY else r.quote
        return {a: v for a, v in self.commissions.items() if a != got and v > 0}

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "side": self.side, "kind": self.kind, "status": self.status,
            "executed_qty": fmt(self.executed_qty), "quote_qty": fmt(self.quote_qty),
            "commissions": {k: fmt(v) for k, v in self.commissions.items()},
            "order_id": self.order_id, "client_id": self.client_id,
            "latency_ms": round(self.latency_ms, 2), "error": self.error,
        }


class Gateway(Protocol):
    def free(self, asset: str) -> Decimal: ...
    async def limit_ioc(self, symbol: str, side: str, qty: Decimal, price: Decimal) -> Fill: ...
    async def market(self, symbol: str, side: str, qty: Decimal | None = None,
                     quote_qty: Decimal | None = None) -> Fill: ...
    async def refresh(self) -> None: ...


# --------------------------------------------------------------------------
# Live gateway
# --------------------------------------------------------------------------

def _commissions(fills: list[dict[str, Any]]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(Decimal)
    for f in fills:
        out[f["commissionAsset"]] += D(f["commission"])
    return dict(out)


class LiveGateway:
    def __init__(self, client: BinanceREST, on_rtt: Callable[[float], None] | None = None):
        self.client = client
        self.on_rtt = on_rtt
        self.balances: dict[str, Decimal] = {}

    def free(self, asset: str) -> Decimal:
        return self.balances.get(asset, ZERO)

    async def refresh(self) -> None:
        acct = await self.client.account()
        self.balances = {b["asset"]: D(b["free"]) for b in acct.get("balances", [])}

    async def limit_ioc(self, symbol: str, side: str, qty: Decimal, price: Decimal) -> Fill:
        params = {"type": "LIMIT", "timeInForce": "IOC", "quantity": fmt(qty), "price": fmt(price)}
        return await self._send(symbol, side, "LIMIT_IOC", params)

    async def market(self, symbol: str, side: str, qty: Decimal | None = None,
                     quote_qty: Decimal | None = None) -> Fill:
        params: dict[str, Any] = {"type": "MARKET"}
        if qty is not None:
            params["quantity"] = fmt(qty)
        else:
            params["quoteOrderQty"] = fmt(quote_qty or ZERO)
        return await self._send(symbol, side, "MARKET", params)

    async def _send(self, symbol: str, side: str, kind: str, params: dict[str, Any]) -> Fill:
        cid = "arb" + uuid.uuid4().hex[:24]
        t0 = time.perf_counter()
        try:
            resp = await self.client.new_order(symbol=symbol, side=side, newClientOrderId=cid, **params)
        except RateLimited:
            raise                            # 429/418: stop trading rather than retry into a ban
        except BinanceAPIError as e:
            if e.status >= 500:
                # Binance: a 5xx means the order status is UNKNOWN, not failed.
                return await self._resolve(symbol, side, kind, cid, t0)
            return Fill(symbol, side, kind, REJECTED, client_id=cid, error=str(e),
                        latency_ms=(time.perf_counter() - t0) * 1000)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            log.warning("order %s %s %s: %r, resolving by client id", symbol, side, cid, e)
            return await self._resolve(symbol, side, kind, cid, t0)
        latency = (time.perf_counter() - t0) * 1000
        if self.on_rtt:
            self.on_rtt(latency)
        return Fill(
            symbol, side, kind, resp["status"],
            executed_qty=D(resp.get("executedQty", "0")),
            quote_qty=D(resp.get("cummulativeQuoteQty", "0")),
            commissions=_commissions(resp.get("fills", [])),
            order_id=resp.get("orderId"), client_id=cid, latency_ms=latency,
        )

    async def _resolve(self, symbol: str, side: str, kind: str, cid: str, t0: float) -> Fill:
        not_found = 0
        for attempt in range(6):
            await asyncio.sleep(0.3 * (attempt + 1))
            try:
                o = await self.client.get_order(symbol, cid)
            except BinanceAPIError as e:
                if e.code == -2013:          # order does not exist
                    not_found += 1
                    if not_found >= 3:
                        return Fill(symbol, side, kind, REJECTED, client_id=cid, error="order was not placed",
                                    latency_ms=(time.perf_counter() - t0) * 1000)
                continue
            except (asyncio.TimeoutError, aiohttp.ClientError):
                continue
            if o["status"] in ("NEW", "PARTIALLY_FILLED", "PENDING_NEW"):
                continue                     # IOC/MARKET orders finish almost at once
            fill = Fill(symbol, side, kind, o["status"], executed_qty=D(o["executedQty"]),
                        quote_qty=D(o["cummulativeQuoteQty"]), order_id=o["orderId"], client_id=cid,
                        latency_ms=(time.perf_counter() - t0) * 1000)
            if fill.executed_qty > 0:
                trades = await self.client.my_trades(symbol, o["orderId"])
                fill.commissions = _commissions(trades)
            return fill
        raise OrderStateUnknown(f"could not resolve order {cid} on {symbol}")


# --------------------------------------------------------------------------
# Paper gateway
# --------------------------------------------------------------------------

class PaperGateway:
    """Fills orders against the live (or replayed) top of book after a
    simulated network delay. Only the visible best level is assumed to be
    there at the touch price; market orders that need more pay a penalty.
    Queue competition is not modelled, so results are an upper bound."""

    def __init__(self, books: BookStore, rules: dict[str, SymbolRules], fees: FeeModel, clock: Any,
                 cfg: PaperConfig):
        self.books = books
        self.rules = rules
        self.fees = fees
        self.clock = clock
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.balances: dict[str, Decimal] = defaultdict(Decimal, {k: D(v) for k, v in cfg.starting_balances.items()})

    def free(self, asset: str) -> Decimal:
        return self.balances.get(asset, ZERO)

    async def refresh(self) -> None:
        return None

    async def _half_trip(self) -> None:
        rtt = max(1.0, self.cfg.rtt_ms + self.rng.gauss(0, self.cfg.rtt_jitter_ms))
        await self.clock.sleep(rtt / 2000.0)

    @staticmethod
    def _consume(q: Any, side: str, qty: Decimal) -> None:
        """Our fill takes visible liquidity until the exchange sends a new update."""
        if side == BUY:
            q.ask_qty = max(0.0, q.ask_qty - float(qty))
        else:
            q.bid_qty = max(0.0, q.bid_qty - float(qty))

    def _reject(self, symbol: str, side: str, kind: str, why: str) -> Fill:
        return Fill(symbol, side, kind, REJECTED, client_id="paper", error=why)

    def _apply(self, r: SymbolRules, side: str, kind: str, base: Decimal, quote: Decimal) -> Fill:
        fee = D(self.fees.rate(r.symbol))
        if side == BUY:
            commission = {r.base: base * fee} if base > 0 else {}
            self.balances[r.quote] -= quote
            self.balances[r.base] += base - base * fee
        else:
            commission = {r.quote: quote * fee} if base > 0 else {}
            self.balances[r.base] -= base
            self.balances[r.quote] += quote - quote * fee
        return Fill(r.symbol, side, kind, FILLED, executed_qty=base, quote_qty=quote,
                    commissions=commission, client_id="paper")

    async def limit_ioc(self, symbol: str, side: str, qty: Decimal, price: Decimal) -> Fill:
        r = self.rules[symbol]
        t0 = self.clock.now()
        why = r.check(qty, price)
        if why:
            return self._reject(symbol, side, "LIMIT_IOC", why)
        need = qty * price if side == BUY else qty
        if self.free(r.quote if side == BUY else r.base) < need:
            return self._reject(symbol, side, "LIMIT_IOC", "insufficient balance")
        await self._half_trip()
        q = self.books.get(symbol)
        filled, px = ZERO, ZERO
        if q is not None:
            if side == BUY and D(q.ask) <= price:
                px, filled = D(q.ask), min(qty, D(q.ask_qty))
            elif side == SELL and D(q.bid) >= price:
                px, filled = D(q.bid), min(qty, D(q.bid_qty))
            filled = r.floor_qty(filled)
            self._consume(q, side, filled)
        fill = self._apply(r, side, "LIMIT_IOC", filled, filled * px)
        fill.status = FILLED if filled == qty else EXPIRED
        await self._half_trip()
        fill.latency_ms = (self.clock.now() - t0) * 1000
        return fill

    async def market(self, symbol: str, side: str, qty: Decimal | None = None,
                     quote_qty: Decimal | None = None) -> Fill:
        r = self.rules[symbol]
        t0 = self.clock.now()
        await self._half_trip()
        q = self.books.get(symbol)
        if q is None:
            return self._reject(symbol, side, "MARKET", "no market")
        pen = D(self.cfg.depth_penalty_bps) / 10000
        if side == BUY:
            px, top = D(q.ask), D(q.ask_qty)
            worse = px * (1 + pen)
            if qty is None:
                budget = quote_qty or ZERO
                base = budget / px if budget <= px * top else top + (budget - px * top) / worse
                qty = r.floor_qty(base, market=True)
            cost = qty * px if qty <= top else top * px + (qty - top) * worse
            why = r.check(qty, px, market=True)
            if why:
                return self._reject(symbol, side, "MARKET", why)
            if self.free(r.quote) < cost:
                return self._reject(symbol, side, "MARKET", "insufficient balance")
            fill = self._apply(r, side, "MARKET", qty, cost)
            self._consume(q, side, min(qty, top))
        else:
            assert qty is not None, "paper market SELL needs a base quantity"
            px, top = D(q.bid), D(q.bid_qty)
            worse = px * (1 - pen)
            proceeds = qty * px if qty <= top else top * px + (qty - top) * worse
            why = r.check(qty, px, market=True)
            if why:
                return self._reject(symbol, side, "MARKET", why)
            if self.free(r.base) < qty:
                return self._reject(symbol, side, "MARKET", "insufficient balance")
            fill = self._apply(r, side, "MARKET", qty, proceeds)
            self._consume(q, side, min(qty, top))
        await self._half_trip()
        fill.latency_ms = (self.clock.now() - t0) * 1000
        return fill


# --------------------------------------------------------------------------
# Cycle executor
# --------------------------------------------------------------------------

@dataclass
class CycleResult:
    triangle: str
    start_asset: str
    expected_bps: float
    started: float
    outcome: str = MISS
    spent: Decimal = ZERO
    end_amount: Decimal = ZERO
    fills: list[Fill] = field(default_factory=list)
    dust: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    stuck: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    external_fees: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    finished: float = 0.0
    pnl: float = 0.0               # start-asset units; excludes dust and stuck holdings
    dust_value: float = 0.0
    stuck_value: float = 0.0

    @property
    def realized_bps(self) -> float:
        return self.pnl / float(self.spent) * 1e4 if self.spent > 0 else 0.0

    @property
    def mtm_bps(self) -> float:
        """Realised plus the value of anything left over: what the brain learns from."""
        if self.spent <= 0:
            return 0.0
        return (self.pnl + self.dust_value + self.stuck_value) / float(self.spent) * 1e4

    def as_dict(self) -> dict[str, Any]:
        return {
            "triangle": self.triangle, "start_asset": self.start_asset, "outcome": self.outcome,
            "expected_bps": round(self.expected_bps, 3), "realized_bps": round(self.realized_bps, 3),
            "spent": fmt(self.spent), "end_amount": fmt(self.end_amount), "pnl": self.pnl,
            "dust": {k: fmt(v) for k, v in self.dust.items() if v}, "dust_value": self.dust_value,
            "stuck": {k: fmt(v) for k, v in self.stuck.items() if v}, "stuck_value": self.stuck_value,
            "external_fees": {k: fmt(v) for k, v in self.external_fees.items()},
            "latency_ms": round((self.finished - self.started) * 1000, 2),
            "fills": [f.as_dict() for f in self.fills], "notes": self.notes, "error": self.error,
        }


def reverse_leg(leg: Leg, holding: str) -> Leg:
    return Leg(leg.symbol, SELL if leg.side == BUY else BUY, holding, leg.src)


class CycleExecutor:
    def __init__(self, gateway: Gateway, books: BookStore, rules: dict[str, SymbolRules], fees: FeeModel,
                 valuer: Valuer, clock: Any, slippage_bps: float):
        self.gw = gateway
        self.books = books
        self.rules = rules
        self.fees = fees
        self.valuer = valuer
        self.clock = clock
        self.slippage_bps = slippage_bps

    async def execute(self, plan: CyclePlan) -> CycleResult:
        tri = plan.triangle
        res = CycleResult(tri.id, tri.start, plan.value_bps, self.clock.now())
        holding: list[Any] = [tri.start, ZERO]
        try:
            await self._run(plan, res, holding)
        except Exception as e:  # noqa: BLE001 - anything here must stop trading
            log.exception("cycle %s failed", tri.id)
            res.error = repr(e)
            if res.spent > 0:
                res.outcome = BROKEN
                if holding[0] != tri.start and holding[1] > 0:
                    res.stuck[holding[0]] += holding[1]
        res.finished = self.clock.now()
        self._account(res)
        return res

    # -- steps -----------------------------------------------------------------
    async def _run(self, plan: CyclePlan, res: CycleResult, holding: list[Any]) -> None:
        tri = plan.triangle
        lp = plan.legs[0]
        r1 = self.rules[lp.symbol]
        f1 = await self.gw.limit_ioc(lp.symbol, lp.side, lp.qty, lp.price)
        res.fills.append(f1)
        if f1.executed_qty <= 0:
            res.outcome = MISS
            if f1.error:
                res.notes.append(f"leg 1: {f1.error}")
            return
        res.spent = f1.spent()
        self._fees(res, f1, r1)
        asset, amount = tri.legs[0].dst, f1.received(r1)
        holding[:] = [asset, amount]

        forward = self._estimate(amount, tri.legs[1:])
        unwind_leg = reverse_leg(tri.legs[0], asset)
        back = self._estimate(amount, (unwind_leg,))
        if forward is None or (back is not None and back > forward):
            res.notes.append(f"leg 2 re-priced: forward={forward} unwind={back}; unwinding")
            asset, amount, _ = await self._market_leg(res, unwind_leg, amount)
            holding[:] = [asset, amount]
            self._finish(res, tri, asset, amount, as_planned=False)
            return

        as_planned = True
        for leg in tri.legs[1:]:
            asset, amount, ok = await self._ioc_leg(res, leg, amount)
            holding[:] = [asset, amount]
            as_planned &= ok
            if asset != leg.dst:
                break
        self._finish(res, tri, asset, amount, as_planned)

    def _finish(self, res: CycleResult, tri: Triangle, asset: str, amount: Decimal, as_planned: bool) -> None:
        if asset == tri.start:
            res.end_amount = amount
            res.outcome = COMPLETE if as_planned else BROKEN
        else:
            res.stuck[asset] += amount
            res.outcome = BROKEN
            res.notes.append(f"left holding {fmt(amount)} {asset}")

    async def _ioc_leg(self, res: CycleResult, leg: Leg, amount: Decimal) -> tuple[str, Decimal, bool]:
        """Send an IOC for `amount`; finish any unfilled remainder at market."""
        r = self.rules[leg.symbol]
        q = self.books.get(leg.symbol)
        received, remaining, ok = ZERO, amount, True
        lp = None
        if q is not None:
            price = leg_limit_price(q, leg.side, self.slippage_bps, r)
            lp, why = plan_leg(r, leg.side, amount, price, D(self.fees.rate(leg.symbol)))
            if lp is None:
                res.notes.append(f"{leg.symbol}: {why}")
        if lp is not None:
            f = await self.gw.limit_ioc(leg.symbol, leg.side, lp.qty, lp.price)
            res.fills.append(f)
            self._fees(res, f, r)
            received += f.received(r)
            remaining -= f.spent()
            if f.status != FILLED:
                ok = False
        else:
            ok = False
        if not ok and remaining > 0:
            got, left = await self._market(res, leg, remaining)
            received += got
            remaining = left
        if received > 0:
            if remaining > 0:
                res.dust[leg.src] += remaining
            return leg.dst, received, ok
        return leg.src, remaining, False

    async def _market_leg(self, res: CycleResult, leg: Leg, amount: Decimal) -> tuple[str, Decimal, bool]:
        got, left = await self._market(res, leg, amount)
        if got > 0:
            if left > 0:
                res.dust[leg.src] += left
            return leg.dst, got, False
        return leg.src, left, False

    async def _market(self, res: CycleResult, leg: Leg, amount: Decimal) -> tuple[Decimal, Decimal]:
        """Market order spending up to `amount` of leg.src. Returns (received, unspent)."""
        r = self.rules[leg.symbol]
        q = self.books.get(leg.symbol)
        if q is None:
            res.notes.append(f"{leg.symbol}: no quote for market order")
            return ZERO, amount
        if leg.side == SELL:
            qty = r.floor_qty(amount, market=True)
            why = r.check(qty, D(q.bid), market=True)
            if why:
                res.notes.append(f"{leg.symbol} market: {why}")
                return ZERO, amount
            f = await self.gw.market(leg.symbol, SELL, qty=qty)
        else:
            est_qty = r.floor_qty(amount / D(q.ask), market=True)
            why = r.check(est_qty, D(q.ask), market=True)
            if why:
                res.notes.append(f"{leg.symbol} market: {why}")
                return ZERO, amount
            if r.quote_order_qty_market_allowed:
                f = await self.gw.market(leg.symbol, BUY, quote_qty=r.floor_quote(amount))
            else:
                # Leave 0.5% headroom in case the price ticks up before we arrive.
                qty = r.floor_qty(amount / (D(q.ask) * D("1.005")), market=True)
                f = await self.gw.market(leg.symbol, BUY, qty=qty)
        res.fills.append(f)
        if f.status == REJECTED:
            res.notes.append(f"{leg.symbol} market rejected: {f.error}")
            return ZERO, amount
        self._fees(res, f, r)
        return f.received(r), amount - f.spent()

    async def convert(self, asset: str, amount: Decimal, target: str) -> CycleResult:
        """Market-convert leftover `asset` into `target` (inventory sweep)."""
        res = CycleResult(f"sweep {asset}>{target}", target, 0.0, self.clock.now())
        leg = None
        for r in self.rules.values():
            if (r.base, r.quote) == (asset, target):
                leg = Leg(r.symbol, SELL, asset, target)
            elif (r.base, r.quote) == (target, asset):
                leg = Leg(r.symbol, BUY, asset, target)
            if leg:
                break
        if leg is None:
            res.notes.append(f"no market between {asset} and {target}")
            return res
        got, left = await self._market(res, leg, amount)
        res.spent, res.end_amount = amount - left, got
        res.finished = self.clock.now()
        return res

    # -- helpers ---------------------------------------------------------------
    def _estimate(self, amount: Decimal, legs: Any) -> float | None:
        x = float(amount)
        for leg in legs:
            q = self.books.get(leg.symbol)
            if q is None:
                return None
            fm = 1.0 - self.fees.rate(leg.symbol)
            x = x / q.ask * fm if leg.side == BUY else x * q.bid * fm
        return x

    def _fees(self, res: CycleResult, f: Fill, r: SymbolRules) -> None:
        for asset, v in f.external_fees(r).items():
            res.external_fees[asset] += v

    def _value(self, asset: str, amount: Decimal, target: str) -> float:
        if amount == 0:
            return 0.0
        v = self.valuer.value(asset, float(amount), target)
        return 0.0 if v is None else v

    def _account(self, res: CycleResult) -> None:
        start = res.start_asset
        fees = sum(self._value(a, v, start) for a, v in res.external_fees.items())
        res.dust_value = sum(self._value(a, v, start) for a, v in res.dust.items())
        res.stuck_value = sum(self._value(a, v, start) for a, v in res.stuck.items())
        if res.spent > 0:
            res.pnl = float(res.end_amount - res.spent) - fees
