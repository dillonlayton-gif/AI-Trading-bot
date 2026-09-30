# Guarded paper trader

Python 3.11+ paper trading prototype with a separate public market-data recorder. It cannot submit Coinbase orders and contains no exchange credentials. All intents reach `PaperBroker.submit`, which calls the deterministic `RiskEngine` before any ledger fill. AI output, if added later, must be converted to an `Intent` and use this boundary. This is an engineering prototype, not a profitable strategy or an authorization to trade real funds.

## Run

```bash
python -m unittest discover -s tests -v
python -m trading_bot.cli replay data/example.csv --db paper.db --buy-quantity 0.5
python -m trading_bot.cli stop --db paper.db
```

CSV columns: `time` (timezone-aware ISO 8601), `price` (positive decimal). Replay is single-product, sequential, durable SQLite. Reuse of a ledger refuses duplicate or earlier timestamps. Paper fills apply 0.2% adverse slippage and 0.6% fee. Default risk limits: $100 per order, 20% position/equity, 3% UTC-day mark-to-market loss, no shorting or leverage. Stop persists. Risk limits are fixed in code for this phase. Replay does not place automatic strategy orders; `--buy-quantity` submits one sample order through risk for demonstration. Logs include marks, rejects, fills and fees in SQLite `events` and standard logging.

## Sequential gates

1. **Core and paper ledger:** implemented; unit tests and replay command verified locally.
2. **Market data:** public historical candles and WebSocket recorder implemented; see the Phase 2 commands and validation status below. No trading key or strategy integration.
3. **Indicators/features:** deterministic finalized-candle features implemented; see Phase 3 below.
4. **Strategy and backtesting:** deterministic offline long/flat baselines implemented; see Phase 4 below.
5. **Risk and recovery:** durable deterministic paper risk gate and restart reconciliation implemented; see Phase 5 below. Real funds remain disconnected.

A Coinbase live broker is deliberately absent. Adding one requires a separate reviewed implementation and explicit decision after the paper gates. Coinbase documents the Advanced Trade Python SDK and REST/WebSocket APIs: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sdk


## Phase 2: public market data

Install the tested WebSocket transport dependency in an isolated environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install '.[market]'
python -m unittest discover -s tests -v
python scripts/validate_market_data.py
```

The offline smoke uses explicitly synthetic data in `data/market_sample.json`. It rebuilds an archive and feed state from the journal, compares results and prints a SHA-256 digest. The foundation's five paper/risk tests and original CSV replay remain separate regression checks. GitHub Actions is configured to run these checks on Linux and Windows without Coinbase access or repository write permissions.

Retrieve completed, aligned UTC candles from the unauthenticated Coinbase Advanced Trade public endpoint:

```bash
python -m trading_bot.market_data.cli historical --db market.db --product BTC-USD \
  --start 2026-01-01T00:00:00Z --end 2026-01-01T01:00:00Z --seconds 300
