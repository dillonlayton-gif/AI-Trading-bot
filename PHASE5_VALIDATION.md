# Phase 5 validation gate

Verified 2026-09-30 (America/Chicago). Branch: `phase-5-risk-recovery`.
Base: published Phase 4 `214ffc4aed20de224842f169b4981909b4429574`, rechecked against local
`23e20660b1be24e23d2651ad8940afaeefff7ad4` with no tracked-file differences.
Checkpoint: the commit containing this report (`git rev-parse HEAD`).

## Risk controls

The durable paper API in `trading_bot/recovery/ledger.py` extends the original fixed
risk/cost assumptions in a new strict persistence schema. Default limits: maximum
filled order notional 100; position and portfolio exposure each 20% of projected
post-cost equity; daily loss 3%; lifetime peak drawdown 10%; one open spot position;
market/receipt/held-mark age 3600 seconds; fee 0.6% and adverse slippage 0.2% each side.
Only typed explicit long-spot BUY/SELL intents enter the API. No raw fill/account setter,
leverage/shorts, AI, exchange client or execution credentials exist in the new layer.

All admitted intents reach the same deterministic risk reducer; replay uses that same
reducer to verify decisions and fills. ID conflicts/duplicates, kill state, invalid or
stale/unhealthy/provisional/future/gapped data, position count, cash/inventory, per-order,
per-position/portfolio and loss limits have explicit stable rejection reasons. Limit
configuration is frozen and persisted; changed limits on reopen fail closed. Exact
reason precedence and boundary definitions are in README. A risk-reducing SELL is also
blocked by kill/loss/feed lockouts; no emergency liquidation exception is implemented.

Daily loss remains latched until a valid new UTC day; equity peak drawdown remains
latched for the entire run. Projected cost losses can latch a breaker without filling.
Clearing a kill switch does not clear loss latches. All valid packet marks maintain
cash/positions, weighted-average cost basis, realized/unrealized P&L, marked equity,
day reference, lifetime peak, risk latches, kill reason and intent outcomes.

## Persistence and reconciliation

Fresh durable schema only: metadata, projection and append-only audit. Legacy paper
DBs are rejected without guessing or migration. SQLite WAL/FULL and BEGIN IMMEDIATE
commit request, decision/fill, audit link and projection in one transaction. Stable
intent IDs prevent repeat fills after a committed but lost response. Uncommitted writes
roll back, allowing the same ID to retry. Rejected IDs remain adjudicated; new decisions
require new IDs. Duplicate retries append audit rejections without changing accounting.

Startup and every write reconstruct from original cash and pinned limits, validate
SQLite/schema/immutable trigger definitions, audit sequence and SHA-256 links, recompute
all risk decisions/fills, and compare complete reconstructed state to the projection.
Missing/extra/malformed or conflicting state fails closed. No repair-by-guessing is
performed. Failed ledger instances remain closed. Reports serialize canonical state,
event count, head digest and zero pending operations. Reopening appends no event.

Existing Phase 1 broker/CLI remains a compatibility demonstration through its original
risk gate, not the new durable schema. Phase 4 is an offline research simulator with no
paper-order capability. No strategy-to-ledger automation or path around a paper risk
gate is added; strategy code receives no durable accounting capability. These distinctions
preserve old behavior without claiming legacy databases have the new guarantees.

## Synthetic restart fixture

`data/recovery_sample.json` provides finalized BTC-USD five-minute candles and explicit
paper operations. Default limits and initial cash 1000 are used. Accepted fills include
partial entries/exits and full closure. Rejections include order limit, kill switch,
unhealthy feed and duplicate intent ID. The main restart cut is after kill activation,
while a position is open; tests additionally restart at every possible fixture boundary.

| Final result | Expected |
| --- | --- |
| Cash / equity | 1003.83526 / 1003.83526 |
| Realized P&L | 3.83526 |
| Unrealized P&L / positions | 0 / empty |
| Kill / daily / drawdown latches | false / false / false |
| Audit events | 14, byte-identical to uninterrupted reference |
| Intent fingerprints/outcomes, price marks, basis, peak/day state | identical |
| Pending operations | 0 |

