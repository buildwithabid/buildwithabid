"""Record live book-ticker data to a file and replay it through the agent.

A recording is gzip'd JSON lines. Line 1 is a header holding the exchange
rules for the recorded symbols; every other line is
[receive_time, symbol, update_id, bid, bid_qty, ask, ask_qty].

Replay drives the real agent (paper gateway) on a virtual clock, so simulated
network latency lands on the book as it actually was at that later moment.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path
from typing import IO, Any

from .agent import Agent
from .clock import VirtualClock
from .config import Config
from .market import parse_exchange_info

log = logging.getLogger("arbagent")


class Recorder:
    def __init__(self, agent: Agent, path: Path):
        self.agent = agent
        self.path = path
        self.fh: IO[str] | None = None
        self.lines = 0

    def __call__(self, ts: float, d: dict[str, Any]) -> None:
        if self.fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = gzip.open(self.path, "wt")
            wanted = set(self.agent.symbols)
            header = {
                "type": "header", "version": 1, "created": time.time(),
                "start_assets": self.agent.cfg.general.start_assets,
                "exchange_info": {"symbols": [s for s in self.agent.exchange_info.get("symbols", [])
                                              if s["symbol"] in wanted]},
            }
            self.fh.write(json.dumps(header) + "\n")
        self.fh.write(json.dumps([round(ts, 4), d["s"], d["u"], d["b"], d["B"], d["a"], d["A"]]) + "\n")
        self.lines += 1

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()


async def record(cfg: Config, out: Path, duration: float) -> int:
    agent = Agent(cfg, "scan", persist=False)
    rec = Recorder(agent, out)
    agent.on_raw = rec
    try:
        await agent.run(duration)
    finally:
        rec.close()
    log.info("recorded %d updates to %s", rec.lines, out)
    return rec.lines


async def replay(cfg: Config, path: Path, status_every_s: float = 60.0) -> Agent:
    with gzip.open(path, "rt") as fh:
        header = json.loads(fh.readline())
        if header.get("type") != "header":
            raise ValueError(f"{path} is not an arbagent recording")
        first = fh.readline()
        if not first:
            raise ValueError(f"{path} has no market data")
        clock = VirtualClock(start=json.loads(first)[0])
        cfg.general.start_assets = [a for a in cfg.general.start_assets if a in header["start_assets"]] \
            or header["start_assets"]
        agent = Agent(cfg, "replay", clock=clock, persist=False)
        agent.build(parse_exchange_info(header["exchange_info"]))
        books = agent.books
        next_status = clock.now() + status_every_s
        n = 0
        for line in [first, *fh]:
            ts, sym, uid, b, bq, a, aq = json.loads(line)
            await clock.advance_to(ts, agent.task)
            if books.update(sym, uid, float(b), float(bq), float(a), float(aq), ts):
                agent.on_update(sym)
                if agent.task is not None and not agent.task.done():
                    await clock.settle(agent.task)
            n += 1
            if ts >= next_status:
                log.info(agent.status_line(status_every_s))
                next_status = ts + status_every_s
        # let an in-flight cycle finish
        while agent.task is not None and not agent.task.done():
            wake = clock.next_wake()
            await clock.advance_to(wake if wake is not None else clock.now() + 1.0, agent.task)
    log.info("replayed %d updates over %.0f s of market time", n, clock.now() - json.loads(first)[0])
    return agent
