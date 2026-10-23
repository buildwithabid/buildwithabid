"""The autonomous loop: perceive -> detect -> decide -> act -> learn.

  perceive  BookStream keeps the best bid/ask of every relevant market live.
  detect    Scanner re-prices only the triangles touched by each update.
  decide    Exact simulation (rounding, minimums, fees), then the brain's
            expected-value test, then the risk manager's circuit breakers.
  act       CycleExecutor sends the orders (paper or live, same code).
  learn     Each outcome updates the brain; everything goes to the journal.
"""
from __future__ import annotations

import asyncio
import logging
import random
import signal
from collections import Counter
from pathlib import Path
from typing import Any

from .book import BookStore, Valuer
from .brain import BROKEN, COMPLETE, MISS, Brain, Decision
from .clock import RealClock
from .config import Config, api_credentials
from .exchange import BinanceREST
from .executor import CycleExecutor, CycleResult, Gateway, LiveGateway, PaperGateway
from .graph import find_triangles
from .journal import Journal
from .market import D, SymbolRules, parse_exchange_info
from .pricing import CompiledTriangle, CyclePlan, FeeModel, optimize_size
from .risk import RiskManager
from .scanner import Opportunity, Scanner
from .stream import BookStream

log = logging.getLogger("arbagent")


class UsdtRates(dict):  # type: ignore[type-arg]
    """Lazy asset -> USDT price lookup built from 24h ticker last prices."""

    def __init__(self, tickers: list[dict[str, Any]]) -> None:
        super().__init__(USDT=1.0)
        self.last = {t["symbol"]: float(t.get("lastPrice") or 0) for t in tickers}

    def __missing__(self, asset: str) -> float | None:
        last, btc = self.last, self.last.get("BTCUSDT", 0.0)
        v: float | None = None
        if last.get(asset + "USDT"):
            v = last[asset + "USDT"]
        elif last.get("USDT" + asset):
            v = 1.0 / last["USDT" + asset]
        elif btc and last.get(asset + "BTC"):
            v = last[asset + "BTC"] * btc
        elif btc and last.get("BTC" + asset):
            v = btc / last["BTC" + asset]
        self[asset] = v
        return v


def filter_universe(rules: dict[str, SymbolRules], tickers: list[dict[str, Any]],
                    min_volume_usdt: float) -> dict[str, SymbolRules]:
    if min_volume_usdt <= 0 or not tickers:
        return dict(rules)
    rates = UsdtRates(tickers)
    keep: dict[str, SymbolRules] = {}
    for t in tickers:
        r = rules.get(t["symbol"])
        if r is None:
            continue
        rate = rates[r.quote]
        if rate and float(t.get("quoteVolume") or 0) * rate >= min_volume_usdt:
            keep[r.symbol] = r
    return keep


