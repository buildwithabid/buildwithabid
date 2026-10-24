import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from arbagent.agent import Agent, filter_universe
from arbagent.config import load_config
from arbagent.market import parse_exchange_info
from arbagent.replay import replay

from conftest import EXCHANGE_INFO

ROOT = Path(__file__).resolve().parents[1]

FAIR = {"BTCUSDT": ("49999.99", "2", "50000", "2"),
        "ETHBTC": ("0.05999", "50", "0.06", "50"),
        "ETHUSDT": ("3000", "50", "3000.01", "50")}


def write_recording(path: Path, events: list[tuple[float, str, tuple[str, str, str, str]]]) -> None:
    uid = {}
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps({"type": "header", "version": 1, "start_assets": ["USDT"],
                             "exchange_info": EXCHANGE_INFO}) + "\n")
        for ts, sym, (b, bq, a, aq) in events:
            uid[sym] = uid.get(sym, 0) + 1
            fh.write(json.dumps([ts, sym, uid[sym], b, bq, a, aq]) + "\n")


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.general.start_assets == ["USDT"]
    assert cfg.strategy.max_trade["USDT"] == 50.0


def test_unknown_config_key_is_an_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[strategy]\nmax_trades = 5\n")
    with pytest.raises(ValueError, match="unknown config key: strategy.max_trades"):
        load_config(p)


def test_validation_requires_max_trade_for_each_start_asset(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[general]\nstart_assets = ["BTC"]\n')
    with pytest.raises(ValueError, match="max_trade"):
        load_config(p)


def test_universe_filter_converts_volume_to_usdt(rules):
    tickers = [
        {"symbol": "BTCUSDT", "lastPrice": "50000", "quoteVolume": "1000000"},
        {"symbol": "ETHUSDT", "lastPrice": "3000", "quoteVolume": "100"},       # thin
        {"symbol": "ETHBTC", "lastPrice": "0.06", "quoteVolume": "10"},         # 10 BTC = 500k USDT
    ]
    kept = filter_universe(rules, tickers, 250_000)
    assert set(kept) == {"BTCUSDT", "ETHBTC"}


def test_agent_build_watches_the_right_markets(cfg):
    agent = Agent(cfg, "paper", persist=False)
    agent.build(parse_exchange_info(EXCHANGE_INFO))
    assert len(agent.triangles) == 2
    assert set(agent.symbols) == {"BTCUSDT", "ETHUSDT", "ETHBTC"}
    assert cfg.brain.policy == "thompson"      # "auto" explores in paper mode


async def test_replay_trades_only_the_liquidity_that_exists(cfg, tmp_path):
    cfg.brain.policy = "mean"
    cfg.brain.prior_complete_prob = 0.95
    cfg.strategy.max_trade = {"USDT": 1000.0}
    cfg.strategy.capacity_fraction = 1.0
    cfg.paper.rtt_jitter_ms = 0
    events = [(100.0 + i * 0.001, s, q) for i, (s, q) in enumerate(FAIR.items())]
    events += [
        (110.0, "ETHUSDT", ("3015", "0.4", "3015.01", "50")),     # +50 bps gross, only 0.4 ETH bid
        (110.5, "BTCUSDT", ("49999.99", "2.1", "50000", "2")),    # re-evaluates while still there
        (110.8, "ETHBTC", ("0.05999", "51", "0.06", "50")),
        (111.0, "ETHUSDT", ("3000", "50", "3000.01", "50")),      # gone
        (120.0, "BTCUSDT", ("49999.99", "2", "50000", "2")),
    ]
    path = tmp_path / "rec.jsonl.gz"
    write_recording(path, events)
    agent = await replay(cfg, path)
    assert agent.outcomes["complete"] >= 1
    assert agent.outcomes["broken"] == 0
    assert agent.pnl["USDT"] > 0
    first = agent.results[0]
    assert first.finished - first.started == pytest.approx(3 * cfg.paper.rtt_ms / 1000)  # 3 round trips
    sold = sum(f.executed_qty for r in agent.results for f in r.fills if f.symbol == "ETHUSDT")
    assert 0 < sold <= Decimal("0.4")       # our fills used up the visible bid
    assert agent.scanner.stats.episodes == 1


async def test_replay_with_real_fees_and_fair_books_never_trades(cfg, tmp_path):
    events = [(100.0 + i, s, q) for i, (s, q) in enumerate(list(FAIR.items()) * 20)]
    path = tmp_path / "rec.jsonl.gz"
    write_recording(path, events)
    agent = await replay(cfg, path)
    assert sum(agent.outcomes.values()) == 0
    assert agent.scanner.stats.episodes == 0
