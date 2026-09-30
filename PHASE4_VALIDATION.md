# Phase 4 validation gate

Verified 2026-09-30 (America/Chicago). Local branch: `phase-4-strategy-backtest`.
Base is published Phase 3 commit `cb9c352df816770b36a2eb3f06cc4ca8a17be98a`.
Before branching, its full tracked tree was rechecked against local checkpoint
`5233eca04a5ba6b65b39872b7a2a3edde268bcdd`: identical. The Phase 4 checkpoint is the
commit containing this report (`git rev-parse HEAD`). No GitHub writes were performed.

## Exact baseline rules

| Strategy | Entry target | Exit target | Warm-up |
| --- | --- | --- | --- |
| `SMATrend` (`sma_trend`) | long when finalized close > current trailing SMA | flat when close <= SMA | flat until SMA has N candles; default 20, fixture 2 |
| `MACDTrend` (`macd_trend`) | long when MACD > signal EMA | flat when MACD <= signal | flat until slow+signal−1 candles; defaults 12/26/9 (34 candles), fixture 2/3/2 (4 candles) |

Both are frozen, stateless deterministic baselines. No tuning, selection, rankings,
AI, outside state or automatic deployment is implemented. Signal reasons and targets
are retained in immutable bar events. The interface supplies only the current finalized
candle and its internally computed Phase 3 feature row, never future history.

## Simulation assumptions and causal timing

- Long/flat only, fixed base quantity, one position, no pyramiding/rebalancing/shorts/leverage.
- Initial cash 10000 and quantity 0.5 by default. Fixture uses cash 1000 and quantity 1.
- Fee = 0.006 of actual fill notional on each side; adverse slippage = 0.002 on each side.
  BUY at next open × 1.002; SELL at next open × 0.998. Defaults match Phase 1 `Limits`.
- Close signals execute at the next contiguous candle open. No same-close fills.
  First bar has no pending strategy order. Last-close targets have no fill without a next bar.
- Buy-and-hold is defined before replay and buys the same fixed quantity at the first open;
  it holds remaining cash and keeps its position open. It is not an all-cash allocation.
- Mark at close. Entry basis includes entry fees; realized P&L includes both sides' costs;
  unrealized P&L includes entry costs but no hypothetical exit cost. No final liquidation.
- Reject missing, duplicate/revised/earlier, mixed-product/granularity and provisional input.
  Do not fill gaps or reset features inside a run. Repair data or run separate fresh-capital
  segments. Finalization is a required caller certification; use finalized-only archives.
- Step changes are atomic, including feature and custom strategy state. Previous bar/fill/trade
  snapshots cannot be rewritten. A strategy gets a copied candle, protecting execution input.
  Fixed 34-digit Decimal arithmetic and canonical JSON serialization are independent of
  wall clock and ambient precision. Batch/sequential/repeated results are byte-identical.
- This simulator has no broker or risk-engine integration. The unchanged paper engine still
  enforces its mandatory risk boundary. Its order/position/day-loss limits are not modeled
  here. Any future paper integration must route every intent through that boundary.

## Known synthetic fixture

`data/backtest_sample.json` has 16 contiguous BTC-USD five-minute candles, open=close:
10, 12, 14, 12, 10, 11, 13, 15, 12, 14, 10, 8, 12, 13, 11, 10.
Indices below are zero-based. OHLC envelopes and volumes are explicitly synthetic.
The fixture stores expected trade indices, net P&L and selected metrics; the pinned
result covers every event, fill, trade, configuration, equity row and benchmark metric.

| Strategy | Entry → exit index | Net trade P&L |
| --- | --- | --- |
| SMA | 2 → 4 | -4.192048 |
| SMA | 6 → 9 | +0.784012 |
| SMA | 10 → 11 | -2.144024 |
| SMA | 13 → 15 | -3.184036 |
| MACD | 7 → 9 | -1.232012 |
| MACD | 10 → 11 | -2.144024 |
| MACD | 13 → 15 | -3.184036 |

