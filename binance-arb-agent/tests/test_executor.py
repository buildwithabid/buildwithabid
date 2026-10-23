"""Executor scenarios on a virtual clock.

Paper RTT is 100 ms with no jitter, so each order matches 50 ms after it is
sent and returns 50 ms later: leg 1 matches at t+0.05, leg 2 is decided at
t+0.10 and matches at t+0.15, leg 3 matches at t+0.25.
"""
import asyncio

import aiohttp
import pytest

from arbagent.brain import BROKEN, COMPLETE, MISS
from arbagent.clock import VirtualClock
from arbagent.exchange import BinanceAPIError, RateLimited, sign_query
from arbagent.executor import (FILLED, REJECTED, CycleExecutor, LiveGateway, OrderStateUnknown,
                               PaperGateway)
from arbagent.market import D
from arbagent.pricing import simulate_cycle

from conftest import set_books

T0 = 1_000.0


def make(rules, books, fees, valuer, paper_cfg, slippage_bps=0.0):
    clock = VirtualClock(T0)
    gw = PaperGateway(books, rules, fees, clock, paper_cfg)
    ex = CycleExecutor(gw, books, rules, fees, valuer, clock, slippage_bps)
    return clock, gw, ex


async def run_with(clock, ex, plan, events=()):
    """Start the cycle, apply (time, fn) book changes at virtual times, finish."""
    task = asyncio.create_task(ex.execute(plan))
    await clock.settle(task)
    for t, fn in events:
        await clock.advance_to(T0 + t, task)
        fn()
    while not task.done():
        await clock.advance_to((clock.next_wake() or clock.now()) + 1e-9, task)
    return task.result()


def plan_for(tri, books, rules, fees, amount="1000"):
    plan = simulate_cycle(tri, D(amount), books, rules, fees)
    assert plan.reject is None
    return plan


async def test_complete_cycle_matches_plan(tri_profitable, rules, books, fees, valuer, paper_cfg):
    clock, gw, ex = make(rules, books, fees, valuer, paper_cfg)
    plan = plan_for(tri_profitable, books, rules, fees)
    res = await run_with(clock, ex, plan)
    assert res.outcome == COMPLETE
    assert res.end_amount == plan.end_amount
    assert res.spent == plan.spent
    assert [f.status for f in res.fills] == [FILLED] * 3
    assert res.realized_bps == pytest.approx(plan.profit_bps)
    assert res.mtm_bps == pytest.approx(plan.value_bps, abs=0.05)
    assert gw.free("USDT") == D("1000") - plan.spent + plan.end_amount
    assert gw.free("ETH") == plan.legs[2].dust                  # only rounding dust left
    assert res.finished - res.started == pytest.approx(0.3)     # three round trips


async def test_leg1_miss_costs_nothing(tri_profitable, rules, books, fees, valuer, paper_cfg):
    clock, gw, ex = make(rules, books, fees, valuer, paper_cfg)
    plan = plan_for(tri_profitable, books, rules, fees)
    res = await run_with(clock, ex, plan, [(0.02, lambda: set_books(books, BTCUSDT=(50010, 2, 50020, 2)))])
    assert res.outcome == MISS
    assert res.spent == 0 and res.pnl == 0
    assert gw.free("USDT") == D("1000")
    assert len(res.fills) == 1


async def test_leg2_ioc_fails_then_finishes_at_market(tri_profitable, rules, books, fees, valuer, paper_cfg):
    clock, gw, ex = make(rules, books, fees, valuer, paper_cfg)
    plan = plan_for(tri_profitable, books, rules, fees)
    # While leg 2 is in flight, ETHBTC ask rises 2 ticks: the IOC at the old price expires.
    res = await run_with(clock, ex, plan, [(0.12, lambda: set_books(books, ETHBTC=(0.06, 50, 0.06002, 50)))])
    assert res.outcome == BROKEN
    kinds = [(f.symbol, f.kind, f.status) for f in res.fills]
    assert kinds[1] == ("ETHBTC", "LIMIT_IOC", "EXPIRED")
    assert kinds[2][:2] == ("ETHBTC", "MARKET")
    assert kinds[3][:2] == ("ETHUSDT", "LIMIT_IOC")
    assert res.end_amount > 0 and not res.stuck
    # Still profitable here because the original edge (+50 bps gross) absorbed the move.
    assert res.pnl + res.dust_value > 0


async def test_unwinds_when_rest_of_cycle_collapses(tri_profitable, rules, books, fees, valuer, paper_cfg):
    clock, gw, ex = make(rules, books, fees, valuer, paper_cfg)
    plan = plan_for(tri_profitable, books, rules, fees)
    # After leg 1 fills, ETHUSDT bid crashes 3%: finishing would lose more than unwinding.
    res = await run_with(clock, ex, plan, [(0.07, lambda: set_books(books, ETHUSDT=(2925, 50, 2925.01, 50)))])
    assert res.outcome == BROKEN
    assert any("unwinding" in n for n in res.notes)
    assert [(f.symbol, f.side, f.kind) for f in res.fills[1:]] == [("BTCUSDT", "SELL", "MARKET")]
    # Unwind cost = spread + two fees, about 20 bps, not the 3% the forward path would have cost.
    assert -25 < res.mtm_bps < -15
    assert gw.free("BTC") < D("0.00001")


