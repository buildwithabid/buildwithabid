import pytest

from arbagent.brain import BROKEN, COMPLETE, MISS, Brain
from arbagent.config import BrainConfig, RiskConfig
from arbagent.pricing import CompiledTriangle
from arbagent.risk import RiskManager
from arbagent.scanner import Scanner

from conftest import set_books


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def now(self) -> float:
        return self.t


# -- brain ---------------------------------------------------------------------

def brain(**kw) -> Brain:
    return Brain(BrainConfig(**({"policy": "mean", "safety_margin_bps": 0.0, "min_ev_bps": 0.0} | kw)))


def test_ev_formula():
    b = brain(prior_complete_prob=0.8)
    d = b.decide("T", edge_bps=10.0, book_prior_bps=20.0, now=0)
    assert d.p_complete == pytest.approx(0.8)
    assert d.ev_bps == pytest.approx(0.8 * 10 - 0.2 * 20)
    assert d.execute


def test_small_edge_rejected_when_failures_are_costly():
    d = brain(prior_complete_prob=0.5).decide("T", edge_bps=5.0, book_prior_bps=30.0, now=0)
    assert not d.execute and d.ev_bps < 0


def test_lcb_is_more_cautious_than_mean():
    mean = brain(prior_complete_prob=0.7).p_complete("T")
    lcb = brain(prior_complete_prob=0.7, policy="lcb").p_complete("T")
    assert lcb < mean


def test_learning_moves_the_estimate_and_shrinks_to_pool():
    b = brain(prior_complete_prob=0.5)
    for i in range(20):
        b.record("BAD", BROKEN, 10, -25, -0.01, now=i)
    assert b.p_complete("BAD") < 0.15
    assert b.broken_cost_bps("BAD", 10.0) > 20       # learned the real cost, not the 10 bps prior
    # An unseen triangle borrows the pooled (now pessimistic) estimate...
    assert b.p_complete("NEW") < 0.5
    # ...but less extreme than the triangle with direct evidence.
    assert b.p_complete("NEW") > b.p_complete("BAD")


def test_successes_raise_probability_and_track_slippage():
    b = brain()
    for i in range(30):
        b.record("GOOD", COMPLETE, 10, 7, 0.001, now=i)
    assert b.p_complete("GOOD") > 0.85
    assert b.slip_bps("GOOD") == pytest.approx(-3, abs=0.5)


def test_misses_do_not_count_as_failures_but_flag_phantoms():
    b = brain(prior_complete_prob=0.9, triangle_cooldown_s=0)
    p0 = b.p_complete("P")
    for i in range(10):
        b.record("P", MISS, 10, 0, 0, now=i)
    assert b.p_complete("P") == pytest.approx(p0)
    d = b.decide("P", 50, 10, now=100)
    assert not d.execute and d.reason.startswith("phantom")


def test_cooldown_after_failure():
    b = brain(prior_complete_prob=0.9, triangle_cooldown_s=5)
    b.record("T", BROKEN, 10, -20, -0.01, now=100)
    assert b.decide("T", 50, 10, now=103).reason == "cooldown"
    assert b.decide("T", 50, 10, now=106).reason != "cooldown"


def test_forgetting_lets_the_model_recover():
    b = brain(forgetting=0.9)
    for i in range(30):
        b.record("T", BROKEN, 10, -20, 0, now=i)
    low = b.p_complete("T")
    for i in range(30):
        b.record("T", COMPLETE, 10, 10, 0, now=100 + i)
    assert b.p_complete("T") > 0.8 > low


def test_save_load_roundtrip(tmp_path):
    b = brain()
    b.record("T", COMPLETE, 10, 9, 0.5, now=1)
    b.record_rtt(42)
    path = tmp_path / "brain.json"
    b.save(path)
    c = brain()
    assert c.load(path)
    assert c.tri["T"].pnl == pytest.approx(0.5)
    assert c.rtt_ms == 42
    assert c.p_complete("T") == pytest.approx(b.p_complete("T"))


# -- risk ----------------------------------------------------------------------

def risk(tmp_path, **kw):
    clock = FakeClock()
    return RiskManager(RiskConfig(**kw), clock, tmp_path / "STOP"), clock


def test_kill_switch_file(tmp_path):
    r, _ = risk(tmp_path)
    assert r.allow()[0]
    (tmp_path / "STOP").touch()
    r._kill_checked = -1e9
    ok, why = r.allow()
    assert not ok and "kill switch" in why


def test_consecutive_failures_pause_then_resume(tmp_path):
    r, clock = risk(tmp_path, max_consecutive_failures=2, failure_pause_s=60)
    r.on_cycle(BROKEN, -0.1, 30, 0)
    assert r.allow()[0]
    r.on_cycle(BROKEN, -0.1, 30, 0)
    assert not r.allow()[0]
    clock.t += 61
    assert r.allow()[0]


def test_misses_do_not_trip_breakers(tmp_path):
    r, _ = risk(tmp_path, max_consecutive_failures=1)
    for _ in range(5):
        r.on_cycle(MISS, 0, 0, 0)
    assert r.allow()[0]


def test_daily_loss_limit_resets_next_utc_day(tmp_path):
    r, clock = risk(tmp_path, daily_loss_limit_usdt=1.0, max_consecutive_failures=99)
    r.on_cycle(BROKEN, -1.5, 30, 0)
    ok, why = r.allow()
    assert not ok and "daily loss" in why
    clock.t += 86400
    assert r.allow()[0]


def test_stuck_inventory_is_a_hard_halt(tmp_path):
    r, clock = risk(tmp_path, max_stuck_value_usdt=5)
    r.on_cycle(BROKEN, -0.1, 30, 6)
    clock.t += 86400 * 2
    ok, why = r.allow()
    assert not ok and "stuck inventory" in why


def test_cycle_rate_cap(tmp_path):
    r, clock = risk(tmp_path, max_cycles_per_minute=2)
    r.on_attempt()
    r.on_attempt()
    assert not r.allow()[0]
    clock.t += 61
    assert r.allow()[0]


# -- scanner -------------------------------------------------------------------

def test_scanner_detects_and_times_episodes(triangles, books, fees):
    clock = FakeClock(100.0)
    cts = [CompiledTriangle(t, fees) for t in triangles]
    sc = Scanner(cts, books, min_edge_bps=1.0, clock=clock)
    opps = sc.on_update("ETHUSDT")
    assert [o.ct.id for o in opps] == ["USDT>BTC>ETH>USDT"]
    clock.t = 100.25
    set_books(books, ETHUSDT=(3000.0, 50.0, 3000.01, 50.0))
    assert sc.on_update("ETHUSDT") == []
    (ep,) = sc.drain_closed()
    assert ep.lifetime_ms == pytest.approx(250)
    assert sc.stats.episodes == 1


def test_scanner_only_reprices_affected_triangles(triangles, books, fees):
    sc = Scanner([CompiledTriangle(t, fees) for t in triangles], books, 1.0, FakeClock())
    sc.on_update("UNRELATED")
    assert sc.stats.evaluations == 0
    sc.on_update("ETHBTC")
    assert sc.stats.evaluations == 2
