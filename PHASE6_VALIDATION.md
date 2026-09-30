# Phase 6 validation gate

Validated 2026-09-30 UTC on Linux/Python 3.12. Branch `phase-6-forward-paper`.
Base: published Phase 5 `6a8a396c661de67de4f8f8e61116014e7c734578`, verified equal across
tracked files to local `fb507700e5f04241aacb4dadace7f2152cba9c74` before branching.
Checkpoint: the commit containing this report (`git rev-parse HEAD`).

## Scope and contracts

Only sustained public-data paper operation is added. Fixed explicit SMA/MACD baseline
selection, product, selected timeframe, quantity, features and risk limits. No AI,
credentials, authenticated Coinbase client, exchange orders, live broker, strategy
optimization/mutation, main merge or Phase 7 functionality. GitHub was read for the
base verification; no remote write, push, branch creation or merge was performed.

The existing public collector handles subscriptions, accepted heartbeat/candle data,
validation, disconnects, capped interruptible reconnect/backoff, asynchronous bounded
REST repair and drain. Provisional WebSocket prices never reach the strategy. Complete
REST-confirmed five-minute bars can aggregate to the supported larger UTC timeframes;
gaps are errors and incomplete groups remain unavailable. Phase 3 features and Phase 4
stateless baselines are reused without modification. Signals are causal and immutable.

Execution is explicitly a latency-aware **prior signal at next finalized close** paper
model, not a historical next-open fill. Every submitted intent reaches only the Phase 5
DurablePaperLedger. Existing 0.6% fee/0.2% adverse slippage, notional/exposure/cash/count,
stale/feed, kill, daily-loss and drawdown controls remain authoritative and immutable.
No raw fill/account setter is added. Freshness is additionally bounded by 90 seconds
from finalization and receipt for creating forward orders. Unhealthy feeds pause
processing. Stale/disconnected status clears pending signals; degraded confirmation
retains a pending signal while prohibiting fills until fresh healthy data returns.
Routine repair therefore cannot turn into stale signal execution. Prior finalized
results are never revised; duplicates are no-ops and conflicts/missing bars fail closed.

## Durable sessions, handoff and reconciliation

OS-held cross-platform single-writer lease. SQLite WAL/FULL and atomic transactions
protect the forward journal; config/event update/delete triggers, canonical JSON and
SHA-256 sequence links are checked at startup and every operation. A prepared operation
has a hash tied to the prior journal head and risk event count/head. The Phase 5 ledger
continues to reconstruct and verify every decision/fill against its own projection.
The session records causal feature rows, signals, paper lifecycle, complete accounting
snapshots and authoritative risk anchors. Features/pending signal/session state rebuild
from the completed journal, preserving previously finalized results.

Restart adopts exactly one matching committed risk event without resubmitting it. Zero
or one matching event is permitted; extra/mismatched/unrelated events fail closed. A
prepared but uncommitted intent prevents **live** restart, because feed health has not
been re-established. Explicit offline replay recovery is tested separately and still
uses the gate. No CLI flag permits delayed historical paper fills during live startup.
No guessing, silently deleting pending rows or resetting losses/positions is provided.
Session startup/stop, operator kill changes, health transitions and candle processing
are durable. Lifecycle includes filled/rejected/no-order with intent IDs and fill costs.
Daily counts, equity movement and realized P&L, current/max drawdown, cash/positions,
unrealized/realized P&L and complete reconciliation are available in session summaries.
Logs rotate and feed health retains heartbeat/receipt ages, counts and reconnect state.

## Deterministic fixture

`data/forward_sample.json` and `scripts/validate_forward.py` cover normal finalized data,
disconnect/reconnect, stale pause, healthy historical catch-up, explicit kill on/off,
accepted/rejected paper operations, two logical sessions, restart with an open position,
full closure and final reconciliation. The reference, reopened run and completed-journal
replay produce byte-identical canonical output including all accounting and risk events.