Restart/reconciliation SHA-256:
`3b0af0752d2265c752125372a590a1883e3226fdbecd955eea1309d8bad8853b`

## Checks passed

| Check | Result |
| --- | --- |
| Full inherited + new suite | 193 tests passed: 140 inherited + 53 new |
| Risk boundaries | Order, position, portfolio, position-count, cash/inventory and config/schema checks passed |
| Loss protection | Exact daily/drawdown boundaries, projected-cost breaches, mark-only latch, recovered-price lockout and UTC-day reset passed |
| Feed protection | Stale age boundary, stale receipt/other held product, unhealthy/future/provisional/invalid/gapped/duplicate data passed |
| Stop/restart | Kill persistence and explicit clear; clear cannot reset drawdown; backward controls rejected |
| Accounting | Open-position restart, weighted basis, partial sale, full closure, realized/unrealized P&L and original costs passed |
| Idempotency | Accepted/rejected IDs, changed-ID-payload conflict, semantic decimal equality and restart retry passed |
| Interrupted writes | Connection rollback, actual os._exit subprocess crash with uncommitted write, audit insertion rolled back on projection failure passed |
| Corruption | Incomplete audit/projection/schema, missing or replaced trigger, legacy DB and changed limits rejected/fail-closed |
| Reconciliation | Stable canonical report, unchanged startup events, every fixture restart cut, two serialized connections and immutable audit passed |
| Determinism | Fixed Decimal isolation, fresh subprocess validator, portable LF output and pinned hash passed |
| Inherited replays | Paper sample, market-data, indicator and Phase 4 backtest hashes all passed |
| Credential/private-key scan | 47 tracked text files; zero matches |
| Scope review | New recovery imports allowlisted; only original Limits imported from core; no AI/network/order client |

Paper sample remains cash=9949.5994000, units=0.5, stopped=0. Prior phase hashes:
- Market data: `10f97aaa4635ff41c5ea5a644183d77b0ffee2057dc59e83fd90dfbe1c86250e`
- Indicators: `a82bc6072b239f76d355d488881cbaffde6dd25188fbca96debfb2ff980b2062`
- Backtest: `9fce5de70cf0156afb363e60f59a522170421776d4c013ac1016b9fb0dff09bf`

## Reproduce

```bash
python -m pip install '.[market]'
python -m unittest discover -s tests -v
python scripts/validate_market_data.py
python scripts/validate_indicators.py
python scripts/validate_backtest.py
python scripts/validate_recovery.py --output recovery-result.json
python scripts/scan_secrets.py
python -m trading_bot.cli replay data/example.csv --db fresh-paper.db --buy-quantity 0.5
```

Use a new legacy paper DB for its sample; the recovery validator creates isolated fresh
DBs automatically. CI is configured to include recovery on Linux/Windows Python 3.11/3.12.
Validation here ran on Linux; native Windows Phase 5 and remote CI were not executed.

## Limitations and stopping boundary

No unresolved failures remain in the local gate. Health/finalization/time are caller
certifications; no authenticated live feed integration exists. Risk bounds exposure,
not stop-distance sizing. All lockouts freeze every fill, including exits; open positions
can lose further value. Same-candle mark-then-submit is rejected; combine a packet and
intent atomically. Reconciliation replays the entire audit before each mutation and is
suitable for modest paper workloads, not a high-throughput service. Coordinate one writer.
No journal compaction, backup automation, schema/limit migration, restore tool or external
audit signature is implemented. Triggers/hash chains detect inconsistent edits, not a
complete malicious DB/filesystem rewrite. Private Python members are not a security
sandbox for hostile plugins. Secrets scanning detects known patterns, not all encodings.

No AI decisions, credentials, Coinbase order client, live execution or Phase 6 code was
added. Phase 1–4 source/tests remain unchanged. GitHub was not modified/pushed; no merge
to main occurred. Stopped after Phase 5 local validation.
