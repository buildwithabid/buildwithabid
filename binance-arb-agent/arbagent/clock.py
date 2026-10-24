"""Real and virtual clocks.

Everything time-dependent takes a clock, so a recorded market session can be
replayed through the exact same agent code with simulated latency.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time


class RealClock:
    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class VirtualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._heap: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self.waiting: set[asyncio.Task[object]] = set()

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self._now + max(0.0, seconds), next(self._seq), fut))
        task = asyncio.current_task()
        if task is not None:
            self.waiting.add(task)  # type: ignore[arg-type]
        try:
            await fut
        finally:
            if task is not None:
                self.waiting.discard(task)  # type: ignore[arg-type]

    async def settle(self, task: asyncio.Task[object] | None, max_yields: int = 1000) -> None:
        """Let `task` run until it finishes or blocks on this clock."""
        for _ in range(max_yields):
            if task is None or task.done() or task in self.waiting:
                return
            await asyncio.sleep(0)

    async def advance_to(self, t: float, task: asyncio.Task[object] | None = None) -> None:
        """Move time forward to `t`, waking sleepers in order along the way."""
        while self._heap and self._heap[0][0] <= t:
            wake, _, fut = heapq.heappop(self._heap)
            self._now = max(self._now, wake)
            if not fut.done():
                fut.set_result(None)
            await asyncio.sleep(0)
            await self.settle(task)
        self._now = max(self._now, t)
        await asyncio.sleep(0)   # always yield, so callers polling in a loop make progress

    def next_wake(self) -> float | None:
        return self._heap[0][0] if self._heap else None