python -m trading_bot.market_data.cli live --db market.db --product BTC-USD --duration 600
python -m trading_bot.market_data.cli health --db market.db --product BTC-USD
python -m trading_bot.market_data.cli replay --db market.db --product BTC-USD --seconds 300 > candles.jsonl
python -m trading_bot.market_data.cli replay-journal --db market.db --output-db rebuilt-market.db
```

Historical ranges are half-open `[start,end)` with timezone-aware, bucket-aligned endpoints; `end` must already be completed. Supported bucket sizes in seconds: 60, 300, 900, 1800, 3600, 7200, 14400, 21600, 86400. Requests are paged to at most 350 rows, sorted and validated. Missing buckets remain gaps, without invented prices or volume. Retryable network/429/5xx errors get four attempts with 1/2/4-second backoff (30-second cap, 10-second request timeout); permanent errors and invalid data fail immediately. Numeric/date `Retry-After` is respected up to that cap. Earlier pages remain durable if a later request fails; rerun the range to resume idempotently.

The public WebSocket uses separate `heartbeats` and `candles` subscriptions and a single product per collector/database. Coinbase live candles have five-minute buckets and revisions every second. These are **provisional** and excluded from finalized candle replay until REST confirms the completed bucket. Revisions must preserve the open, expand OHLC bounds and not reduce volume. Finalized rows are immutable; conflicting data latches an integrity error requiring operator review. A newly closed bucket schedules REST confirmation even without a sequence gap.

Sequence tracking resets on reconnect and is independent per channel. Duplicate sequences/counters, older sequences/timestamps/buckets, invalid values, stale messages and future timestamps are recorded without refreshing accepted-data timestamps. Channel or heartbeat gaps, bucket gaps and reconnects schedule REST recovery. Backfill runs concurrently with live ingestion, one page per attempt at most every 30 seconds; it advances only over a contiguous prefix of finalized candles. Missing data keeps the feed degraded. An integrity error remains latched even if the gap is subsequently repaired.

Health states are `warming`, `healthy`, `stale`, `degraded`, `disconnected`, `stopped` or `unavailable`. Defaults: maximum incoming message age 15 seconds; heartbeat age 15 seconds; candle update age 30 seconds; future clock tolerance 5 seconds. Health includes counters, ages, session, reconnect count, unresolved repair range and last error. Freshness uses exchange timestamps, so retransmitted old data cannot make the feed healthy. A quiet/invalid feed reconnects; transport failure uses capped exponential 1/2/4/.../30-second backoff and resubscribes. SIGINT/SIGTERM handlers are installed where the event loop supports them; unsupported handlers (including Windows event loops) are skipped. `--duration` works on both Windows and Unix. Supported signals or `--duration` stop ingestion and drains any bounded in-flight REST request before closing the database. Duration is a stop trigger, not a strict process-exit deadline.

SQLite WAL/FULL synchronization stores canonical candles, raw public REST/WS responses, outcomes, session controls and persistent health state. Candle writes, journal entries and health changes are transactional. Binary/oversized WS frames retain an omission marker and rejection reason rather than storing arbitrary payloads. Standard logs report normalization outcomes, gaps, retries, failures and reconnects; raw payloads stay in the journal. Run one writer per database. Shut down the writer before copying the database for backup; copying only the main file while WAL is active is unsafe. Disk retention/rotation and long-running soak validation are later operational work.

`replay` opens the source database read-only and emits canonical finalized candles in ascending bucket order. `replay-journal` requires a new destination path and replays receipt times, sessions, raw messages and captured recovery results without using the wall clock or network; it compares every decision and the resulting archive. It cannot reconstruct unjournaled manual database edits. Repeated finalized replay gives byte-identical output and hash. This layer imports no paper broker, risk engine, strategy or AI provider, and never submits trading intents.

API contracts: [Coinbase WebSocket overview](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-overview), [public endpoints/channels](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-endpoints), [official public REST client](https://github.com/coinbase/coinbase-advanced-py/blob/master/coinbase/rest/public.py), [WebSocket message schemas](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/advanced-trade-asyncapi.json). External Coinbase connectivity must be confirmed in the deployment environment before a forward-data soak. No order credentials or live execution are included.

See `PHASE2_VALIDATION.md` for this checkpoint's results and outstanding environment/GitHub checks.


## Package layout and CLI entry checks

Extract checkpoint ZIPs with their directory structure intact. These files have separate roles:

| File | Role |
| --- | --- |
| `trading_bot/__init__.py` | Root namespace, without eager imports |
| `trading_bot/cli.py` | Phase 1 paper replay and stop commands |
| `trading_bot/market_data/__init__.py` | MarketCandle and MarketStore exports from the market-data subpackage |
| `trading_bot/market_data/cli.py` | Phase 2 public data commands, including the Windows signal fix |

The repeated filenames belong to their containing folders; preserve the full paths when copying updates. Check both entry points after extraction:

```bash
python -m trading_bot.cli --help
python -m trading_bot.market_data.cli --help
python -m unittest discover -s tests -v
```

Package-entry regression tests launch fresh Python subprocesses to import the public package paths, display market-data help, replay archived market candles and run the original paper sample. Wheel validation also checks installed CLI entry points from outside the source directory.


## Phase 3: deterministic indicators and features

The independent `trading_bot.indicators` package consumes normalized `MarketCandle` objects.
It imports only the market-data model and Python standard library. It has no strategy,
AI, broker, risk-engine, credentials, network or execution dependency. There are no
signals, intents or trading decisions in this layer.

Run the complete offline gate:

```bash
python -m unittest discover -s tests -v
python scripts/validate_market_data.py
python scripts/validate_indicators.py
python scripts/validate_indicators.py --output features.jsonl
python scripts/scan_secrets.py
python -m trading_bot.cli replay data/example.csv --db fresh-paper.db --buy-quantity 0.5
```

Use a fresh paper database for each sample replay. The indicator validator uses the
explicitly synthetic 16-candle fixture `data/indicator_sample.json`, compares batch,
sequential and repeated replay, and checks a pinned SHA-256 digest. Optional output
is canonical sorted-key JSONL with decimal strings, `null` values and LF bytes on
both Windows and Unix. No database or network is needed for this validation.

Example with an existing finalized archive (stop the writer before reproducible replay):

```python
from pathlib import Path
from trading_bot.market_data import MarketStore
from trading_bot.indicators import FeatureConfig, FeatureEngine, calculate

