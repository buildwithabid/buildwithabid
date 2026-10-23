"""Cycle pricing.

Two levels:
  * `CompiledTriangle.evaluate` – fast float maths on every book update.
  * `simulate_cycle` – exact Decimal walk-through of the three orders the
    agent would send, with Binance rounding, minimums and fees applied. A
    cycle is only traded if this simulation still shows a profit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .book import BookStore, Quote
from .graph import Triangle
from .market import BUY, D, SymbolRules


class FeeModel:
    def __init__(self, taker_fee: float, bnb_discount: bool = False, overrides: dict[str, float] | None = None):
        self.default = taker_fee * (0.75 if bnb_discount else 1.0)
        self.overrides = dict(overrides or {})

    def rate(self, symbol: str) -> float:
        return self.overrides.get(symbol, self.default)

    def set_default(self, taker_fee: float) -> None:
        self.default = taker_fee


@dataclass
class Evaluation:
    rate: float                     # end/start multiplier after fees at the touch
    capacity: float                 # max start-asset amount the top of book supports
    quotes: tuple[Quote, Quote, Quote]

    @property
    def edge_bps(self) -> float:
        return (self.rate - 1.0) * 1e4


class CompiledTriangle:
    """A triangle with per-leg fee multipliers precomputed for the hot path."""

    __slots__ = ("tri", "id", "symbols", "is_buy", "fee_mult")

    def __init__(self, tri: Triangle, fees: FeeModel) -> None:
        self.tri = tri
        self.id = tri.id
        self.symbols = tri.symbols
        self.is_buy = tuple(leg.is_buy for leg in tri.legs)
        self.fee_mult = tuple(1.0 - fees.rate(leg.symbol) for leg in tri.legs)

    def evaluate(self, books: BookStore) -> Evaluation | None:
        rate = 1.0
        prefix = 1.0          # start-asset -> current-leg-input conversion so far
        capacity = float("inf")
        quotes = []
        for sym, buy, fm in zip(self.symbols, self.is_buy, self.fee_mult):
            q = books.get(sym)
            if q is None:
                return None
            quotes.append(q)
            if buy:
                max_in = q.ask * q.ask_qty      # quote we can spend at the best ask
                step = fm / q.ask
            else:
                max_in = q.bid_qty               # base we can sell at the best bid
                step = q.bid * fm
            cap = max_in / prefix
            if cap < capacity:
                capacity = cap
            prefix *= step
            rate *= step
        return Evaluation(rate, capacity, tuple(quotes))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Exact simulation
# --------------------------------------------------------------------------

@dataclass
class LegPlan:
    symbol: str
    side: str
    price: Decimal          # limit price (on tick grid)
    qty: Decimal            # base quantity (on step grid)
    amount_in: Decimal      # asset spent (quote for BUY, base for SELL)
    amount_out: Decimal     # asset received after fee
    dust: Decimal           # input left behind by rounding


@dataclass
class CyclePlan:
    triangle: Triangle
    start_amount: Decimal
    legs: list[LegPlan] = field(default_factory=list)
    end_amount: Decimal = Decimal(0)
    reject: str | None = None
    # Rounding dust left on legs 2-3, marked through the rest of the cycle at
    # the touch after fees (start-asset units). It stays in the account and
    # the agent's inventory sweep converts it back later.
    dust_value: float = 0.0

    @property
    def spent(self) -> Decimal:
        return self.legs[0].amount_in if self.legs else Decimal(0)

    @property
    def profit(self) -> Decimal:
        return self.end_amount - self.spent

    @property
    def profit_bps(self) -> float:
        """Cash-only edge: dust counted as worthless."""
        return float(self.profit / self.spent * 10000) if self.spent > 0 else 0.0

    @property
    def value(self) -> float:
        return float(self.profit) + self.dust_value

    @property
    def value_bps(self) -> float:
        """Edge with dust at its marked value: comparable to realised mark-to-market."""
        return self.value / float(self.spent) * 1e4 if self.spent > 0 else 0.0


def leg_limit_price(quote: Quote, side: str, slippage_bps: float, rules: SymbolRules) -> Decimal:
    if side == BUY:
        raw = D(quote.ask) * (1 + D(slippage_bps) / 10000)
    else:
        raw = D(quote.bid) * (1 - D(slippage_bps) / 10000)
    return rules.round_price(raw, side)


def plan_leg(
    rules: SymbolRules, side: str, amount_in: Decimal, price: Decimal, fee: Decimal
) -> tuple[LegPlan | None, str | None]:
    """Turn `amount_in` of the leg's input asset into a valid limit order."""
    if side == BUY:
        qty = rules.floor_qty(amount_in / price)
        spent = qty * price
        out = qty * (1 - fee)
    else:
        qty = rules.floor_qty(amount_in)
        spent = qty
        out = qty * price * (1 - fee)
    reason = rules.check(qty, price)
    if reason:
        return None, f"{rules.symbol}: {reason}"
    return LegPlan(rules.symbol, side, price, qty, spent, out, amount_in - spent), None


def simulate_cycle(
    tri: Triangle,
    start_amount: Decimal,
    books: BookStore,
    rules: dict[str, SymbolRules],
    fees: FeeModel,
    slippage_bps: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> CyclePlan:
    plan = CyclePlan(tri, start_amount)
    amount = start_amount
    mults: list[float] = []          # start-asset value of one unit of each leg's input
    for leg, slip in zip(tri.legs, slippage_bps):
        q = books.get(leg.symbol)
        if q is None:
            plan.reject = f"{leg.symbol}: no live quote"
            return plan
        r = rules[leg.symbol]
        price = leg_limit_price(q, leg.side, slip, r)
        lp, reason = plan_leg(r, leg.side, amount, price, D(fees.rate(leg.symbol)))
        if lp is None:
            plan.reject = reason
            return plan
        plan.legs.append(lp)
        amount = lp.amount_out
        fm = 1 - fees.rate(leg.symbol)
        mults.append(fm / q.ask if leg.is_buy else q.bid * fm)
    plan.end_amount = amount
    # value of dust on leg i = dust * product of remaining legs' conversion rates
    for i in (1, 2):
        rest = 1.0
        for m in mults[i:]:
            rest *= m
        plan.dust_value += float(plan.legs[i].dust) * rest
    return plan


def optimize_size(
    tri: Triangle,
    low: Decimal,
    high: Decimal,
    books: BookStore,
    rules: dict[str, SymbolRules],
    fees: FeeModel,
    steps: int = 12,
    count_dust: bool = True,
) -> CyclePlan:
    """Pick the start amount with the largest absolute profit.

    Step-size rounding leaves dust on every leg, and how much depends on the
    exact amount: on a real book the same cycle can be +18 bps at one size and
    negative at a slightly smaller one. A small grid search finds the size
    where the rounding works out.
    """
    best: CyclePlan | None = None
    if high <= low:
        return simulate_cycle(tri, high, books, rules, fees)
    span = high - low
    for i in range(steps + 1):
        amount = high - span * i / steps
        plan = simulate_cycle(tri, amount, books, rules, fees)
        if plan.reject is not None:
            if best is None:
                best = plan
            continue
        score = plan.value if count_dust else float(plan.profit)
        if best is None or best.reject is not None or score > (best.value if count_dust else float(best.profit)):
            best = plan
    assert best is not None
    return best
