"""Configuration: dataclass defaults, overridable from a TOML file.

API keys are never read from the config file, only from the environment
(BINANCE_API_KEY / BINANCE_API_SECRET), so the file is safe to commit.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

MAINNET_REST = "https://api.binance.com"
PUBLIC_REST = "https://data-api.binance.vision"
PUBLIC_STREAM = "wss://data-stream.binance.vision"
MAINNET_STREAM = "wss://stream.binance.com:9443"
TESTNET_REST = "https://testnet.binance.vision"
TESTNET_STREAM = "wss://stream.testnet.binance.vision"


@dataclass
class GeneralConfig:
    start_assets: list[str] = field(default_factory=lambda: ["USDT"])
    data_dir: str = "data"
    kill_switch_file: str = "STOP"
    status_interval_s: float = 10.0
    log_level: str = "INFO"


@dataclass
class ExchangeConfig:
    rest_url: str = MAINNET_REST
    public_rest_url: str = PUBLIC_REST
    stream_url: str = PUBLIC_STREAM
    testnet: bool = False
    recv_window_ms: int = 5000
    request_timeout_s: float = 5.0
    # Refuse to trade with a key that can withdraw funds.
    require_no_withdraw_permission: bool = True


@dataclass
class UniverseConfig:
    exclude_assets: list[str] = field(default_factory=list)
    include_only_assets: list[str] = field(default_factory=list)
    # Thin markets produce phantom edges that never fill. 0 disables the filter.
    min_24h_quote_volume_usdt: float = 250_000.0
    max_streams_per_connection: int = 200


@dataclass
class StrategyConfig:
    taker_fee: float = 0.001
    # Binance charges 75% of the standard fee when it is paid in BNB.
    bnb_fee_discount: bool = False
    fee_overrides: dict[str, float] = field(default_factory=dict)
    # Net edge (after fees, at top of book) for a cycle to count as an opportunity.
    min_edge_bps: float = 0.5
    # Only take this fraction of the visible top-of-book size: improves fill odds.
    capacity_fraction: float = 0.5
    # Per start asset, in units of that asset.
    max_trade: dict[str, float] = field(default_factory=lambda: {"USDT": 50.0})
    min_trade: dict[str, float] = field(default_factory=lambda: {"USDT": 6.0})
    # How far legs 2 and 3 may be priced through the touch to raise fill odds.
    leg_slippage_bps: float = 3.0
    # Count rounding dust at its marked value when judging a cycle (it is swept
    # back to the start asset later). False treats dust as lost: stricter.
    count_dust: bool = True
    # Smallest absolute profit (start-asset units) worth sending orders for.
    min_profit: dict[str, float] = field(default_factory=lambda: {"USDT": 0.002})


@dataclass
class BrainConfig:
    # Weight of the pooled (all-triangle) fill rate when judging one triangle.
    prior_strength: float = 5.0
    # Starting belief that a filled leg 1 leads to a complete cycle.
    prior_complete_prob: float = 0.5
    # Per-observation decay, so the model adapts when the market changes.
    forgetting: float = 0.995
    # "auto": "thompson" (explores) in paper/replay, "lcb" (cautious) live.
    # Or force "lcb", "mean" or "thompson".
    policy: str = "auto"
    risk_aversion_z: float = 1.0
    min_ev_bps: float = 0.5
    safety_margin_bps: float = 1.0
    triangle_cooldown_s: float = 5.0
    # Assumed price drift while the remaining legs are in flight, before data exists.
    prior_adverse_move_bps: float = 8.0


@dataclass
class RiskConfig:
    daily_loss_limit_usdt: float = 10.0
    max_consecutive_failures: int = 3
    failure_pause_s: float = 300.0
    max_cycles_per_minute: int = 10
    max_stuck_value_usdt: float = 10.0
    max_daily_volume_usdt: float = 5_000.0
    stream_stale_s: float = 10.0


@dataclass
class PaperConfig:
    # Round trip to Binance's matching engine. Measure yours with `arbagent ping`.
    rtt_ms: float = 120.0
    rtt_jitter_ms: float = 30.0
    # Extra cost applied to market-order size beyond the visible top level.
    depth_penalty_bps: float = 10.0
    starting_balances: dict[str, float] = field(default_factory=lambda: {"USDT": 1000.0})
    seed: int = 7


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)

    @property
    def data_path(self) -> Path:
        return Path(self.general.data_dir)

    def validate(self) -> None:
        s, b, r = self.strategy, self.brain, self.risk
        if not self.general.start_assets:
            raise ValueError("general.start_assets must not be empty")
        if not 0 <= s.taker_fee < 0.01:
            raise ValueError("strategy.taker_fee looks wrong (expected e.g. 0.001)")
        if not 0 < s.capacity_fraction <= 1:
            raise ValueError("strategy.capacity_fraction must be in (0, 1]")
        if s.leg_slippage_bps < 0 or s.leg_slippage_bps > 100:
            raise ValueError("strategy.leg_slippage_bps must be in [0, 100]")
        for asset in self.general.start_assets:
            if asset not in s.max_trade:
                raise ValueError(f"strategy.max_trade has no entry for start asset {asset}")
        if b.policy not in ("auto", "lcb", "mean", "thompson"):
            raise ValueError("brain.policy must be auto, lcb, mean or thompson")
        if not 0 < b.prior_complete_prob < 1:
            raise ValueError("brain.prior_complete_prob must be in (0, 1)")
        if not 0.5 < b.forgetting <= 1:
            raise ValueError("brain.forgetting must be in (0.5, 1]")
        if r.max_consecutive_failures < 1:
            raise ValueError("risk.max_consecutive_failures must be >= 1")
        if self.universe.max_streams_per_connection > 1024:
            raise ValueError("Binance allows at most 1024 streams per connection")

    def use_testnet(self) -> None:
        self.exchange.testnet = True
        self.exchange.rest_url = TESTNET_REST
        self.exchange.public_rest_url = TESTNET_REST
        self.exchange.stream_url = TESTNET_STREAM
        # Testnet has no /sapi endpoints, so the permission check cannot run.
        self.exchange.require_no_withdraw_permission = False


def _merge(obj: Any, data: dict[str, Any], path: str = "") -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"config section {path}{key} must be a table")
            _merge(current, value, f"{path}{key}.")
        else:
            setattr(obj, key, value)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg = Config()
    if path:
        with open(path, "rb") as fh:
            _merge(cfg, tomllib.load(fh))
    cfg.general.start_assets = [a.upper() for a in cfg.general.start_assets]
    cfg.validate()
    return cfg


def api_credentials() -> tuple[str | None, str | None]:
    return os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET")