store = MarketStore(Path("market.db"), readonly=True)
try:
    # MarketStore.candles defaults to finalized=True; provisional WS rows are excluded.
    candles = list(store.candles("BTC-USD", 300))
    config = FeatureConfig()
    rows = calculate(candles, config, finalized=True)
    engine = FeatureEngine(config)
    sequential = tuple(engine.update(c, finalized=True) for c in candles)
    assert rows == sequential
finally:
    store.close()
```

`finalized=True` is a required caller certification: the model itself has no
finalization flag. Feed raw/provisional revisions must never be passed as finalized.
There is no automatic live feed hookup. Sequential updates can consume newly
REST-finalized candles; they return immutable rows and never revise earlier output.
For a revised archive, replay a fresh engine rather than editing historical rows.

Each row preserves product, candle start and granularity. `available_at` is the
bucket end, the earliest candle-close time at which its complete OHLCV can be known;
actual REST confirmation may occur later. Use that actual receipt time when evaluating
latency. No current-candle feature is available before its close. Calculation order
must be ascending, one product and one granularity per engine; no sorting, filling,
look-ahead, wall-clock reads or external state is used.

All arithmetic uses an isolated Decimal context: 34 significant digits, half-even
rounding, explicit exponent limits and arithmetic traps. Logarithms and square roots
use Decimal operations, not binary floats. Outputs are finite Decimal values or
`None`, with `unavailable` giving `warmup` or `zero_volume` for each missing feature.
Warm-up is full-window, with no partial estimates. Periods must be integers in
`[1,10000]`; MACD fast must be smaller than slow. Default periods and definitions:

| Feature | Default | Definition | First available candle in contiguous segment |
| --- | --- | --- | --- |
| SMA | 20 | Mean of trailing closes | N |
| EMA | 20 | Seed with N-close SMA; update E + 2/(N+1) × (close−E) | N |
| RSI | 14 | Wilder-smoothed gains/losses, seeded from N close changes | N+1 |
| MACD | 12/26/9 | Fast seeded EMA minus slow seeded EMA; SMA-seeded signal EMA of MACD values | Slow; signal/histogram at Slow+Signal−1 |
| ATR | 14 | First TR = high−low; later TR = max(high−low, abs(high−previous close), abs(low−previous close)); N-TR mean seed, Wilder smoothing | N |
| Rolling VWAP | 20 | Sum(((high+low+close)/3) × volume) / sum(volume) over N candles | N, if total volume > 0 |
| Simple return | 1 change | close/previous close − 1, fraction | 2 |
| Log return | 1 change | ln(close/previous close) | 2 |
| Momentum / ROC | 10 | close−close[N bars ago]; close/close[N bars ago]−1, fraction | N+1 |
| Rolling volatility | 20 | Population standard deviation of N log returns; no annualization | N+1 |
| Volume SMA | 20 | Mean of trailing N base-asset volumes, including current candle | N |
| Relative volume | 20 | Current volume / current trailing N-volume mean | N, if denominator > 0 |
| Volume vs prior | 20 | Current volume / preceding N-volume mean, excluding current candle | N+1, if denominator > 0 |

RSI is 50 for a flat series, 100 for only gains, and 0 for only losses. Zero-volume
candles are valid and retained; zero VWAP/relative-volume denominators explicitly
return unavailable. Arbitrarily small positive volumes are retained. Rolling VWAP
is an OHLC typical-price approximation, not tick VWAP, and does not reset by calendar
session. Volatility is per candle; changing granularity changes its interpretation.

Invalid prices, OHLC bounds, volumes, schemas or configuration are rejected. Duplicate,
out-of-order, revised candles and product/granularity changes are rejected without
changing engine state. Missing buckets raise `DataError` by default. Repair the archive
and replay, or explicitly use `FeatureConfig(gap_policy="reset")`: a gap then starts a
new segment, resets every indicator/warm-up, and marks the first row `gap_reset=True`.
No return is computed across a missing bucket. Arithmetic failure leaves state intact.

The engine retains bounded rolling windows and EMA/Wilder state; batch calculation
materializes output rows and runs the same sequential algorithm. Persistence of engine
state across process restarts is not included: rebuild from the finalized archive.
See `PHASE3_VALIDATION.md` for the verified checkpoint and scope boundary.


## Phase 4: deterministic strategy and backtesting

The `trading_bot.backtest` package is an offline research simulator. It consumes
finalized normalized `MarketCandle` objects and computes Phase 3 features sequentially.
It cannot submit paper or Coinbase orders and does not import any broker, credentials,
network client or AI provider. The existing paper engine and mandatory risk boundary
are unchanged. This research simulator does not apply the paper engine's $100 order,
20% position or 3% daily-loss limits; a future paper integration must route every intent
through that engine. There is no integration or live-order capability in Phase 4.

Run the complete offline gate:

```bash
python -m unittest discover -s tests -v
python scripts/validate_market_data.py
python scripts/validate_indicators.py
python scripts/validate_backtest.py
python scripts/validate_backtest.py --output backtest-result.json
python scripts/scan_secrets.py
python -m trading_bot.cli replay data/example.csv --db fresh-paper.db --buy-quantity 0.5
```

Use a new paper database for the sample. The backtest validator runs both explicitly
named baselines on the synthetic fixture `data/backtest_sample.json`, checks known
trades/metrics, compares sequential and repeated replay, checks the benchmark, and
verifies a pinned SHA-256 hash. Saved output uses sorted-key JSON, decimal strings and
LF bytes on Windows and Unix. GitHub Actions includes this offline gate alongside all
previous checks; no remote CI result is claimed by local validation.

| Baseline | Long rule | Flat/exit rule | Warm-up |
| --- | --- | --- | --- |
| `SMATrend` | Finalized close > trailing SMA | close <= SMA | Flat until N SMA candles; default N=20 |
| `MACDTrend` | MACD line > signal EMA | MACD <= signal | Flat until slow+signal−1 candles; defaults 12/26/9, ready at candle 34 |

These are target-state rules, not optimized recommendations. Equality is flat.
There is no strategy ranking, automatic selection, optimization or adaptive logic.
`Strategy.decide(candle, features)` returns an immutable `Signal('long' or 'flat', reason)`;
the built-ins are frozen, stateless and inspect only the supplied aligned current row.
A custom implementation must be deterministic, deepcopy-compatible, and must not access
external/future state. The Python interface is not a sandbox for untrusted plugins.

The engine starts flat. A target computed at candle close can cause a fill only at
the **next contiguous candle's open**, using that open as reference price. Fill timestamps
include the preceding signal's availability time. No current-close signal fills at
that same close. Only supplied completed candles and internally generated causal
features are consumed. No future rows are passed to a strategy, no input is sorted,
and no missing prices or candles are invented. OHLC data is revalidated. Duplicate,
revised, out-of-order, mixed-product/granularity, provisional or gapped input fails.
Caller certification `finalized=True` is mandatory; the model has no finalization flag.
Use the market store's default finalized-only candle iterator.

`BacktestConfig` defaults: cash 10000, fixed entry quantity 0.5 base units, fee 0.006
(0.6%) of filled notional each side, adverse slippage 0.002 (0.2%) each side. BUY price
is open × 1.002; SELL price is open × 0.998. These defaults are tested against Phase 1
`Limits`. Cash cannot go negative; unaffordable entries fail without advancing state.
No shorts, leverage, pyramiding, partial fills, rebalancing or cash injection occur.
Long-to-long and flat-to-flat targets produce no extra fills. Flat-to-long buys the
fixed quantity; long-to-flat sells the complete position. Strategy/feature/account state
is copied before a step and committed on success. Persistent immutable ledger entries
allow rollback without rewriting prior rows. Result snapshots are immutable.

Entry basis includes entry fees. Realized P&L is net sale proceeds minus full entry
basis, including both fees and slippage. Unrealized P&L is units × current close minus
remaining basis; it includes entry costs but no estimated exit fee/slippage. Equity
is cash + units × close. No final liquidation is invented: an open position remains
marked to market, and the last candle's new target remains unfilled without a next open.
The result includes close-marked equity/account rows, target/reason events, fill ledger,
closed round-trip ledger, costs, configuration and separate benchmark metrics.

The buy-and-hold benchmark is prespecified before replay: it buys the same fixed
quantity at the **first candle open**, pays the same costs, and holds through the last
close. It keeps the remaining cash and is independent of strategy warm-up. It is not
an all-capital benchmark. It remains open and pays no hypothetical final-sale costs.
`excess_total_return` is strategy return minus benchmark return for the same cash,
quantity, candles and dates; it is not risk-adjusted alpha.

| Metric | Definition / unavailable behavior |
| --- | --- |
| Total return | Final close equity / initial cash − 1 |
| Max drawdown | Largest decline from a running close-equity peak, including initial cash; fraction |
| Trade count / fill count | Closed round trips / individual BUY and SELL fills; open trades excluded from trade statistics |
| Win/loss rate | Positive/negative net-P&L trips divided by all closed trips; breakevens counted separately; null with no closed trips |
| Average win/loss | Mean positive / negative currency P&L; null when that class is absent |
| Expectancy | Mean net currency P&L over all closed trips, including breakevens; null with no trips |
| Profit factor | Total positive P&L / absolute total negative P&L; null with no losses, zero when losses but no wins |
| Exposure / time in market | Fraction of full candle intervals held after open fills / corresponding seconds; binary time exposure, not capital fraction |
| Return volatility | Population standard deviation of per-bar simple equity returns, including the first close versus initial cash; null with fewer than two bars |
| Annualized volatility / Sharpe | Volatility × sqrt(365×86400/seconds); mean per-bar return / volatility × same factor, zero risk-free rate; Sharpe null with zero volatility or fewer than two bars |
| Sortino | Mean per-bar return / downside RMS × same annualizer; downside RMS includes zero terms for nonnegative returns; null with no downside or fewer than two bars |

All arithmetic uses the same isolated 34-digit half-even Decimal policy as Phase 3.
No wall-clock-dependent values appear in results. Short synthetic samples make annualized
metrics descriptive regression values, not evidence of expected performance. Intrabar
peaks/drawdowns, liquidity, volume participation, spreads beyond configured slippage,
funding, taxes, market impact and exchange minimum-size/precision rules are not modeled.
Zero-volume candles remain valid: no fill filter uses the execution candle's future
complete volume. Full fills at open are an explicit simulation assumption even on those
candles. This model requires a separate realism/forward-paper validation phase before
any trading deployment. No autonomous selection or deployment is included.

Example with a finalized archive:

```python
from pathlib import Path
from trading_bot.market_data import MarketStore
from trading_bot.indicators import FeatureConfig
from trading_bot.backtest import BacktestConfig, BacktestEngine, SMATrend, run_backtest

