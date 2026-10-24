"""SQLite journal: every cycle attempt, every opportunity episode, every event."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    triangle TEXT NOT NULL,
    start_asset TEXT NOT NULL,
    outcome TEXT NOT NULL,
    spent REAL NOT NULL,
    expected_bps REAL NOT NULL,
    realized_bps REAL NOT NULL,
    pnl REAL NOT NULL,
    dust_value REAL NOT NULL,
    stuck_value REAL NOT NULL,
    latency_ms REAL NOT NULL,
    decision TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    triangle TEXT NOT NULL,
    lifetime_ms REAL NOT NULL,
    max_edge_bps REAL NOT NULL,
    max_capacity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cycles_mode_ts ON cycles(mode, ts);
CREATE INDEX IF NOT EXISTS episodes_mode_ts ON episodes(mode, ts);
"""


class Journal:
    def __init__(self, path: Path, mode: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def cycle(self, result: Any, decision: dict[str, Any] | None) -> None:
        d = result.as_dict()
        self.db.execute(
            "INSERT INTO cycles (ts, mode, triangle, start_asset, outcome, spent, expected_bps, realized_bps,"
            " pnl, dust_value, stuck_value, latency_ms, decision, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (result.started, self.mode, result.triangle, result.start_asset, result.outcome, float(result.spent),
             result.expected_bps, result.realized_bps, result.pnl, result.dust_value, result.stuck_value,
             d["latency_ms"], json.dumps(decision), json.dumps(d)),
        )
        self.db.commit()

    def episodes(self, episodes: Iterable[Any]) -> None:
        rows = [(e.start, self.mode, e.triangle, e.lifetime_ms, e.max_edge_bps, e.max_capacity) for e in episodes]
        if rows:
            self.db.executemany(
                "INSERT INTO episodes (ts, mode, triangle, lifetime_ms, max_edge_bps, max_capacity)"
                " VALUES (?,?,?,?,?,?)", rows)
            self.db.commit()

    def event(self, level: str, kind: str, message: str, ts: float | None = None) -> None:
        self.db.execute("INSERT INTO events (ts, mode, level, kind, message) VALUES (?,?,?,?,?)",
                        (ts or time.time(), self.mode, level, kind, message))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- reporting -------------------------------------------------------------
    def report(self, mode: str | None = None) -> dict[str, Any]:
        mode = mode or self.mode
        q = self.db.execute
        out: dict[str, Any] = {"mode": mode}
        out["outcomes"] = dict(q("SELECT outcome, COUNT(*) FROM cycles WHERE mode=? GROUP BY outcome",
                                 (mode,)).fetchall())
        out["pnl_by_asset"] = {
            a: {"cycles": n, "pnl": round(p, 8), "dust_value": round(dv, 8), "stuck_value": round(sv, 8),
                "spent": round(s, 4), "avg_expected_bps": round(e, 3), "avg_realized_bps": round(r, 3)}
            for a, n, p, dv, sv, s, e, r in q(
                "SELECT start_asset, COUNT(*), SUM(pnl), SUM(dust_value), SUM(stuck_value), SUM(spent),"
                " AVG(expected_bps), AVG(realized_bps) FROM cycles WHERE mode=? AND outcome!='miss'"
                " GROUP BY start_asset", (mode,)).fetchall()
        }
        out["top_triangles"] = [
            {"triangle": t, "attempts": n, "pnl": round(p, 8)}
            for t, n, p in q("SELECT triangle, COUNT(*), SUM(pnl) FROM cycles WHERE mode=? GROUP BY triangle"
                             " ORDER BY COUNT(*) DESC LIMIT 10", (mode,)).fetchall()
        ]
        n, avg_life, max_edge = q("SELECT COUNT(*), AVG(lifetime_ms), MAX(max_edge_bps) FROM episodes"
                                  " WHERE mode=?", (mode,)).fetchone()
        lifetimes = [r[0] for r in q("SELECT lifetime_ms FROM episodes WHERE mode=? ORDER BY lifetime_ms",
                                     (mode,)).fetchall()]
        out["opportunities"] = {
            "episodes": n,
            "avg_lifetime_ms": None if avg_life is None else round(avg_life, 1),
            "p50_lifetime_ms": lifetimes[len(lifetimes) // 2] if lifetimes else None,
            "max_edge_bps": None if max_edge is None else round(max_edge, 2),
        }
        out["recent_events"] = [
            {"ts": ts, "level": lv, "kind": k, "message": m}
            for ts, lv, k, m in q("SELECT ts, level, kind, message FROM events WHERE mode=? ORDER BY ts DESC"
                                  " LIMIT 10", (mode,)).fetchall()
        ]
        return out
