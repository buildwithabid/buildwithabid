# arbagent: an autonomous triangular-arbitrage agent for Binance spot

An agent that watches every Binance spot market and finds three-trade loops that end with more than they started (for example USDT → BTC → ETH → USDT). It decides whether a loop is worth trading, places the orders, recovers when a leg fails, and learns from each result. Paper trading is the default. Live trading needs API keys and an explicit flag.

> **Read this first: it will very likely not make you money.** I measured live Binance data while building this (results below). Edges that beat retail fees appear a few times a minute and last **1 to 3 milliseconds**. A retail order takes **100 ms or more** just to reach Binance. The code is built to be correct and safe, and to tell you honestly what it sees. It is not a money printer, and nothing here is financial advice.

---

## What I measured (23 Oct 2026, live Binance data)

| Test | Result |
|---|---|
| REST snapshot, all 1,658 triangles that start in USDT | Best loop was **+0.14% before fees**. Three taker fees cost 0.30%, so every loop lost money. |
| 75 s live scan, full market (1,179 pairs, ~3,000 book updates/s) | 2 loops beat 0.1% fees, at **+8.8 bps**. They lived **0 ms and 3.3 ms**. |
| 5 min live recording (807,554 updates), replayed | 49 loops beat fees. Median life **0.1 ms**, longest **3.9 ms**. **0%** lived long enough to reach with a 120 ms round trip. The agent tried 2: one missed, one broke. |
| 4 min paper trading, real fees, default settings | 40 opportunities (median life 1.4 ms). The one attempt broke at **−70 bps** (−0.35 USDT): leg 1 filled, the rest of the loop was gone 120 ms later, and the agent unwound. The brain learned that cost and rejected the next 114 candidates. Replaying the recording reproduced this same trade to the sixth decimal. |
| 90 s paper trading with fees set to **zero** | Even with no fees: 3 cycles completed, 10 broke, net loss. Edges vanish within one round trip. |

What it would take to compete (none of this is something software alone can give you):

- **Fees.** VIP tiers or the BNB discount (0.075% × 3 = 0.225% per loop). Most visible edges are smaller than that.
- **Latency.** Binance's matching engine runs in AWS Tokyo (ap-northeast-1). A server there gets a round trip of about 1–5 ms; a home connection in Europe or the US gets 150–300 ms. Run `arbagent ping` to measure yours.
- **Order entry.** Binance's WebSocket API or FIX/SBE instead of REST, and inventory pre-positioned in all three assets, so the three legs can fire at once rather than one after another.

Professional firms do all of this and pay close to zero maker fees. They are who you would be racing.

---

## How the agent works

```
perceive ──► detect ──► decide ──────────────────────► act ──────► learn
BookStream   Scanner    exact simulation               Cycle       Brain updates
(WebSocket   re-prices  → size optimiser               Executor    p(complete),
 best bid/   only the   → brain: expected value        (paper or   broken-leg cost,
 ask for     triangles  → risk: circuit breakers       live, same  slippage. Journal
 every pair) touched                                    code)       records it all.
```

| Module | Job |
|---|---|
| `stream.py` | Combined `@bookTicker` streams (≤1,024 per connection), auto-reconnect, books invalidated while disconnected, seeded from a REST snapshot at start |
| `graph.py` | Finds every A→B→C→A cycle for your start assets |
| `scanner.py` | On each update re-prices only the triangles that use that symbol (~30–40k evaluations/s in pure Python). Measures how long each opportunity lives |
| `pricing.py` | Fast float check, then an **exact Decimal simulation** of the orders with Binance tick/step rounding, minimum notional and fees. A **size optimiser** picks the amount where rounding leaves the least behind (the same loop can be +18 bps at one size and negative at a slightly smaller one) |
| `brain.py` | Decides whether a trade's expected value is positive (below) |
| `risk.py` | Kill-switch file, daily loss and volume limits, pause after losing streaks, cycle-rate cap, hard halt on stuck inventory or unknown order state |
| `executor.py` | Places the three orders and recovers when a leg fails (below). `LiveGateway` for Binance, `PaperGateway` simulates fills with network latency |
| `exchange.py` | Signed REST client: HMAC-SHA256, server-time sync, rate-limit headers, honours `Retry-After` on 429/418 |
| `journal.py` | SQLite log of every attempt, fill, opportunity and event |
| `replay.py` | Record live data, replay it through the same agent on a virtual clock (~13× real time for the full market) |

### The brain: when is a visible edge worth trading?

Every attempt ends one of three ways:

- **miss**: leg 1 doesn't fill (someone was faster). Costs nothing.
- **complete**: all three legs fill. Earns about the edge.
- **broken**: leg 1 fills but a later leg doesn't. The agent finishes at market or unwinds, paying fees, spread and price drift.

The brain trades only when

```
EV = p × (edge + slippage) − (1 − p) × broken_cost − safety_margin  >  min_ev
```

