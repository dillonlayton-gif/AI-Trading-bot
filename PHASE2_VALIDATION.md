# Phase 2 market-data checkpoint

Date: 2026-09-29 UTC. Runtime: Python 3.12, websockets 16.0.
Base: published `phase-2-market-data` commit `fc357af375176c3959097f71de6796ddfdf4fd86`. That published copy had market-data files at the root initializer and paper CLI paths. This checkpoint repairs those package boundaries from verified checkpoint `9dd2389`, preserving its Windows signal-handler fix.
Scope: public market data only. No strategy, AI provider, order credentials, live broker or order submission.

## Results

| Check | Result |
| --- | --- |
| Historical/normalization/persistence stage | 13 tests passed before live implementation |
| Final automated suite | 53 tests passed: original 42 plus 7 signal portability/cleanup/drain tests plus 4 package-entry subprocess tests |
| Real WebSocket transport | Passed against local async WebSocket server |
| Concurrent REST recovery | Passed; live ingestion continues during backfill |
| Failure recovery and replay | Passed, including partial pages, conflicts, rejected payloads and failed backfill |
| Original paper CSV replay | Passed; cash 9949.5994000, units 0.5, stopped 0 |
| Synthetic market-data replay | Passed; 7 journal records, 3 canonical rows, 2 finalized candles; identical archive and feed state |
| Replay SHA-256 | `10f97aaa4635ff41c5ea5a644183d77b0ffee2057dc59e83fd90dfbe1c86250e` |
| Python wheel build and installed entry paths | Passed for version 0.2.0; both installed CLIs start outside the source tree, installed paper replay passes |
| Foundation boundary | Root paper CLI restored byte-for-byte from verified `9dd2389`; core, original tests and original CSV unchanged |
| Credential scan | No provider-token, credential-assignment or private-key pattern matches in tracked project text |
| External Coinbase historical smoke | Blocked: environment returned an HTML “Site Unavailable / Unable to access this site” page; adapter rejected it |
| Published packaging bug reproduction | Confirmed in `fc357af`: market-data CLI fails with missing `trading_bot.models` |
| Public package-entry regression | Passed: fresh-process root/market imports, market CLI help/replay and Phase 1 paper CLI replay |
| Windows unsupported-handler regression | Passed with simulated `NotImplementedError`; duration shutdown and real collector repair drain verified |
| Native Windows execution | Not run in this Linux environment; CI configured for Windows and Linux on Python 3.11/3.12 |
| Package-entry patch publication/CI | Local checkpoint only; no GitHub changes or remote CI run |

Local functional validation passes. **The complete Phase 2 checkpoint gate remains pending** until external public Coinbase REST/WebSocket connectivity is verified in the deployment environment. Native Windows execution of this package-entry patch is also pending. The local WebSocket test validates framing, subscription, normalization, persistence and clean shutdown, but does not prove Coinbase reachability or authorize real trading. No Phase 3 work was started.

The isolated local branch is named `phase-2-market-data` and is based on the actual published foundation history. Its committed patch can be applied to that foundation or to an existing Phase 2 checkout after confirming the branch base. The original workspace branch, GitHub `main`, and GitHub `paper-foundation` remain untouched.

## Reproduce

```bash
python -m pip install '.[market]'
python -m unittest discover -s tests -v
python scripts/validate_market_data.py
python scripts/scan_secrets.py
python -m trading_bot.cli replay data/example.csv --db /tmp/fresh-paper.db --buy-quantity 0.5
```

Use a fresh paper database path for each sample regression run. Tests use temporary databases and synthetic messages; the WebSocket transport test binds only to localhost. In this restricted execution environment, asyncio socket/thread wakeups required permission outside the sandbox; with permission, the full suite completed in approximately 1.7 seconds. Unsupported signal registration is caught per signal, only installed handlers are removed, and duration timers are canceled on success or failure. The collector is still awaited through its existing stop and in-flight repair drain.

The scanner reports paths/categories only, never values. It checks known credential/private-key formats and literal credential assignments; a pattern scan cannot prove the absence of every possible secret encoding. Only tracked source, tests, fixtures, documentation and configuration are included in the checkpoint. Generated databases, build output, environment files and credentials are excluded.

The public-data collector is single-writer with explicit freshness thresholds, capped retry/reconnect delays, bounded recovery pages and immutable finalized candles. Continuous forward-data soak, retention, backup/recovery drills and operational deployment validation remain later work. See README for commands and data semantics.
