"""Discover every three-market cycle that starts and ends in a start asset."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .market import BUY, SELL, SymbolRules


@dataclass(frozen=True)
class Leg:
    symbol: str
    side: str          # BUY: spend quote, receive base. SELL: spend base, receive quote.
    src: str           # asset we hold before the leg
    dst: str           # asset we hold after the leg

    @property
    def is_buy(self) -> bool:
        return self.side == BUY


@dataclass(frozen=True)
class Triangle:
    legs: tuple[Leg, Leg, Leg]

    @property
    def start(self) -> str:
        return self.legs[0].src

    @property
    def id(self) -> str:
        a, b, c = self.legs
        return f"{a.src}>{a.dst}>{b.dst}>{c.dst}"

    @property
    def symbols(self) -> tuple[str, str, str]:
        return tuple(leg.symbol for leg in self.legs)  # type: ignore[return-value]

    def __str__(self) -> str:
        legs = " ".join(f"{leg.side[0]}:{leg.symbol}" for leg in self.legs)
        return f"{self.id} [{legs}]"


def find_triangles(
    rules: dict[str, SymbolRules],
    start_assets: Iterable[str],
    exclude_assets: Iterable[str] = (),
    include_only_assets: Iterable[str] = (),
) -> list[Triangle]:
    exclude = set(exclude_assets)
    only = set(include_only_assets)
    starts = list(start_assets)

    def allowed(asset: str) -> bool:
        if asset in exclude:
            return False
        return not only or asset in only or asset in starts

    # edges[asset] -> legs that spend `asset`
    edges: dict[str, list[Leg]] = defaultdict(list)
    for r in rules.values():
        if not (allowed(r.base) and allowed(r.quote)):
            continue
        edges[r.quote].append(Leg(r.symbol, BUY, r.quote, r.base))
        edges[r.base].append(Leg(r.symbol, SELL, r.base, r.quote))

    out: list[Triangle] = []
    for a in starts:
        for l1 in edges.get(a, ()):
            x = l1.dst
            for l2 in edges.get(x, ()):
                y = l2.dst
                if y == a:
                    continue
                for l3 in edges.get(y, ()):
                    if l3.dst == a:
                        out.append(Triangle((l1, l2, l3)))
    return out
