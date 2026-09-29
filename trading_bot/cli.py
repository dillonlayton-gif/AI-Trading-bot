"""Offline paper replay. Intents are explicit; no AI or network order submission."""
import argparse
import logging
from decimal import Decimal
from pathlib import Path
from .core import Intent, Limits, PaperBroker, PaperLedger, PaperRunner, RiskEngine, amount, read_candles


def main():
    parser = argparse.ArgumentParser(description="Offline, paper-only crypto trader")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("csv", type=Path)
    replay.add_argument("--db", type=Path, required=True)
    replay.add_argument("--product", default="BTC-USD")
    replay.add_argument("--initial-cash", default="10000")
    replay.add_argument("--buy-quantity", default="0", help="Optional fixed quantity on first candle only")
    stop = sub.add_parser("stop")
    stop.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "stop":
        if not args.db.exists():
            parser.error("ledger does not exist")
        ledger = PaperLedger(args.db, Decimal(1))
        with ledger.db:
            ledger.set("stopped", "1")
        ledger.close()
        return
    if args.db.exists() and not args.db.is_file():
        parser.error("db path must be a file")
    ledger = PaperLedger(args.db, amount(args.initial_cash))
    try:
        runner = PaperRunner(ledger, PaperBroker(ledger, RiskEngine(Limits())))
        quantity = amount(args.buy_quantity)
        if quantity < 0:
            parser.error("buy quantity must be nonnegative")
        for index, candle in enumerate(read_candles(args.csv, args.product)):
            intent = Intent("BUY", quantity, args.product) if index == 0 and quantity else None
            runner.step(candle, intent)
        print(f"PAPER cash={ledger.get('cash')} units={ledger.get('units')} stopped={ledger.get('stopped')}")
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