store = MarketStore(Path("market.db"), readonly=True)
try:
    candles = list(store.candles("BTC-USD", 300))
    features, costs = FeatureConfig(), BacktestConfig()
    result = run_backtest(candles, SMATrend(), features, costs, finalized=True)
    engine = BacktestEngine(SMATrend(), features, costs)
    for candle in candles:
        engine.step(candle, finalized=True)
    assert result.serialize() == engine.result().serialize()
    Path("research-result.json").write_bytes(result.serialize().encode("utf-8"))
    print(dict(result.metrics))
finally:
    store.close()
```

Stop the writer before taking a reproducible archive snapshot. Empty or incomplete
archives fail. To handle a gap, repair the archive or explicitly split it into independent
runs with fresh capital and report them separately; silently joining those runs is not
supported. After a process restart, replay from the archived start; checkpoint/resume
of engine state is not implemented. See `PHASE4_VALIDATION.md` for known fixture results,
verification evidence and remaining limitations. Phase 5 adds a separate durable risk-gated paper API below.


## Phase 5: durable paper risk and recovery

`trading_bot.recovery.DurablePaperLedger` strengthens the original risk assumptions in a
new, explicit paper-only persistence schema. It consumes normalized finalized candles
and explicitly supplied intents. Its only financial write operation is `submit`, which
always runs the deterministic risk reducer. There is no raw fill API, strategy access to
accounting setters, broker integration, AI, exchange key, network client or live-order
capability. Phase 4 remains an offline research simulator, not an order/intent route.
The Phase 1 CLI and broker remain compatibility demonstrations with their original risk
gate; they do **not** provide the new restart guarantees. Use the durable API for Phase 5.
Legacy databases are rejected rather than implicitly migrated or guessed.

```bash
python -m unittest discover -s tests -v
python scripts/validate_market_data.py
python scripts/validate_indicators.py
python scripts/validate_backtest.py
python scripts/validate_recovery.py
python scripts/validate_recovery.py --output recovery-result.json
python scripts/scan_secrets.py
```

Example with a finalized candle from a completed archive (no autonomous strategy loop):

```python
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from trading_bot.recovery import DurablePaperLedger, OrderIntent, RiskLimits

