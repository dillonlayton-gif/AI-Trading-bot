"""Public market-data commands; no trading or credential inputs."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path

from .feed import FeedPolicy, FeedProcessor, health_report
from .live import LiveCollector
from .models import DataError, GRANULARITIES, product_id, timestamp
from .replay import replay_journal
from .rest import HistoricalClient, TransportError
from .store import MarketStore


async def collect(processor, duration):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    timer = loop.call_later(duration, stop.set) if duration else None
    try:
        await LiveCollector(processor).run(stop)
    finally:
        if timer:
            timer.cancel()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('historical', 'live', 'health', 'replay', 'replay-journal'):
        p = commands.add_parser(command)
        p.add_argument('--db', type=Path, required=True)
        if command != 'replay-journal':
            p.add_argument('--product', type=product_id, default='BTC-USD')
        if command in ('historical', 'replay'):
            p.add_argument('--seconds', type=int, choices=GRANULARITIES, default=300)
        if command == 'historical':
            p.add_argument('--start', type=timestamp, required=True)
            p.add_argument('--end', type=timestamp, required=True)
        if command == 'live':
            p.add_argument('--duration', type=float, help='stop after this many seconds; omit for continuous ingestion')
        if command == 'replay-journal':
            p.add_argument('--output-db', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'live' and args.duration is not None and not 0 < args.duration <= 86400:
        parser.error('duration must be in (0,86400] seconds')
    if args.command == 'replay-journal' and args.output_db.exists():
        parser.error('output database must not already exist')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    store = MarketStore(args.db, readonly=args.command in ('health', 'replay', 'replay-journal'))
    try:
        if args.command == 'historical':
            candles = HistoricalClient().fetch(args.product, args.start, args.end, args.seconds, store=store)
            print(json.dumps({'candles': len(candles), 'sha256': store.replay_digest(args.product, args.seconds)}))
        elif args.command == 'live':
            asyncio.run(collect(FeedProcessor(store, args.product), args.duration))
        elif args.command == 'health':
            saved = store.get_meta('feed:' + args.product)
            if saved is None:
                print(json.dumps({'product': args.product, 'status': 'unavailable'}))
                return 1
            state = json.loads(saved)
            print(json.dumps(health_report(state, datetime.now(timezone.utc), FeedPolicy(**state.get('policy', {}))), sort_keys=True))
        elif args.command == 'replay':
            for line in store.replay_jsonl(args.product, args.seconds):
                print(line, end='')
        elif args.command == 'replay-journal':
            destination = MarketStore(args.output_db)
            try:
                print(json.dumps(replay_journal(store, destination), sort_keys=True))
            finally:
                destination.close()
    except (DataError, TransportError) as exc:
        logging.error('market_command_failed error=%s', str(exc))
        return 1
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
