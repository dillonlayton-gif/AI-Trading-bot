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
4. **Strategy and evaluation:** future work requiring separate authorization; no strategy is implemented.
5. **Operations and extended paper validation:** future work requiring separate authorization. Real funds remain disconnected.

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
