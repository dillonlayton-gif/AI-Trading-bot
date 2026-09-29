# Guarded paper trader

Python 3.11+ offline paper trading prototype. It cannot submit Coinbase orders and contains no exchange credentials. All intents reach `PaperBroker.submit`, which calls the deterministic `RiskEngine` before any ledger fill. AI output, if added later, must be converted to an `Intent` and use this boundary. This is an engineering prototype, not a profitable strategy or an authorization to trade real funds.

## Run

```bash
python -m unittest discover -s tests -v
python -m trading_bot.cli replay data/example.csv --db paper.db --buy-quantity 0.5
python -m trading_bot.cli stop --db paper.db
```

CSV columns: `time` (timezone-aware ISO 8601), `price` (positive decimal). Replay is single-product, sequential, durable SQLite. Reuse of a ledger refuses duplicate or earlier timestamps. Paper fills apply 0.2% adverse slippage and 0.6% fee. Default risk limits: $100 per order, 20% position/equity, 3% UTC-day mark-to-market loss, no shorting or leverage. Stop persists. Risk limits are fixed in code for this phase. Replay does not place automatic strategy orders; `--buy-quantity` submits one sample order through risk for demonstration. Logs include marks, rejects, fills and fees in SQLite `events` and standard logging.

## Sequential gates

1. **Core and paper ledger:** implemented; unit tests and replay command verified locally.
2. **Market data:** add Coinbase Advanced Trade public market data adapter with bounded retries, product increments, timestamps, gap and staleness checks; record raw data for deterministic replay. No authenticated trading key.
3. **Strategy and evaluation:** specify rules before testing, replay unseen periods, account for spread/fees/slippage, compare buy-and-hold, check drawdown and robustness. AI may suggest intents but cannot modify risk limits or ledger.
4. **Operations:** restart/reconciliation, durable idempotent order IDs, alerting, circuit breakers for stale feeds and API failures, metrics, backup and recovery drills.
5. **Paper validation:** run forward for weeks with complete logs, reconcile every fill, confirm loss limits, stop behavior and failure recovery. Review results before considering any live capability.

A Coinbase live broker is deliberately absent. Adding one requires a separate reviewed implementation and explicit decision after the paper gates. Coinbase documents the Advanced Trade Python SDK and REST/WebSocket APIs: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sdk
