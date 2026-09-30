# Phase 3 validation gate

Verified 2026-09-29 (America/Chicago). Scope: deterministic indicators/features only.
Local branch: `phase-3-indicators`, created directly from published Phase 2 commit
`66137cf606b0946570330c0b2013f9add6a10875`. Its complete tree was compared with local
checkpoint `d9bc43f13b595c66a4319591999c653188ca3612` before branching: no differences.
The checkpoint is the commit containing this report; obtain it with `git rev-parse HEAD`.

## Implementation

`trading_bot/indicators/features.py` implements SMA, SMA-seeded EMA, Wilder RSI,
MACD/signal/histogram, Wilder ATR, rolling typical-price VWAP, simple/log returns,
momentum/ROC, population log-return volatility and rolling volume/relative-volume
features. `trading_bot/indicators/__init__.py` exports the independent API.
README defines every formula, warm-up boundary, timestamp convention and gap policy.

Fixed 34-digit Decimal context isolates precision, rounding, exponent limits and traps.
Warm-up and zero-volume denominators produce explicit unavailable values/reasons.
Normalized, caller-certified finalized candles only; duplicates, revisions, earlier
candles and product/granularity changes fail without changing state. Gaps reject by
default, or explicitly reset the complete contiguous segment. Immutable output rows
remain unchanged by subsequent updates. Current configuration is read-only.
Rolling history is bounded; batch and sequential calculations use the same algorithm.
No future candle, wall clock, network, paper broker or execution dependency is used.

## Verified results

| Check | Result |
| --- | --- |
| Existing Phase 1/2 suite before changes | 53 passed |
| Full Phase 1/2/3 suite | 95 passed: all 53 existing + 42 new |
| Known expected values | SMA, seeded EMA, RSI including classic 14-period seed, nonlinear MACD, ATR, VWAP, returns, momentum/ROC, volatility and volume ratios passed |
| Edge cases | Warm-up for every feature, minimum periods, flat/rising/falling prices, zero/tiny volume, gaps, invalid config/values, provisional rows, duplicates/revisions, alignment and arithmetic failure atomicity passed |
| Causality and determinism | Every prefix unchanged by future suffix; immutable earlier rows; batch/sequential 200-candle stream, gap restart, repeated fixture replay and hostile ambient Decimal context passed |
| Archive integration | Finalized market-store replay accepted; provisional row excluded |
| Phase 2 transport and recovery | Existing local WebSocket transport, reconnect/backoff, concurrent REST repair, Windows unsupported-signal and clean stop/drain tests all passed |
| Package-entry regressions | All existing package/CLI entry tests passed |
| Indicator output portability | Fresh subprocess ran validator outside repository cwd; saved JSONL had canonical LF bytes and pinned hash |
| Phase 1 paper sample | cash=9949.5994000, units=0.5, stopped=0 |
| Phase 2 deterministic market-data replay | 7 journal records, 3 replay candles, 2 finalized; archive and health matched |
| Phase 3 deterministic replay | 16 synthetic candles, 16 features per row; batch/sequential/repeated output and pinned hash matched |
| Credential/private-key scan | All 33 tracked text files scanned; zero pattern matches |
| Scope and regression review | Existing paper/market source and tests unchanged; new indicator dependency allowlist test passed; no strategy, AI, order credentials or execution functionality added |

Market-data replay SHA-256:
`10f97aaa4635ff41c5ea5a644183d77b0ffee2057dc59e83fd90dfbe1c86250e`

Indicator replay SHA-256:
`a82bc6072b239f76d355d488881cbaffde6dd25188fbca96debfb2ff980b2062`

## Reproduce

```bash
python -m pip install '.[market]'
python -m unittest discover -s tests -v
python scripts/validate_market_data.py
python scripts/validate_indicators.py --output features.jsonl
python scripts/scan_secrets.py
python -m trading_bot.cli replay data/example.csv --db fresh-paper.db --buy-quantity 0.5
```

Use a new paper database. The synthetic indicator fixture and pinned digest are
tracked; generated JSONL/database files are not needed in the checkpoint.
GitHub Actions now includes the indicator validator in its existing Linux/Windows,
Python 3.11/3.12 matrix. That remote CI has not been executed as part of this local gate.
This validation ran on Linux; native Windows Phase 3 execution was not independently
performed here. Phase 2's previously reported native Windows Coinbase results remain
historical evidence, not a new connectivity check in this phase.

## Boundaries and limitations

`available_at` denotes candle bucket end, not actual REST receipt time. Callers must
use only finalized archive data and account for confirmation delay in later evaluation.
VWAP uses candle typical prices rather than trades. Volatility is population/per-bar
and unannualized. Returns/ROC are fractions. Definitions intentionally avoid partial
warm-up estimates and calendar session resets. Rebuild an engine by deterministic
archive replay after restart; no persistent engine-state schema is added.
Credential scanning detects known patterns; it is not proof against every possible
secret encoding. No credentials were supplied or intentionally added.

The local Phase 3 gate is satisfied. GitHub publication/remote CI is not claimed.
No GitHub write, main modification/merge, real funds connection or Phase 4 work occurred.
