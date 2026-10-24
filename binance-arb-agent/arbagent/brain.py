"""The agent's decision model.

A visible edge is not a profit. Each attempt ends one of three ways:

  miss     leg 1 does not fill (someone was faster). Costs nothing.
  complete all three legs fill at their limit prices. Earns roughly the edge.
  broken   leg 1 fills but a later leg does not, so the agent finishes at
           market or unwinds. Costs fees, spread and price drift.

The brain learns, per triangle, the probability that a filled leg 1 leads to
a complete cycle, plus the realised cost of broken ones and the slippage on
complete ones. Estimates are shrunk toward the all-triangle average when a
triangle has little history, and old observations decay so the model follows
the market. It trades only when the expected value is positive:

    EV = p * (edge + slippage) - (1 - p) * broken_cost - safety_margin
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import BrainConfig

MISS, COMPLETE, BROKEN = "miss", "complete", "broken"


@dataclass
class Stats:
    n: float = 0.0              # decayed count of leg-1 fills
    s: float = 0.0              # decayed count of complete cycles among them
    attempts: int = 0           # all attempts, including misses
    misses: int = 0
    fail_cost_bps: float | None = None
    fail_n: float = 0.0
    slip_bps: float | None = None
    slip_n: float = 0.0
    pnl: float = 0.0            # realised, in start-asset units
    last_attempt: float = 0.0
    cooldown_until: float = 0.0


@dataclass
class Decision:
    execute: bool
    reason: str
    edge_bps: float
    ev_bps: float = 0.0
    p_complete: float = 0.0
    broken_cost_bps: float = 0.0
    slip_bps: float = 0.0

    def as_dict(self) -> dict[str, float | str | bool]:
        return asdict(self)


def _ewma(old: float | None, new: float, alpha: float) -> float:
    return new if old is None else old + alpha * (new - old)


def _blend(prior: float, learned: float | None, n: float, k: float) -> float:
    if learned is None or n <= 0:
        return prior
    return (k * prior + n * learned) / (k + n)


@dataclass
class Brain:
    cfg: BrainConfig
    global_stats: Stats = field(default_factory=Stats)
    tri: dict[str, Stats] = field(default_factory=dict)
    rtt_ms: float | None = None
    rng: random.Random = field(default_factory=random.Random)

    # -- estimates -----------------------------------------------------------
    def _stats(self, tri_id: str) -> Stats:
        st = self.tri.get(tri_id)
        if st is None:
            st = self.tri[tri_id] = Stats()
        return st

    def completion_posterior(self, tri_id: str) -> tuple[float, float]:
        """Beta(alpha, beta) for P(complete | leg 1 filled)."""
        g = self.global_stats
        k = self.cfg.prior_strength
        p0 = self.cfg.prior_complete_prob
        g_alpha = p0 * k + g.s
        g_beta = (1 - p0) * k + (g.n - g.s)
        g_p = g_alpha / (g_alpha + g_beta)
        st = self.tri.get(tri_id) or Stats()
        return g_p * k + st.s, (1 - g_p) * k + (st.n - st.s)

    def p_complete(self, tri_id: str) -> float:
        a, b = self.completion_posterior(tri_id)
        if self.cfg.policy == "thompson":
            return self.rng.betavariate(a, b)
        mean = a / (a + b)
        if self.cfg.policy == "mean":
            return mean
        std = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
        return max(0.0, mean - self.cfg.risk_aversion_z * std)

    def broken_cost_bps(self, tri_id: str, book_prior_bps: float) -> float:
        g = self.global_stats
        k = self.cfg.prior_strength
        prior = _blend(book_prior_bps, g.fail_cost_bps, g.fail_n, k)
        st = self.tri.get(tri_id)
        return prior if st is None else _blend(prior, st.fail_cost_bps, st.fail_n, k)

    def slip_bps(self, tri_id: str) -> float:
        g = self.global_stats
        k = self.cfg.prior_strength
        prior = _blend(0.0, g.slip_bps, g.slip_n, k)
        st = self.tri.get(tri_id)
        return prior if st is None else _blend(prior, st.slip_bps, st.slip_n, k)

    # -- decision ------------------------------------------------------------
    def decide(self, tri_id: str, edge_bps: float, book_prior_bps: float, now: float) -> Decision:
        st = self.tri.get(tri_id)
        if st is not None:
            if now < st.cooldown_until:
                return Decision(False, "cooldown", edge_bps)
            if st.attempts >= 10 and st.misses / st.attempts >= 0.9:
                return Decision(False, "phantom: leg 1 almost never fills", edge_bps)
        p = self.p_complete(tri_id)
        broken = self.broken_cost_bps(tri_id, book_prior_bps)
        slip = self.slip_bps(tri_id)
        ev = p * (edge_bps + slip) - (1 - p) * broken - self.cfg.safety_margin_bps
        ok = ev >= self.cfg.min_ev_bps
        reason = "positive expected value" if ok else "expected value too low"
        return Decision(ok, reason, edge_bps, ev, p, broken, slip)

    # -- learning ------------------------------------------------------------
    def record(self, tri_id: str, outcome: str, expected_bps: float, realized_bps: float,
               pnl: float, now: float) -> None:
        f = self.cfg.forgetting
        alpha = 0.2
        for st in (self._stats(tri_id), self.global_stats):
            st.attempts += 1
            st.last_attempt = now
            st.pnl += pnl
            if outcome == MISS:
                st.misses += 1
                continue
            st.n = st.n * f + 1
            st.s = st.s * f + (1 if outcome == COMPLETE else 0)
            if outcome == COMPLETE:
                st.slip_bps = _ewma(st.slip_bps, realized_bps - expected_bps, alpha)
                st.slip_n = st.slip_n * f + 1
            else:
                st.fail_cost_bps = _ewma(st.fail_cost_bps, -realized_bps, alpha)
                st.fail_n = st.fail_n * f + 1
        st = self._stats(tri_id)
        if outcome != COMPLETE:
            st.cooldown_until = now + self.cfg.triangle_cooldown_s

    def record_rtt(self, ms: float) -> None:
        self.rtt_ms = _ewma(self.rtt_ms, ms, 0.1)

    # -- persistence ---------------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "global": asdict(self.global_stats),
            "triangles": {k: asdict(v) for k, v in self.tri.items()},
            "rtt_ms": self.rtt_ms,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)

    def load(self, path: Path) -> bool:
        if not path.exists():
            return False
        data = json.loads(path.read_text())
        self.global_stats = Stats(**data.get("global", {}))
        self.tri = {k: Stats(**v) for k, v in data.get("triangles", {}).items()}
        # Cooldowns are wall-clock; a replay or restart should not inherit them.
        for st in [self.global_stats, *self.tri.values()]:
            st.cooldown_until = 0.0
        self.rtt_ms = data.get("rtt_ms")
        return True

    def summary(self) -> dict[str, float | int | None]:
        g = self.global_stats
        a, b = self.completion_posterior("__none__")
        return {
            "attempts": g.attempts,
            "misses": g.misses,
            "leg1_fills": round(g.n, 2),
            "p_complete_pooled": round(a / (a + b), 3),
            "broken_cost_bps": None if g.fail_cost_bps is None else round(g.fail_cost_bps, 2),
            "slip_bps": None if g.slip_bps is None else round(g.slip_bps, 2),
            "rtt_ms": None if self.rtt_ms is None else round(self.rtt_ms, 1),
        }