Known results (default costs/limits, synthetic quantity 0.5, SMA period 2):

| Result | Expected |
| --- | --- |
| Sessions / finalized bars | 2 / 12 |
| Paper fills / risk rejections | 4 / 1 |
| Feed pauses | 2 |
| BUY closes | 120 and 110 |
| SELL closes | 90 and 100 |
| Final cash and equity | 9978.31976 |
| Realized P&L | -21.68024 |
| Positions / unrealized P&L | empty / 0 |
| Kill / daily / drawdown locks | false / false / false |

Pinned Phase 6 SHA-256:
`f4de9bc4359c96bc3bca55aa3d1069d38dbaf474aee937937feaca2944e6abc7`.

## Automated gate

`python -m unittest discover -s tests -v`: **253 passing**; 193 inherited, 60 new.
New coverage: warm-up/flat/falling series, prior-signal causality and immutable prefixes,
exact costs/accounting/exit P&L, strategy-to-gate routing, order limit, daily/drawdown
lockouts including open positions, stale/unhealthy/disconnected feeds, reconnect,
duplicate/conflicting deliveries, product/finalization/receipt/clock validation,
aggregation/partial groups/gaps, config immutability and explicit MACD, session/daily
summary calculations, lease exclusivity, corrupted risk/session/pending data and orphan
risk events. Crash injection covers after prepare, after risk commit and before journal
commit; committed rejects recover once; mismatched/extra events fail closed. An actual
child process exits after risk commit and recovers exactly one fill. Mock live-loop and
public WebSocket + REST confirmation pipeline tests exercise sustained accepted trading,
shutdown/drain, collector failure and Windows-unsupported signal registration/duration.

All inherited deterministic validation scripts pass unchanged:

| Replay | SHA-256 |
| --- | --- |
| Market data | `10f97aaa4635ff41c5ea5a644183d77b0ffee2057dc59e83fd90dfbe1c86250e` |
| Indicators | `a82bc6072b239f76d355d488881cbaffde6dd25188fbca96debfb2ff980b2062` |
| Backtest and benchmarks | `9fce5de70cf0156afb363e60f59a522170421776d4c013ac1016b9fb0dff09bf` |
| Phase 5 restart/reconciliation | `3b0af0752d2265c752125372a590a1883e3226fdbecd955eea1309d8bad8853b` |

Original paper CSV replay: cash 9949.5994000, units 0.5, stopped 0.
`python scripts/scan_secrets.py`: zero credential/private-key pattern matches in all
tracked project text. This is a pattern scan, not a claim that it detects every possible
secret format. Scope inspection confirms public read-only data calls and paper-only
ledger admission; all pre-existing Phase 1–5 implementation/test files are unchanged.

## Operational limits and Windows soak

The exact PowerShell install/test/24-hour forward-paper command is in README. Unsupported
signal handlers and module entry are tested here; native Windows Phase 6 execution and
a real sustained Coinbase soak are not claimed. Run that operator soak before relying
on laptop operational behavior. Cold-start warm-up is live-only and trades are not
forced. Public feed/REST availability and confirmation latency can pause/reject trading.
A long outage outside the accounting freshness window or missing bars can make recovery
impossible; the runner stops closed. Prepared uncommitted intents require offline
investigation. Kill/loss lockouts block exits too; they do not cap an open position's
future loss. No emergency liquidation is implemented.

Full-journal reconstruction and retained feature/event history suit modest paper
workloads; no compaction, high-throughput/multiwriter service, backup automation or
external audit signing is added. Logs rotate but journals require disk monitoring.
UTC clock regressions stop the run. One product/timeframe per immutable run. Daily
equity statistics are marked observations, not tick-level performance. File hashes and
private Python members are not a sandbox against arbitrary local code/filesystem
rewrites. No Phase 7 work is included; stop at this verified local checkpoint.