ledger = DurablePaperLedger(Path("durable-paper.db"), Decimal("10000"), RiskLimits())
try:
    # candle is a normalized MarketCandle from finalized-only archive data.
    as_of = candle.start + timedelta(seconds=candle.seconds)
    decision = ledger.submit(
        OrderIntent("run1:entry1", candle.product, "BUY", Decimal("0.5")), candle,
        as_of=as_of, received_at=as_of, health="healthy", finalized=True,
    )
    print(decision)
    print(ledger.snapshot())
    print(ledger.reconcile().serialize())
finally:
    ledger.close()
```

For forward data, supply the actual recorded receipt/evaluation timestamps and observed
feed health. All times are UTC-normalized; no wall clock is consulted. Caller health and
finalization are certifications, not authenticated exchange attestations. This phase
adds no live collector-to-strategy/order wiring. Paper intents are explicit; a historical
paper fill uses the supplied finalized close with the configured costs. No Phase 4
next-open strategy execution is silently converted to a same-close signal fill.

| Risk setting | Default / rule |
| --- | --- |
| Max filled order notional | 100 quote units; equality allowed |
| Max position / portfolio exposure | 0.20 / 0.20 of projected post-cost equity; equality allowed |
| Daily loss | 0.03 of UTC-day starting marked equity; equality trips a latch |
| Drawdown | 0.10 from lifetime marked equity peak; equality trips a permanent run latch |
| Open positions | 1; configurable positive count for a multi-product spot portfolio |
| Market/receipt/held-position age | 3600 seconds maximum; equality allowed |
| Fee / slippage | 0.006 / 0.002 each side, matching Phase 1 |
| Kill switch | Persisted bool with operator reason; explicit audited enable/disable |

Long spot holdings only; no leverage/shorting. SELL quantity cannot exceed held units.
BUY must have cash for notional plus fee. Position/exposure limits use all held quantities
at recorded marks and projected equity after costs. This bounds nominal exposure, not
stop-distance risk; no stop orders or adaptive sizing are added. Stale held marks block
orders rather than valuing a portfolio from an untrusted price. Prices are marked only
from healthy, finalized, fresh, contiguous candles. Duplicate/revised/earlier candles,
granularity changes, invalid OHLC, future timestamps and unhealthy feeds are rejected.
Each accepted market packet advances one candle. Submit an intent with its packet in one
operation; do not first mark that same candle and then resubmit it as a new operation.

Order rejection priority is deterministic: duplicate ID/conflict; kill switch; market
validation; drawdown latch; daily latch; product/intent/equity validity; stale portfolio;
order notional; sell inventory; cash; open count; position; portfolio exposure; projected
loss/drawdown. Every typed risk request, including rejected and duplicate attempts, is
stored in the immutable audit. Invalid API schemas/identifiers fail before admission as
risk requests. `Decision` contains accepted/reason/intent ID and accepted fill details.

Daily loss latches for the rest of the UTC day even if equity recovers; a new UTC day
resets its day reference/latch on a valid market packet. Lifetime peak drawdown latches
for the whole run. Projected fee/slippage losses can trip either breaker without a fill.
Kill-switch clearing does not clear loss latches. All lockouts block buys **and sells**;
there is no automatic liquidation. A stop is not a guarantee of a limited final loss on
an already-open position. Reconciliation failure permanently closes that ledger object;
restore a trusted verified backup or resolve the inconsistency outside the trading API.
No automatic repair or cash/position guessing is provided.

SQLite WAL/FULL, `BEGIN IMMEDIATE` and a single transaction atomically commit the raw
operation, its recomputed risk decision/fill, SHA-256 audit chain and state projection.
No durable pending operation is normally possible. A process crash before commit rolls
back the whole operation and its intent ID; after commit, a retry cannot fill again.
After a lost response, reopen/reconcile and retry with the **same stable ID**. Previously
accepted/rejected IDs are never reused for a new intent. Equal decimal quantities have
canonical fingerprints; a changed product/side/quantity with that ID is a conflict.
Duplicate retries add a rejection audit record but never modify accounting or marks.

Persisted state includes cash, quantity/cost basis by product, realized/unrealized P&L,
equity, finalized price marks, day/day-start, daily/drawdown latches, lifetime equity peak,
kill state/reason, last evaluation time, all intent IDs/fingerprints/outcomes, initial cash
and the exact fixed limits. Partial sales use weighted-average entry basis and allocate
entry costs proportionally. BUY/SELL fills apply adverse close × 1.002/0.998 and 0.6%
notional fees. Startup ignores a replacement initial-cash argument and uses the original
persisted capital; passing different limits fails closed. Limits are frozen/read-only
and cannot be silently replaced during a run. No risk reset or limit migration API exists.

Startup and every admitted operation check SQLite integrity, metadata/schema, immutable
trigger definitions, audit sequence/hash links, recomputed risk decisions and fills, and
exact agreement of reconstructed accounting/risk/ID state with the projection. Reports
contain a canonical state JSON, event count and audit head; a healthy report has zero
pending operations. Restart itself appends no event and changes no finalized result.
Updates/deletes of audit and metadata are blocked by SQLite triggers. Hashes detect
inconsistent edits; they are not an external signature or protection against an attacker
with complete database/filesystem rewrite access. Python private members are not an
untrusted-plugin security sandbox. No AI or untrusted strategy plugin is present.

The synthetic fixture `data/recovery_sample.json` compares uninterrupted execution with
a mid-run close/reopen while positions and the kill state are persisted. It exercises
accepted fills, order rejection, kill pause/resume, unhealthy feed retry, partial exits,
full closure and a duplicate ID retry. All possible fixture restart cuts are also tested.
Final cash/equity is 1003.83526; realized P&L 3.83526; positions/unrealized P&L are zero;
kill and loss latches are false; all 14 audit events match exactly. The pinned output
includes reconstructed state, risk state, all ID outcomes, decisions and the audit log.

Remaining limitations: reconciliation replays the full journal before every operation
and is intended for modest paper workloads; no compaction, asynchronous multiwriter
service, backup automation or external audit anchor is included. Coordinate one writer,
and stop writers/checkpoint WAL or use SQLite's backup API before backups. Native Windows
Phase 5 and remote CI are not claimed by Linux local validation. No credentials, live
execution, autonomous strategy selection or Phase 6 work is implemented.
See `PHASE5_VALIDATION.md` for verification evidence.

## Phase 6: sustained forward paper sessions

Phase 6 adds the runner in `trading_bot/forward`; the Phase 5 discussion above describes
that earlier checkpoint. The Phase 1 compatibility CLI and Phase 4 research backtester
remain unchanged. This new runner uses **only** the Phase 5 durable paper gate. There
is no authenticated Coinbase client, exchange order transport, AI, strategy optimizer,
or runtime strategy replacement. Choose `sma_trend` or `macd_trend` explicitly.

The public WebSocket supplies provisional five-minute candles and heartbeats. The
inherited bounded REST repair confirms closed candles before feature calculation.
`--seconds` selects 300, 900, 1800, 3600, 7200, 14400, 21600 or 86400; higher periods
aggregate complete, contiguous UTC-aligned finalized five-minute groups. Partial
leading/trailing groups are unavailable, never filled in. One-minute forward candles
are unsupported. Product/timeframe, quantity, feature settings and risk limits are
fixed and persisted for a run; changing them requires a separate run/state directory.

Signals use only the current finalized candle's causal Phase 3 features. SMA is long
when close > SMA, otherwise flat, with a flat warm-up until its configured period;
MACD is long when MACD > signal EMA, otherwise flat, warming up for 34 bars at defaults.
A prior signal can create an order only at the **next finalized candle's close**.
This latency model differs explicitly from the Phase 4 research next-open model.
It applies adverse slippage 0.2% and fee 0.6% each side through the unchanged gate.
One fixed-quantity long position is opened; a flat signal requests sale of held units.
No pyramiding, shorting, leverage, automatic liquidation or intrabar fills are added.

The gate retains Phase 5 defaults: order notional <=100 quote units, position and
portfolio <=20% of projected equity, daily loss 3%, drawdown 10%, one open position,
maximum mark/receipt age 3600 seconds. Forward trading additionally requires healthy
feed and a completed candle/receipt no older than 90 seconds. Historic repair bars can
warm features and advance fresh-enough accounting marks but cannot create orders.
Stale/disconnected/warming/stopped feeds clear pending signals. A brief degraded REST
confirmation pause retains the previous signal but permits no processing/fills until
healthy; catch-up bars outside the trading grace clear it. The next order still needs
a fresh, contiguous finalized candle. Feed integrity faults remain latched upstream.
Missing data, accounting continuity faults and inconsistent journals stop closed.

### Windows public-data soak

Use Python 3.11 or newer (examples use 3.12). From the extracted checkpoint directory,
run these PowerShell commands; activation and exchange accounts are unnecessary:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[market]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\validate_forward.py
.\.venv\Scripts\python.exe -m trading_bot.forward.cli --state-dir .\paper-state\btc-sma --run-id btc-sma-soak --strategy sma_trend --product BTC-USD --seconds 300 --quantity 0.0005 --sma-period 20 --duration 86400
```

