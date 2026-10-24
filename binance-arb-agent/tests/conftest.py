"""Shared fixtures: a small synthetic USDT/BTC/ETH market with real-format rules."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arbagent.book import BookStore, Valuer  # noqa: E402
from arbagent.config import Config, PaperConfig  # noqa: E402
from arbagent.graph import find_triangles  # noqa: E402
from arbagent.market import parse_exchange_info  # noqa: E402
from arbagent.pricing import FeeModel  # noqa: E402


def raw_symbol(symbol: str, base: str, quote: str, tick: str, step: str, min_notional: str,
               market_step: str = "0", qoq: bool = True, legacy_min_notional: bool = False) -> dict:
    notional = ({"filterType": "MIN_NOTIONAL", "minNotional": min_notional, "applyToMarket": True,
                 "avgPriceMins": 5} if legacy_min_notional else
                {"filterType": "NOTIONAL", "minNotional": min_notional, "applyMinToMarket": True,
                 "maxNotional": "9000000", "applyMaxToMarket": False, "avgPriceMins": 5})
    return {
        "symbol": symbol, "status": "TRADING", "baseAsset": base, "quoteAsset": quote,
        "quoteAssetPrecision": 8, "isSpotTradingAllowed": True,
        "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET"], "quoteOrderQtyMarketAllowed": qoq,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": tick, "maxPrice": "1000000", "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": step, "maxQty": "100000", "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "1000", "stepSize": market_step},
            notional,
        ],
    }


EXCHANGE_INFO = {"symbols": [
    raw_symbol("BTCUSDT", "BTC", "USDT", "0.01", "0.00001", "5"),
    raw_symbol("ETHUSDT", "ETH", "USDT", "0.01", "0.0001", "5"),
    raw_symbol("ETHBTC", "ETH", "BTC", "0.00001", "0.0001", "0.0001"),
]}


def set_books(books: BookStore, ts: float = 0.0, **quotes: tuple[float, float, float, float]) -> None:
    """set_books(books, BTCUSDT=(bid, bid_qty, ask, ask_qty), ...)"""
    for sym, (b, bq, a, aq) in quotes.items():
        q = books.quotes.get(sym)
        uid = (q.update_id + 1) if q else 1
        books.update(sym, uid, b, bq, a, aq, ts)


# BUY BTCUSDT @50000, BUY ETHBTC @0.06, SELL ETHUSDT @3015 => +50 bps gross.
PROFITABLE = dict(
    BTCUSDT=(49999.99, 2.0, 50000.0, 2.0),
    ETHBTC=(0.05999, 50.0, 0.06, 50.0),
    ETHUSDT=(3015.0, 50.0, 3015.01, 50.0),
)


@pytest.fixture
def rules():
    return parse_exchange_info(EXCHANGE_INFO)


@pytest.fixture
def books(rules):
    b = BookStore(rules)
    set_books(b, **PROFITABLE)
    return b


@pytest.fixture
def fees():
    return FeeModel(0.001)


@pytest.fixture
def triangles(rules):
    return find_triangles(rules, ["USDT"])


@pytest.fixture
def tri_profitable(triangles):
    return next(t for t in triangles if t.id == "USDT>BTC>ETH>USDT")


@pytest.fixture
def valuer(rules, books):
    return Valuer(rules, books)


@pytest.fixture
def paper_cfg():
    return PaperConfig(rtt_ms=100.0, rtt_jitter_ms=0.0, depth_penalty_bps=10.0,
                       starting_balances={"USDT": 1000.0}, seed=1)


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.general.data_dir = str(tmp_path / "data")
    c.general.kill_switch_file = str(tmp_path / "STOP")
    c.universe.min_24h_quote_volume_usdt = 0
    return c
