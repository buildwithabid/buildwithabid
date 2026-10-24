"""Circuit breakers. The agent asks `allow()` before every cycle.

Soft stops clear on their own (pause after a losing streak, daily limits reset
at 00:00 UTC). Hard stops (stuck inventory, unknown order state, IP ban)
need a human: fix the cause, then restart.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .config import RiskConfig


class Clock(Protocol):
    def now(self) -> float: ...


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, cfg: RiskConfig, clock: Clock, kill_switch: Path | None = None):
        self.cfg = cfg
        self.clock = clock
        self.kill_switch = kill_switch
        self.halted: str | None = None
        self.paused_until = 0.0
        self.consecutive_failures = 0
        self.day = utc_day(clock.now())
        self.daily_pnl_usdt = 0.0
        self.daily_volume_usdt = 0.0
        self.stuck_value_usdt = 0.0
        self.stream_healthy = True
        self._cycles: deque[float] = deque()
        self._kill_checked = 0.0
        self._kill_present = False

    def _roll_day(self, now: float) -> None:
        day = utc_day(now)
        if day != self.day:
            self.day = day
            self.daily_pnl_usdt = 0.0
            self.daily_volume_usdt = 0.0

    def _kill_switch_on(self) -> bool:
        if self.kill_switch is None:
            return False
        mono = time.monotonic()
        if mono - self._kill_checked > 0.5:   # stat() at most twice a second
            self._kill_checked = mono
            self._kill_present = self.kill_switch.exists()
        return self._kill_present

    def allow(self) -> tuple[bool, str]:
        now = self.clock.now()
        self._roll_day(now)
        if self._kill_switch_on():
            return False, f"kill switch file {self.kill_switch} present"
        if self.halted:
            return False, f"halted: {self.halted}"
        if not self.stream_healthy:
            return False, "market data stream unhealthy"
        if now < self.paused_until:
            return False, f"paused for {self.paused_until - now:.0f}s after consecutive failures"
        if self.daily_pnl_usdt <= -self.cfg.daily_loss_limit_usdt:
            return False, "daily loss limit reached (resets 00:00 UTC)"
        if self.daily_volume_usdt >= self.cfg.max_daily_volume_usdt:
            return False, "daily volume cap reached (resets 00:00 UTC)"
        while self._cycles and now - self._cycles[0] > 60:
            self._cycles.popleft()
        if len(self._cycles) >= self.cfg.max_cycles_per_minute:
            return False, "cycle rate cap"
        return True, ""

    def on_attempt(self) -> None:
        self._cycles.append(self.clock.now())

    def on_cycle(self, outcome: str, pnl_usdt: float, volume_usdt: float, stuck_value_usdt: float) -> None:
        self._roll_day(self.clock.now())
        self.daily_pnl_usdt += pnl_usdt
        self.daily_volume_usdt += volume_usdt
        self.stuck_value_usdt += stuck_value_usdt
        if outcome == "miss":
            return
        if outcome != "complete" or pnl_usdt < 0:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.cfg.max_consecutive_failures:
                self.paused_until = self.clock.now() + self.cfg.failure_pause_s
                self.consecutive_failures = 0
        else:
            self.consecutive_failures = 0
        if self.stuck_value_usdt > self.cfg.max_stuck_value_usdt:
            self.halt(f"stuck inventory worth {self.stuck_value_usdt:.2f} USDT exceeds limit")

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = reason

    def snapshot(self) -> dict[str, object]:
        return {
            "halted": self.halted,
            "paused_s": max(0.0, round(self.paused_until - self.clock.now(), 1)),
            "daily_pnl_usdt": round(self.daily_pnl_usdt, 4),
            "daily_volume_usdt": round(self.daily_volume_usdt, 2),
            "stuck_value_usdt": round(self.stuck_value_usdt, 4),
        }