That final command runs for 24 hours using **public Coinbase data and simulated paper
fills only**. Omit `--duration` for continuous operation. Keep the laptop awake, on
power, with reliable Internet and an accurate UTC-synchronized clock. Firewall/proxy
must allow the existing public REST and WebSocket hosts. Do not add API keys. The
0.0005 BTC quantity is explicit; high BTC prices can exceed the 100 quote-unit limit,
which causes an audited rejection, never automatic sizing or limit changes.

A cold SMA run needs 20 complete selected-timeframe bars; at five minutes this is about
100 minutes, plus confirmation latency and the following bar for a first possible fill.
MACD default warm-up is 34 bars. No historical bootstrap or forced trade is provided;
a soak can legitimately have zero fills. Observe `paper_health`, `paper_event`, public
transport reconnect/repair messages and `paper-state\btc-sma\operations.log` (10 MB,
five rotated backups). `summary.json` records sessions, health, orders/fills/rejections,
cash/equity/P&L, peak/current/max drawdown, daily counts/equity/P&L, risk state and
reconciliation. The durable event journal records features, signals, fill lifecycle
and complete accounting snapshots. Transport heartbeat ages/counters, reconnects,
repair state and integrity errors are in feed health and the market journal.

Repeat the **same** command and state directory to resume; durable IDs and journals
prevent duplicate processing/fills. Positions and risk latches persist. Session IDs
are `run-id:0001`, `run-id:0002`, etc. Startup closes a previously active session before
starting a new one and clears its unexecuted signal. Ctrl+C and duration shutdown stop
ingestion and drain its bounded outstanding REST request before reconciliation/close.
Unsupported Windows signal registration is skipped; duration shutdown remains tested.
If power/process failure interrupts that drain, restart reconciliation is authoritative.