class Agent:
    def __init__(self, cfg: Config, mode: str, clock: Any = None, persist: bool = True):
        assert mode in ("scan", "paper", "live", "replay")
        self.cfg = cfg
        self.mode = mode
        self.clock = clock or RealClock()
        self.persist = persist
        if cfg.brain.policy == "auto":
            cfg.brain.policy = "lcb" if mode == "live" else "thompson"
        s = cfg.strategy
        self.fees = FeeModel(s.taker_fee, s.bnb_fee_discount, s.fee_overrides)
        self.brain = Brain(cfg.brain, rng=random.Random(cfg.paper.seed))
        self.risk = RiskManager(cfg.risk, self.clock,
                                Path(cfg.general.kill_switch_file) if cfg.general.kill_switch_file else None)
        self.journal: Journal | None = None
        self.gateway: Gateway | None = None
        self.executor: CycleExecutor | None = None
        self.stream: BookStream | None = None
        self.public: BinanceREST | None = None
        self.private: BinanceREST | None = None
        self.task: asyncio.Task[None] | None = None
        self.skips: Counter[str] = Counter()
        self.outcomes: Counter[str] = Counter()
        self.pnl: Counter[str] = Counter()
        self.results: list[CycleResult] = []
        self._stop = asyncio.Event()
        self._last_msgs = 0
        self._last_evals = 0
        self.exchange_info: dict[str, Any] = {}
        # Leftovers the agent itself created, keyed by (asset, start asset). Only
        # these are ever swept, never other holdings in the account.
        self.inventory: Counter[tuple[str, str]] = Counter()
        self.on_raw: Any = None          # optional hook for every raw stream message (recording)
        self._built = False

    # -- setup -------------------------------------------------------------------
    @property
    def brain_path(self) -> Path:
        return self.cfg.data_path / f"brain-{self.mode}.json"

    def build(self, all_rules: dict[str, SymbolRules], tickers: list[dict[str, Any]] | None = None) -> None:
        u = self.cfg.universe
        self.all_rules = all_rules
        traded = filter_universe(all_rules, tickers or [], u.min_24h_quote_volume_usdt)
        tris = find_triangles(traded, self.cfg.general.start_assets, u.exclude_assets, u.include_only_assets)
        self.triangles = [CompiledTriangle(t, self.fees) for t in tris]
        symbols = sorted({s for ct in self.triangles for s in ct.symbols})
        # Also watch the USDT price of each start asset so P&L can be reported in USDT.
        for a in self.cfg.general.start_assets:
            for sym in (f"{a}USDT", f"USDT{a}"):
                if sym in all_rules and sym not in symbols:
                    symbols.append(sym)
        self.rules = {s: all_rules[s] for s in symbols}
        self.symbols = symbols
        self.books = BookStore(symbols)
        self.valuer = Valuer(all_rules, self.books)
        self.scanner = Scanner(self.triangles, self.books, self.cfg.strategy.min_edge_bps, self.clock)
        if self.persist and self.mode in ("paper", "live") and self.brain.load(self.brain_path):
            log.info("loaded brain state from %s: %s", self.brain_path, self.brain.summary())
        if self.mode in ("paper", "replay"):
            self.gateway = PaperGateway(self.books, self.rules, self.fees, self.clock, self.cfg.paper)
        if self.gateway is not None:
            self.executor = CycleExecutor(self.gateway, self.books, self.rules, self.fees, self.valuer,
                                          self.clock, self.cfg.strategy.leg_slippage_bps)
        if self.persist:
            self.journal = Journal(self.cfg.data_path / "journal.sqlite", self.mode)
        log.info("universe: %d tradable symbols, %d triangles from %s, watching %d markets",
                 len(traded), len(self.triangles), "/".join(self.cfg.general.start_assets), len(symbols))
        if not self.triangles:
            raise RuntimeError("no triangles found: check start_assets and universe filters")
        self._built = True

    async def _connect_live(self) -> None:
        key, secret = api_credentials()
        if not (key and secret):
            raise RuntimeError("live mode needs BINANCE_API_KEY and BINANCE_API_SECRET in the environment")
        ex = self.cfg.exchange
        self.private = BinanceREST(ex.rest_url, key, secret, ex.recv_window_ms, ex.request_timeout_s)
        rtt = await self.private.sync_time()
        self.brain.record_rtt(rtt)
        log.info("time synced: offset %.0f ms, round trip %.0f ms", self.private.time_offset_ms, rtt)
        if ex.require_no_withdraw_permission:
            restr = await self.private.api_restrictions()
            if restr.get("enableWithdrawals"):
                raise RuntimeError("this API key can withdraw funds. Create a trade-only key "
                                   "(withdrawals disabled, ideally IP-restricted) and try again")
            if not restr.get("enableSpotAndMarginTrading", True):
                raise RuntimeError("this API key does not have spot trading enabled")
        acct = await self.private.account()
        if not acct.get("canTrade", False):
            raise RuntimeError("account cannot trade")
        taker = float(acct.get("commissionRates", {}).get("taker", self.cfg.strategy.taker_fee))
        if self.cfg.strategy.bnb_fee_discount:
            taker *= 0.75
        self.fees.set_default(taker)
        log.info("account taker fee %.4f%%%s", taker * 100, " (BNB discount applied)"
                 if self.cfg.strategy.bnb_fee_discount else "")
        self.gateway = LiveGateway(self.private, on_rtt=self.brain.record_rtt)
        await self.gateway.refresh()

    async def start(self) -> None:
        ex = self.cfg.exchange
        public_url = ex.rest_url if self.mode == "live" else ex.public_rest_url
        self.public = BinanceREST(public_url, timeout_s=30)
        if self.mode == "live":
            await self._connect_live()
        info = self.exchange_info = await self.public.exchange_info()
        tickers = await self.public.ticker_24hr() if self.cfg.universe.min_24h_quote_volume_usdt > 0 else []
        self.build(parse_exchange_info(info), tickers)
        if self.mode == "live":
            assert self.gateway is not None
            for a in self.cfg.general.start_assets:
                log.info("balance %s: %s", a, self.gateway.free(a))
        # Quiet markets only stream on change, so seed every book from a snapshot.
        # update_id 0 means any streamed update replaces the seed.
        now = self.clock.now()
        for t in await self.public.book_tickers():
            if t["symbol"] in self.books.quotes:
                self.books.update(t["symbol"], 0, float(t["bidPrice"]), float(t["bidQty"]),
                                  float(t["askPrice"]), float(t["askQty"]), now)
        self.stream = BookStream(ex.stream_url, self.symbols, self.books, self.clock, self.on_update,
                                 self.cfg.universe.max_streams_per_connection, on_raw=self.on_raw)
        self._event("INFO", "start", f"mode={self.mode} triangles={len(self.triangles)}")

    # -- hot path ----------------------------------------------------------------
    def on_update(self, symbol: str) -> None:
        opps = self.scanner.on_update(symbol)
        if not opps or self.executor is None:
            return
        if self.task is not None and not self.task.done():
            return
        ok, why = self.risk.allow()
        if not ok:
            self.skips[why.split(":")[0]] += 1
            return
        opps.sort(key=lambda o: o.ev.edge_bps, reverse=True)
        for opp in opps[:3]:
            plan, decision = self.evaluate(opp)
            if plan is not None and decision is not None and decision.execute:
                self.task = asyncio.create_task(self._execute(opp, plan, decision))
                self.task.add_done_callback(self._task_done)
                return

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("cycle task crashed: %r", exc, exc_info=exc)
            self.risk.halt(f"internal error: {exc!r}")

    def evaluate(self, opp: Opportunity) -> tuple[CyclePlan | None, Decision | None]:
        tri = opp.ct.tri
        start = tri.start
        s = self.cfg.strategy
        size = min(D(s.max_trade[start]), D(round(opp.ev.capacity * s.capacity_fraction, 12)))
        if self.gateway is not None:
            size = min(size, self.gateway.free(start))
        low = D(s.min_trade.get(start, 0))
        if size < low:
            self.skips["size below min_trade"] += 1
            return None, None
        plan = optimize_size(tri, low, size, self.books, self.rules, self.fees, count_dust=s.count_dust)
        if plan.reject:
            self.skips["exchange filters"] += 1
            return None, None
        profit, bps = (plan.value, plan.value_bps) if s.count_dust else (float(plan.profit), plan.profit_bps)
        if profit < s.min_profit.get(start, 0) or bps < s.min_edge_bps:
            self.skips["unprofitable after rounding"] += 1
            return None, None
        leg1 = opp.ev.quotes[0]
        prior = leg1.spread_bps + 2 * self.fees.rate(tri.legs[0].symbol) * 1e4 + self.cfg.brain.prior_adverse_move_bps
        decision = self.brain.decide(tri.id, bps, prior, self.clock.now())
        if not decision.execute:
            self.skips[decision.reason.split(":")[0]] += 1
        return plan, decision

    async def _execute(self, opp: Opportunity, plan: CyclePlan, decision: Decision) -> None:
        assert self.executor is not None and self.gateway is not None
        self.risk.on_attempt()
        res = await self.executor.execute(plan)
        start = res.start_asset
        mtm = res.pnl + res.dust_value + res.stuck_value
        self.brain.record(res.triangle, res.outcome, res.expected_bps, res.mtm_bps, mtm, self.clock.now())
        to_usdt = self.valuer.rate(start, "USDT") or 0.0
        self.risk.on_cycle(res.outcome, mtm * to_usdt, float(res.spent) * 3 * to_usdt, res.stuck_value * to_usdt)
        if res.error:
            self.risk.halt(f"execution error: {res.error}")
        self.outcomes[res.outcome] += 1
        self.pnl[start] += mtm
        for bucket in (res.dust, res.stuck):
            for asset, amount in bucket.items():
                if asset != start and amount > 0:
                    self.inventory[(asset, start)] += float(amount)
        self.results.append(res)
        if self.journal:
            self.journal.cycle(res, decision.as_dict())
            if self.risk.halted:
                self._event("ERROR", "halt", self.risk.halted)
        if res.outcome != MISS:
            log.info("%s %s spent=%s expected=%.2fbps realized=%.2fbps pnl=%+.6f %s%s",
                     res.outcome.upper(), res.triangle, res.spent, res.expected_bps, res.mtm_bps, mtm, start,
                     f" notes={res.notes}" if res.notes else "")
        if res.outcome in (COMPLETE, BROKEN) and self.mode == "live":
            try:
                await self.gateway.refresh()
            except Exception as e:  # noqa: BLE001
                log.warning("balance refresh failed: %r", e)

    async def sweep(self) -> None:
        """Convert accumulated leftovers back to their start asset once they
        clear the exchange minimums. P&L already counted them at mid price, so
        only the difference between mid and the actual fill is booked."""
        if self.executor is None or self.gateway is None or not self.inventory:
            return
        if (self.task is not None and not self.task.done()) or not self.risk.allow()[0]:
            return
        for (asset, start), amount in list(self.inventory.items()):
            amt = min(D(round(amount, 12)), self.gateway.free(asset))
            if amt <= 0:
                del self.inventory[(asset, start)]
                continue
            res = await self.executor.convert(asset, amt, start)
            if res.spent <= 0:
                continue
            at_mid = self.valuer.value(asset, float(res.spent), start) or 0.0
            adjustment = float(res.end_amount) - at_mid
            self.pnl[start] += adjustment
            self.inventory[(asset, start)] -= float(res.spent)
            if self.inventory[(asset, start)] <= 0:
                del self.inventory[(asset, start)]
            msg = f"swept {res.spent} {asset} -> {res.end_amount} {start} (vs mid {adjustment:+.8f})"
            log.info(msg)
            self._event("INFO", "sweep", msg)

    # -- supervision -------------------------------------------------------------
    def _event(self, level: str, kind: str, msg: str) -> None:
        if self.journal:
            self.journal.event(level, kind, msg, self.clock.now())

    def capturable_fraction(self) -> float | None:
        """Share of opportunities that lived longer than half a round trip, the
        least time an order needs to reach the matching engine."""
        lifetimes = self.scanner.stats.lifetimes_ms
        if not lifetimes:
            return None
        rtt = self.brain.rtt_ms or self.cfg.paper.rtt_ms
        return sum(1 for x in lifetimes if x > rtt / 2) / len(lifetimes)

    def status_line(self, interval: float) -> str:
        st = self.scanner.stats
        msgs = self.stream.messages if self.stream else self.books.updates
        rate = (msgs - self._last_msgs) / interval
        evals = (st.evaluations - self._last_evals) / interval
        self._last_msgs, self._last_evals = msgs, st.evaluations
        live = sum(1 for q in self.books.quotes.values() if q.valid)
        lq = st.lifetime_quantiles()
        parts = [
            f"[{self.mode}] books {live}/{len(self.books.quotes)}",
            f"{rate:.0f} upd/s {evals / 1000:.1f}k evals/s",
            f"opportunities {st.episodes} (active {len(self.scanner.active)}"
            + (f", life p50 {lq['p50']:.0f}ms p90 {lq['p90']:.0f}ms" if lq else "") + ")",
        ]
        if st.best_triangle:
            parts.append(f"best net {st.best_edge_bps:+.1f}bps (gross {st.best_gross_bps:+.1f}) {st.best_triangle}")
        if self.mode != "scan":
            o = self.outcomes
            parts.append(f"cycles {o[COMPLETE]} ok/{o[BROKEN]} broken/{o[MISS]} miss")
            parts.append("pnl " + ", ".join(f"{v:+.6f} {k}" for k, v in self.pnl.items()) if self.pnl else "pnl 0")
            ok, why = self.risk.allow()
            parts.append("risk ok" if ok else f"risk BLOCKED: {why}")
        st.reset_window()
        return " | ".join(parts)

    async def run(self, duration: float | None = None) -> None:
        try:
            await self.start()
        except BaseException:
            await self.shutdown()
            raise
        assert self.stream is not None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        stream_task = asyncio.create_task(self.stream.run())
        interval = self.cfg.general.status_interval_s
        deadline = None if duration is None else self.clock.now() + duration
        try:
            while not self._stop.is_set():
                timeout = interval if deadline is None else max(0.0, min(interval, deadline - self.clock.now()))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
                self.supervise()
                log.info(self.status_line(interval))
                if self.mode in ("paper", "live"):
                    await self.sweep()
                if deadline is not None and self.clock.now() >= deadline:
                    break
                if self.risk.halted and self.mode == "live":
                    log.error("HALTED: %s. Fix the cause, then restart.", self.risk.halted)
                    break
        finally:
            await self.shutdown(stream_task)

    def supervise(self) -> None:
        if not self._built:
            return               # never overwrite saved brain state with an empty one
        if self.stream is not None:
            healthy = self.stream.healthy(self.cfg.risk.stream_stale_s)
            if healthy != self.risk.stream_healthy:
                self._event("WARN" if not healthy else "INFO", "stream", "healthy" if healthy else "unhealthy")
            self.risk.stream_healthy = healthy
        if self.journal:
            self.journal.episodes(self.scanner.drain_closed())
        if self.persist and self.mode in ("paper", "live"):
            self.brain.save(self.brain_path)

    async def shutdown(self, stream_task: asyncio.Task[None] | None = None) -> None:
        if self.task is not None and not self.task.done():
            log.info("waiting for the in-flight cycle to finish before exiting")
            await asyncio.gather(asyncio.shield(self.task), return_exceptions=True)
        if self.stream is not None:
            self.stream.stop()
        if stream_task is not None:
            await asyncio.gather(stream_task, return_exceptions=True)
        self.supervise()
        for c in (self.public, self.private):
            if c is not None:
                await c.close()
        if self.journal:
            self._event("INFO", "stop", f"outcomes={dict(self.outcomes)} pnl={dict(self.pnl)}")
            self.journal.close()
            self.journal = None