| Result | SMA | MACD | Buy-and-hold |
| --- | --- | --- | --- |
| Closed trips / fills | 4 / 8 | 3 / 6 | 0 / 1 |
| Final equity | 991.263904 | 993.439928 | 999.91988 |
| Total return (fraction) | -0.008736096 | -0.006560072 | -0.00008012 |
| Realized P&L | -8.736096 | -6.560072 | 0 |
| Unrealized P&L | 0 | 0 | -0.08012 |
| Total fees | 0.552096 | 0.420072 | 0.06012 |
| Win / loss rates | 0.25 / 0.75 | 0 / 1 | null / null |
| Time exposure | 0.5 | 0.3125 | 1 |
| Max close drawdown | 0.008736096 | 0.006560072 | 7 / 1004.91988 |

SMA expectancy is -2.184024 per closed trip; average win +0.784012;
average loss -3.173369333333333333333333333333333; profit factor
0.08235326742091581313993496712432254. Losses are intentional validation scenarios,
not an optimization target. README specifies all metric formulas and null cases.

## Validation results

| Check | Result |
| --- | --- |
| Inherited tests before implementation | 95 passed |
| Complete Phase 1/2/3/4 suite | 140 passed: 95 inherited + 45 new |
| Baseline exact rules and warm-up | passed |
| Causal timing / future suffix invariance / aligned feature interface | passed |
| Flat/rising/falling, profitable/losing/breakeven and zero-trade cases | passed |
| Fees/slippage, transitions, cash/equity, realized/unrealized P&L | passed |
| Drawdown, rates, expectancy, profit factor, exposure and volatility-adjusted metrics | known values passed |
| Gaps, duplicates, revisions, invalid configuration/input, no leverage, failure atomicity | passed |
| Finalized archive integration, provisional exclusion, immutable snapshots | passed |
| Repeated and batch/sequential bytes; ambient Decimal context isolation | passed |
| Independent reproducible benchmark and excess return | passed |
| Fresh subprocess validator and portable output bytes | passed |
| Paper sample | cash=9949.5994000; units=0.5; stopped=0 |
| Phase 2 deterministic archive/health replay and transport/recovery tests | passed |
| Phase 3 pinned indicator replay | passed |
| Phase 4 known trades/metrics, pinned replay | passed |
| Credential/private-key scan | 41 tracked text files; zero matches |
| Phase 1/2/3 implementation and inherited tests | unchanged |
| Scope dependency allowlist | passed; no broker, execution, AI or network imports in new package |

Phase 4 SHA-256:
`9fce5de70cf0156afb363e60f59a522170421776d4c013ac1016b9fb0dff09bf`

Phase 3 SHA-256:
`a82bc6072b239f76d355d488881cbaffde6dd25188fbca96debfb2ff980b2062`

Phase 2 SHA-256:
`10f97aaa4635ff41c5ea5a644183d77b0ffee2057dc59e83fd90dfbe1c86250e`

## Reproduce

```bash
python -m pip install '.[market]'
python -m unittest discover -s tests -v
python scripts/validate_market_data.py
python scripts/validate_indicators.py
python scripts/validate_backtest.py --output backtest-result.json
python scripts/scan_secrets.py
python -m trading_bot.cli replay data/example.csv --db fresh-paper.db --buy-quantity 0.5
```

Use a fresh paper database. Generated result/database files are not checkpoint inputs.
CI adds the Phase 4 gate to the existing Linux/Windows, Python 3.11/3.12 matrix.
This gate ran locally on Linux; native Windows Phase 4 and remote CI were not run.
No external Coinbase connectivity is necessary or claimed for offline Phase 4.

## Remaining limitations and stopping boundary

No unresolved failures remain in this local gate. Model limitations are explicit:
constant full fills at opens (including zero-volume bars), no liquidity/participation
or intrabar drawdown model, no exchange tick/lot/minimum-size rules, no stop orders,
no funding/taxes/impact model, and no engine restart-state persistence. Unaffordable
entries fail rather than resizing or silently skipping. Current-candle complete volume
cannot decide fills at that same open because it would introduce look-ahead. Actual REST
confirmation latency is not represented; historical availability is bucket end.
Annualized Sharpe/Sortino on this short fixture are descriptive test values only.
Trusted custom Python strategies must honor the deterministic interface; it is not a
security sandbox for arbitrary code. Credential scanning detects known patterns, not
all conceivable secret encodings. No credentials were supplied or introduced.

GitHub was not modified or pushed. No merge to main, AI decisions, order credentials,
live execution, autonomous strategy selection or Phase 5 functionality was added.
Stopped after the Phase 4 local verification gate.
