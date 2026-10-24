"""Incremental opportunity detection.

On each book update only the triangles that use the updated symbol are
re-priced. The scanner also measures how long each opportunity lives, which is
the number that decides whether a given setup can capture any of them.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Protocol

from .book import BookStore
from .pricing import CompiledTriangle, Evaluation


class Clock(Protocol):
    def now(self) -> float: ...


@dataclass
class Opportunity:
    ct: CompiledTriangle
    ev: Evaluation
    first_seen: float
    seen_at: float

    @property
    def age_ms(self) -> float:
        return (self.seen_at - self.first_seen) * 1000


@dataclass
class Episode:
    triangle: str
    start: float
    end: float
    max_edge_bps: float
    max_capacity: float

    @property
    def lifetime_ms(self) -> float:
        return (self.end - self.start) * 1000


@dataclass
class ScanStats:
    evaluations: int = 0
    episodes: int = 0
    lifetimes_ms: deque[float] = field(default_factory=lambda: deque(maxlen=5000))
    by_triangle: Counter[str] = field(default_factory=Counter)
    best_edge_bps: float = float("-inf")
    best_triangle: str = ""
    best_gross_bps: float = float("-inf")

    def lifetime_quantiles(self) -> dict[str, float]:
        data = sorted(self.lifetimes_ms)
        if not data:
            return {}
        pick = lambda p: data[min(len(data) - 1, int(p * len(data)))]  # noqa: E731
        return {"p50": pick(0.5), "p90": pick(0.9), "max": data[-1], "mean": statistics.fmean(data)}

    def reset_window(self) -> None:
        self.best_edge_bps = float("-inf")
        self.best_triangle = ""
        self.best_gross_bps = float("-inf")


class Scanner:
    def __init__(self, triangles: list[CompiledTriangle], books: BookStore, min_edge_bps: float, clock: Clock):
        self.books = books
        self.min_edge_bps = min_edge_bps
        self.clock = clock
        self.triangles = triangles
        self.by_symbol: dict[str, list[CompiledTriangle]] = defaultdict(list)
        for ct in triangles:
            for s in ct.symbols:
                self.by_symbol[s].append(ct)
        self.active: dict[str, list[float]] = {}   # id -> [first_seen, max_edge, max_capacity]
        self.stats = ScanStats()
        self.closed: deque[Episode] = deque(maxlen=10000)

    def on_update(self, symbol: str) -> list[Opportunity]:
        now = self.clock.now()
        out: list[Opportunity] = []
        stats = self.stats
        for ct in self.by_symbol.get(symbol, ()):
            ev = ct.evaluate(self.books)
            stats.evaluations += 1
            if ev is None:
                self._close(ct.id, now)
                continue
            edge = ev.edge_bps
            if edge > stats.best_edge_bps:
                stats.best_edge_bps = edge
                stats.best_triangle = ct.id
                fee_mult = ct.fee_mult[0] * ct.fee_mult[1] * ct.fee_mult[2]
                stats.best_gross_bps = (ev.rate / fee_mult - 1.0) * 1e4
            if edge >= self.min_edge_bps:
                ep = self.active.get(ct.id)
                if ep is None:
                    ep = self.active[ct.id] = [now, edge, ev.capacity]
                else:
                    ep[1] = max(ep[1], edge)
                    ep[2] = max(ep[2], ev.capacity)
                out.append(Opportunity(ct, ev, ep[0], now))
            else:
                self._close(ct.id, now)
        return out

    def _close(self, tri_id: str, now: float) -> None:
        ep = self.active.pop(tri_id, None)
        if ep is None:
            return
        episode = Episode(tri_id, ep[0], now, ep[1], ep[2])
        self.closed.append(episode)
        self.stats.episodes += 1
        self.stats.lifetimes_ms.append(episode.lifetime_ms)
        self.stats.by_triangle[tri_id] += 1

    def drain_closed(self) -> list[Episode]:
        out = list(self.closed)
        self.closed.clear()
        return out