- **p** is the chance that a filled leg 1 leads to a complete cycle. It's estimated per triangle as a Beta posterior, shrunk toward the all-triangle average when a triangle has little history (empirical Bayes). Old observations decay so the model follows the market.
- **broken_cost** starts from the live book (leg-1 spread + two fees + expected drift) and is replaced by what broken cycles actually cost.
- **slippage** is learned from completed cycles.
- **Policy.** In paper mode the agent explores with Thompson sampling, so it gathers data. Live, it uses a lower confidence bound on p, so it acts only on evidence. A triangle whose leg 1 almost never fills is flagged as a phantom and skipped. Every failure triggers a per-triangle cooldown.

Its state is saved to `data/brain-<mode>.json`. To start live trading with what paper trading learned, copy `brain-paper.json` to `brain-live.json`. Be aware that paper fills are optimistic (see Limitations).

### Execution and recovery

1. **Leg 1**: LIMIT IOC at the price the opportunity was seen at. If it's gone, the order expires and nothing is lost.
2. **Before leg 2**: re-price the rest of the cycle on the current book, compare it with unwinding leg 1, and take whichever returns more.
3. **Legs 2–3**: LIMIT IOC up to `leg_slippage_bps` through the touch. Any unfilled remainder is finished with a MARKET order, so the agent never sits on an unintended position.
4. **Unknown outcome** (timeout or HTTP 5xx, which Binance says means the status is *unknown*): look the order up by client order id. If that fails, **halt**.
5. **Leftovers**: rounding dust is tracked, and the agent sweeps back only the amounts *it* created. It never touches your other holdings.

The same executor runs in paper and live mode, so a paper run tests the real decision and recovery code.

---

## Safety rails

- Paper by default. `live` refuses to start without `--confirm-live`.
- Refuses API keys that can withdraw funds (checked through `/sapi/v1/account/apiRestrictions`).
- Keys come only from `BINANCE_API_KEY` / `BINANCE_API_SECRET`, never from the config file.
- Create a file named `STOP` in the working directory to stop all trading within half a second.
- Every order is checked against Binance filters before it is sent.
- Hard halt on stuck inventory, unknown order state or internal errors. It never fails silently.
- On Ctrl-C it waits for an in-flight cycle to finish instead of abandoning it halfway.

---

## Quick start

Requires Python 3.11+.

```bash
cd binance-arb-agent
pip install -e '.[dev]'

arbagent scan --duration 300            # watch live markets, no keys, no orders
arbagent paper --duration 3600          # full agent, simulated fills, no keys
arbagent report --mode paper            # summarise the journal
arbagent ping                           # your real round trip to Binance; set paper.rtt_ms to it

arbagent record --out rec.jsonl.gz --duration 1800   # capture a session...
arbagent replay rec.jsonl.gz                         # ...and replay it with different settings

cp config.example.toml config.toml      # every setting, documented
arbagent -c config.toml paper
```

Live, testnet first (testnet prices are not real markets, so this only checks the order plumbing):

```bash
export BINANCE_API_KEY=... BINANCE_API_SECRET=...      # trade-only key, withdrawals OFF, IP-whitelisted
arbagent live --testnet --confirm-live --duration 600
arbagent -c config.toml live --confirm-live            # real money
touch STOP                                             # stop trading now
```

`api.binance.com` returns HTTP 451 from restricted regions (for example the US). Binance.US is a separate exchange and isn't supported.

---

## Tests

```bash
python -m pytest -q        # 54 tests, < 1 s, no network
```

They cover:

- Filter parsing and quantization, including the legacy `MIN_NOTIONAL` filter and market orders that must pass both lot-size filters.
- Triangle discovery, float and exact pricing, dust valuation and the size optimiser.
- Brain maths, learning, forgetting, phantoms and save/load. Every risk breaker.
- Executor scenarios on a virtual clock: complete, leg-1 miss, leg-2 IOC failure finished at market, unwind when the rest of the cycle collapses, and a stuck position when a market disappears.
- The live gateway against a fake exchange: FULL fills with BNB commission, rejection, rate limit (halts), 5xx resolved by client order id, timeout then not-found, and an unresolvable order raising.
- The request signature against Binance's published HMAC example.
- An end-to-end replay that checks the agent never sells more than the visible liquidity.

---

## Limitations (honest list)

- **Paper fills are an upper bound.** They are checked against the real book after the simulated latency, but queue position and other traders' orders arriving at the same instant aren't modelled.
- Sizes are capped at the **best level** of each book (`@bookTicker`). Depth beyond the top is not streamed.
- REST order entry, one leg after another, one cycle at a time. The WebSocket API and parallel legs with pre-positioned inventory would be the next steps for speed.
- HMAC keys only (no Ed25519/RSA).
- Pure Python. It keeps up with the full market on one core, but it won't win a microsecond race.
- Tax and legal treatment of frequent trading varies by country. That's on you.

MIT licence. Use at your own risk.
