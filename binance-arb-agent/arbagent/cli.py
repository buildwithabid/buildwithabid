"""Command line: arbagent {scan,paper,live,record,replay,report,ping}."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
from pathlib import Path

from .agent import Agent
from .brain import BROKEN, COMPLETE, MISS
from .config import Config, load_config
from .exchange import BinanceAPIError, BinanceREST, measure_rtt
from .journal import Journal
from .replay import record, replay

LIVE_WARNING = """
LIVE TRADING. Real orders, real money.
  * Retail triangular arbitrage on Binance is usually unprofitable: three taker
    fees (0.225%-0.3% per cycle) exceed almost every visible edge, and the
    edges that do exceed fees are mostly taken by co-located firms within
    milliseconds.
  * Run `scan` and `paper` first and read the journal before risking funds.
  * Use a trade-only API key with withdrawals disabled and an IP whitelist.
  * Create the file named by general.kill_switch_file (default ./STOP) to stop
    trading immediately.
"""


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")


def _summary(agent: Agent) -> dict[str, object]:
    st = agent.scanner.stats
    return {
        "triangles": len(agent.triangles),
        "markets": len(agent.symbols),
        "opportunity_episodes": st.episodes,
        "lifetime_ms": {k: round(v, 1) for k, v in st.lifetime_quantiles().items()},
        "round_trip_ms_assumed": agent.brain.rtt_ms or agent.cfg.paper.rtt_ms,
        "capturable_fraction": agent.capturable_fraction(),
        "most_frequent": st.by_triangle.most_common(5),
        "cycles": {k: agent.outcomes[k] for k in (COMPLETE, BROKEN, MISS)},
        "pnl": {k: round(v, 8) for k, v in agent.pnl.items()},
        "skipped": dict(agent.skips.most_common(8)),
        "brain": agent.brain.summary(),
        "risk": agent.risk.snapshot(),
    }


def cmd_run(cfg: Config, mode: str, args: argparse.Namespace) -> int:
    if mode == "live":
        print(LIVE_WARNING, file=sys.stderr)
        if not args.confirm_live:
            print("Refusing to trade: pass --confirm-live to acknowledge the above.", file=sys.stderr)
            return 2
    agent = Agent(cfg, mode)
    asyncio.run(agent.run(args.duration))
    print(json.dumps(_summary(agent), indent=2, default=str))
    return 0


def cmd_record(cfg: Config, args: argparse.Namespace) -> int:
    asyncio.run(record(cfg, Path(args.out), args.duration))
    return 0


def cmd_replay(cfg: Config, args: argparse.Namespace) -> int:
    agent = asyncio.run(replay(cfg, Path(args.path)))
    print(json.dumps(_summary(agent), indent=2, default=str))
    return 0


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    path = cfg.data_path / "journal.sqlite"
    if not path.exists():
        print(f"no journal at {path}", file=sys.stderr)
        return 1
    j = Journal(path, args.mode)
    print(json.dumps(j.report(args.mode), indent=2, default=str))
    j.close()
    return 0


def cmd_ping(cfg: Config, args: argparse.Namespace) -> int:
    async def go() -> list[float]:
        async with BinanceREST(args.url or cfg.exchange.rest_url) as c:
            return await measure_rtt(c, args.samples)

    rtts = asyncio.run(go())
    print(f"round trip ms: min {min(rtts):.1f}  median {statistics.median(rtts):.1f}  max {max(rtts):.1f}")
    print("Set paper.rtt_ms to the median so paper trading reflects your real latency.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arbagent", description="Autonomous triangular-arbitrage agent for Binance spot")
    p.add_argument("-c", "--config", help="TOML config file (see config.example.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, help_ in (("scan", "watch live markets and measure opportunities (no keys, no orders)"),
                        ("paper", "full agent with simulated fills against live markets (no keys)"),
                        ("live", "trade real funds (needs keys and --confirm-live)")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--duration", type=float, help="seconds to run (default: until Ctrl-C)")
        if name == "live":
            sp.add_argument("--confirm-live", action="store_true", help="acknowledge the risks")
            sp.add_argument("--testnet", action="store_true", help="use testnet.binance.vision")

    sp = sub.add_parser("record", help="record live book tickers for later replay")
    sp.add_argument("--out", required=True)
    sp.add_argument("--duration", type=float, required=True)

    sp = sub.add_parser("replay", help="run the paper agent over a recording")
    sp.add_argument("path")

    sp = sub.add_parser("report", help="summarise the journal")
    sp.add_argument("--mode", default="paper", choices=["scan", "paper", "live"])

    sp = sub.add_parser("ping", help="measure REST round trip to Binance")
    sp.add_argument("--samples", type=int, default=10)
    sp.add_argument("--url", help="endpoint to ping (default exchange.rest_url)")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "testnet", False):
        cfg.use_testnet()
    _setup_logging(cfg.general.log_level)

    try:
        if args.cmd in ("scan", "paper", "live"):
            return cmd_run(cfg, args.cmd, args)
        cmds = {"record": cmd_record, "replay": cmd_replay, "report": cmd_report, "ping": cmd_ping}
        return cmds[args.cmd](cfg, args)
    except BinanceAPIError as e:
        print(f"error: Binance said {e}", file=sys.stderr)
        if e.status == 451:
            print("hint: Binance blocks this region. Scan/paper/record use data-api.binance.vision, which "
                  "usually still works; trading needs an eligible location.", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