To toggle the persisted kill switch, stop the CLI first and use the runner's audited
`kill(enabled, reason, aware_utc_time)` API with the **identical ForwardConfig**. Do not
edit databases or use the risk ledger directly while the forward session owns it.
There is one OS-held writer lease; it releases on process death. Kill/loss breakers
also block sells; stopping does not liquidate a position.

A prepared handoff is durable before invoking the gate. After a crash, an already
committed matching risk event is adopted without retry or a duplicate audit/fill.
An uncommitted prepared **intent** blocks live startup: the new process has no validated
feed yet and will not backdate a new fill. Preserve the directory and investigate
rather than deleting pending rows. Explicit `replay_recovery=True` is limited to offline
synthetic/replay investigation, runs through the same gate and is not a CLI option.
The fixture tests exact replay recovery at every handoff boundary; an actual child
process exit after risk commit also recovers without duplicates.

### Reproduce the Phase 6 checkpoint

```powershell
.\.venv\Scripts\python.exe scripts\validate_market_data.py
.\.venv\Scripts\python.exe scripts\validate_indicators.py
.\.venv\Scripts\python.exe scripts\validate_backtest.py
.\.venv\Scripts\python.exe scripts\validate_recovery.py
.\.venv\Scripts\python.exe scripts\validate_forward.py --output forward-result.json
```

The forward fixture compares uninterrupted execution, restart with an open position,
and replay from the completed session journal. Pinned SHA-256:
`f4de9bc4359c96bc3bca55aa3d1069d38dbaf474aee937937feaca2944e6abc7`.
It has two sessions, 12 bars, four fills, one kill-switch risk rejection and two feed
pauses. Final cash/equity 9978.31976; realized P&L -21.68024; no open positions.

All 253 local automated tests pass (193 inherited +60 Phase 6). Native Windows and a
real 24-hour public-data soak have **not** been performed for this checkpoint. Remaining
limits include full-journal reconstruction cost, no compaction/automated backups,
no multiwriter/multiproduct service and no automatic recovery across missing finalized
bars or an outage exceeding the gate's accounting freshness window. If continuity
cannot be established, the run stops closed instead of fabricating prices/fills.
Back up all session/risk/market databases and their WAL safely after stopping, or use
SQLite's backup API. Hash chains detect inconsistency but do not authenticate files
against an attacker with complete local rewrite access. See `PHASE6_VALIDATION.md`.