async def test_stuck_when_last_market_disappears(tri_profitable, rules, books, fees, valuer, paper_cfg):
    clock, gw, ex = make(rules, books, fees, valuer, paper_cfg)
    plan = plan_for(tri_profitable, books, rules, fees)
    res = await run_with(clock, ex, plan, [(0.21, lambda: books.invalidate(["ETHUSDT"]))])
    assert res.outcome == BROKEN
    assert res.stuck["ETH"] > 0
    # ETHUSDT is gone, so the stuck ETH is valued through ETHBTC and BTCUSDT instead.
    assert res.stuck_value == pytest.approx(float(res.stuck["ETH"]) * 0.059995 * 49999.995, rel=1e-6)
    assert any("left holding" in n for n in res.notes)


async def test_slippage_padding_still_fills_at_the_touch(tri_profitable, rules, books, fees, valuer, paper_cfg):
    clock, gw, ex = make(rules, books, fees, valuer, paper_cfg, slippage_bps=5.0)
    plan = plan_for(tri_profitable, books, rules, fees)
    res = await run_with(clock, ex, plan)
    assert res.outcome == COMPLETE
    # Limit was padded, but paper (like the exchange) fills at the best price available.
    assert res.fills[1].quote_qty / res.fills[1].executed_qty == D("0.06")


async def test_convert_sweeps_leftovers(rules, books, fees, valuer, paper_cfg):
    clock, gw, ex = make(rules, books, fees, valuer, paper_cfg)
    gw.balances["ETH"] = D("0.01")
    task = asyncio.create_task(ex.convert("ETH", D("0.01"), "USDT"))
    while not task.done():
        await clock.advance_to((clock.next_wake() or clock.now()) + 1e-9, task)
    res = task.result()
    assert res.spent == D("0.01")
    assert res.end_amount == D("0.01") * D("3015") * D("0.999")


# -- live gateway against a fake exchange ----------------------------------------

class FakeClient:
    def __init__(self, new_order=None, get_order=None, trades=None):
        self._new, self._get, self._trades = new_order, list(get_order or []), trades or []
        self.calls = []

    async def new_order(self, **params):
        self.calls.append(("new", params))
        if isinstance(self._new, Exception):
            raise self._new
        return self._new

    async def get_order(self, symbol, cid):
        self.calls.append(("get", cid))
        item = self._get.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def my_trades(self, symbol, order_id):
        return self._trades


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = asyncio.sleep
    monkeypatch.setattr("arbagent.executor.asyncio.sleep", lambda s: real(0))


async def test_live_full_response_parsing_and_bnb_fees(rules):
    resp = {"status": "FILLED", "executedQty": "0.002", "cummulativeQuoteQty": "100.0", "orderId": 7,
            "fills": [{"price": "50000", "qty": "0.002", "commission": "0.0001", "commissionAsset": "BNB"}]}
    gw = LiveGateway(FakeClient(new_order=resp))
    f = await gw.limit_ioc("BTCUSDT", "BUY", D("0.002"), D("50000"))
    assert f.status == FILLED and f.order_id == 7
    assert f.received(rules["BTCUSDT"]) == D("0.002")          # fee not taken in kind
    assert f.external_fees(rules["BTCUSDT"]) == {"BNB": D("0.0001")}
    params = gw.client.calls[0][1]
    assert params["timeInForce"] == "IOC" and params["quantity"] == "0.002" and params["price"] == "50000"
    assert params["newClientOrderId"].startswith("arb")


async def test_live_rejection_is_not_an_unknown_state():
    gw = LiveGateway(FakeClient(new_order=BinanceAPIError(400, -2010, "insufficient balance")))
    f = await gw.market("BTCUSDT", "SELL", qty=D("1"))
    assert f.status == REJECTED and "insufficient" in f.error


async def test_live_rate_limit_raises_so_the_agent_halts():
    gw = LiveGateway(FakeClient(new_order=RateLimited(429, 30, "too many requests")))
    with pytest.raises(RateLimited):
        await gw.market("BTCUSDT", "SELL", qty=D("1"))


async def test_live_5xx_is_resolved_by_client_order_id():
    filled = {"status": "FILLED", "executedQty": "1", "cummulativeQuoteQty": "3000", "orderId": 9}
    trades = [{"commission": "3", "commissionAsset": "USDT"}]
    gw = LiveGateway(FakeClient(new_order=BinanceAPIError(503, None, "unknown"), get_order=[filled],
                                trades=trades))
    f = await gw.market("ETHUSDT", "SELL", qty=D("1"))
    assert f.status == FILLED and f.commissions == {"USDT": D("3")}


async def test_live_timeout_then_not_found_means_not_placed():
    nf = BinanceAPIError(400, -2013, "Order does not exist.")
    gw = LiveGateway(FakeClient(new_order=asyncio.TimeoutError(), get_order=[nf, nf, nf]))
    f = await gw.limit_ioc("ETHUSDT", "SELL", D("1"), D("3000"))
    assert f.status == REJECTED


async def test_live_unresolvable_order_raises():
    err = aiohttp.ClientConnectionError("down")
    gw = LiveGateway(FakeClient(new_order=err, get_order=[err] * 6))
    with pytest.raises(OrderStateUnknown):
        await gw.limit_ioc("ETHUSDT", "SELL", D("1"), D("3000"))


def test_signature_matches_binance_documentation():
    secret = b"NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    params = {"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "1",
              "price": "0.1", "recvWindow": "5000", "timestamp": "1499827319559"}
    assert sign_query(secret, params).endswith(
        "&signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")
