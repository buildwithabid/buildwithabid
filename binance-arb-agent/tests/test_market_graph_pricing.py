from decimal import Decimal

import pytest

from arbagent.graph import find_triangles
from arbagent.market import BUY, SELL, D, fmt, parse_exchange_info, parse_symbol
from arbagent.pricing import CompiledTriangle, FeeModel, optimize_size, simulate_cycle

from conftest import EXCHANGE_INFO, raw_symbol, set_books


# -- market rules --------------------------------------------------------------

def test_parse_skips_non_trading_and_missing_order_types():
    halted = raw_symbol("XUSDT", "X", "USDT", "0.01", "1", "5") | {"status": "BREAK"}
    no_market = raw_symbol("YUSDT", "Y", "USDT", "0.01", "1", "5") | {"orderTypes": ["LIMIT"]}
    rules = parse_exchange_info({"symbols": [halted, no_market, *EXCHANGE_INFO["symbols"]]})
    assert set(rules) == {"BTCUSDT", "ETHUSDT", "ETHBTC"}


def test_legacy_min_notional_filter_is_read():
    r = parse_symbol(raw_symbol("AB", "A", "B", "0.1", "1", "7", legacy_min_notional=True))
    assert r is not None and r.min_notional == Decimal("7")


def test_quantization_never_rounds_in_our_favour(rules):
    r = rules["BTCUSDT"]
    assert r.floor_qty(D("0.0123456")) == D("0.01234")
    assert r.round_price(D("50000.019"), BUY) == D("50000.01")    # buy limit rounded down
    assert r.round_price(D("50000.011"), SELL) == D("50000.02")   # sell limit rounded up
    assert r.floor_quote(D("1.123456789")) == D("1.12345678")


def test_filter_checks(rules):
    r = rules["BTCUSDT"]
    assert r.check(D("0.00009"), D("50000")) is not None        # notional 4.5 < 5
    assert r.check(D("0.0001"), D("50000")) is None
    assert "minQty" in r.check(D("0.000001"), D("50000000"))
    assert r.check(D("0"), D("50000")) == "quantity is zero after rounding"


def test_market_orders_respect_both_lot_size_filters():
    r = parse_symbol(raw_symbol("AB", "A", "B", "0.01", "0.001", "1", market_step="0.1"))
    assert r.floor_qty(D("1.2345")) == D("1.234")
    assert r.floor_qty(D("1.2345"), market=True) == D("1.2")


def test_fmt_has_no_exponent():
    assert fmt(D("1E-8")) == "0.00000001"
    assert fmt(D("10.500")) == "10.5"
    assert fmt(D("1E+2")) == "100"


# -- triangle discovery -----------------------------------------------------

def test_finds_both_directions(rules):
    tris = find_triangles(rules, ["USDT"])
    assert sorted(t.id for t in tris) == ["USDT>BTC>ETH>USDT", "USDT>ETH>BTC>USDT"]
    t = next(t for t in tris if t.id == "USDT>BTC>ETH>USDT")
    assert [(l.symbol, l.side) for l in t.legs] == [("BTCUSDT", BUY), ("ETHBTC", BUY), ("ETHUSDT", SELL)]
    for a, b in zip(t.legs, t.legs[1:]):
        assert a.dst == b.src
    assert t.legs[-1].dst == "USDT"


def test_exclusions(rules):
    assert find_triangles(rules, ["USDT"], exclude_assets=["ETH"]) == []
    assert len(find_triangles(rules, ["USDT", "BTC"])) == 4


# -- pricing ------------------------------------------------------------------

def test_fast_evaluation_matches_hand_maths(tri_profitable, books, fees):
    ev = CompiledTriangle(tri_profitable, fees).evaluate(books)
    expected = (1 / 50000.0) * (1 / 0.06) * 3015.0 * 0.999 ** 3
    assert ev.rate == pytest.approx(expected)
    assert ev.edge_bps == pytest.approx((expected - 1) * 1e4)
    # Capacity: leg1 2 BTC * 50000 = 100k USDT; leg2 50 ETH * 0.06 BTC = 3 BTC -> /rate;
    # leg3 50 ETH. The tightest is leg 3: 50 ETH in USDT terms.
    leg3_cap = 50.0 / ((1 / 50000.0) * 0.999 * (1 / 0.06) * 0.999)
    assert ev.capacity == pytest.approx(min(100000.0, 3.0 / (1 / 50000 * 0.999), leg3_cap))


def test_evaluate_needs_all_quotes(tri_profitable, books, fees):
    books.invalidate(["ETHBTC"])
    assert CompiledTriangle(tri_profitable, fees).evaluate(books) is None


def test_simulation_applies_rounding_and_fees(tri_profitable, books, rules, fees):
    plan = simulate_cycle(tri_profitable, D("100"), books, rules, fees)
    assert plan.reject is None
    l1, l2, l3 = plan.legs
    assert (l1.qty, l1.amount_in) == (D("0.002"), D("100.000"))
    assert l1.amount_out == D("0.002") * D("0.999")
    assert l2.qty == D("0.0333")
    assert l3.qty == D("0.0332")           # 0.0332667 ETH floored to step
    assert plan.end_amount == D("0.0332") * D("3015") * D("0.999")
    assert plan.profit == plan.end_amount - D("100")
    # The float maths says +19.8 bps, but 0.0000667 ETH of rounding dust is left
    # behind on leg 3 (~20 bps of 100 USDT), so the executable cycle loses money.
    assert plan.legs[2].dust == D("0.0000667")
    assert plan.profit_bps < 0
    big = simulate_cycle(tri_profitable, D("1000"), books, rules, fees)
    assert 15 < big.profit_bps < 19.8


def test_dust_is_marked_through_the_rest_of_the_cycle(tri_profitable, books, rules, fees):
    plan = simulate_cycle(tri_profitable, D("100"), books, rules, fees)
    assert plan.dust_value == pytest.approx(0.0000667 * 3015 * 0.999)
    assert plan.value_bps == pytest.approx(plan.profit_bps + plan.dust_value / 100 * 1e4)
    assert 19 < plan.value_bps < 20


def test_size_optimizer_finds_the_size_where_rounding_works_out(tri_profitable, books, rules, fees):
    naive = simulate_cycle(tri_profitable, D("95"), books, rules, fees)
    best = optimize_size(tri_profitable, D("10"), D("100"), books, rules, fees, steps=90, count_dust=False)
    assert best.reject is None
    assert best.profit > naive.profit
    assert best.start_amount == D("100")      # -0.2 bps here vs -12.9 bps at 95


def test_simulation_rejects_below_min_notional(tri_profitable, books, rules, fees):
    plan = simulate_cycle(tri_profitable, D("4"), books, rules, fees)
    assert plan.reject and "minNotional" in plan.reject


def test_fee_overrides_and_bnb_discount():
    f = FeeModel(0.001, bnb_discount=True, overrides={"FREEUSDT": 0.0})
    assert f.rate("BTCUSDT") == pytest.approx(0.00075)
    assert f.rate("FREEUSDT") == 0.0


def test_unprofitable_book_has_negative_edge(tri_profitable, books, fees):
    set_books(books, ETHUSDT=(3000.0, 50.0, 3000.01, 50.0))
    ev = CompiledTriangle(tri_profitable, fees).evaluate(books)
    assert ev.edge_bps < -25  # three 10 bps fees on a fair loop
