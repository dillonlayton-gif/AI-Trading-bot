"""Public Coinbase data to durable risk-gated paper sessions; no order transport."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import signal

from trading_bot.indicators import FeatureConfig
from trading_bot.market_data.feed import FeedProcessor
from trading_bot.market_data.live import LiveCollector
from trading_bot.market_data.models import DataError, timestamp, GRANULARITIES
from trading_bot.market_data.store import MarketStore
from .runner import ForwardConfig, ForwardRunner, aggregate

LOG = logging.getLogger(__name__)


async def forward(runner, processor, *, duration=None, collector=None, clock=None, poll=1.0, stop=None):
    """Stop ingestion, drain bounded REST repair, then summarize/reconcile.

    SQLite work stays on the event loop; the inherited collector owns bounded
    reconnect/backoff and public REST confirmation. Mock dependencies are used
    in tests, with the same orchestration path as the live command.
    """
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise DataError('positive finite duration required')
    if not math.isfinite(poll) or poll <= 0:
        raise DataError('positive finite poll interval required')
    clock = clock or (lambda: datetime.now(timezone.utc))
    stop = stop or asyncio.Event()
    collector = collector or LiveCollector(processor)
    loop = asyncio.get_running_loop()
    installed, timer, task = [], None, None
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try: loop.add_signal_handler(sig, stop.set)
            except NotImplementedError: continue
            installed.append(sig)
        if duration is not None: timer = loop.call_later(duration, stop.set)
        # A crash can leave an active logical session. Close that session using
        # its latest durable clock before opening a new one; positions persist.
        if runner.active: runner.stop(clock())
        runner.start(clock())
        task = asyncio.create_task(collector.run(stop))
        while not stop.is_set():
            if task.done():
                task.result()
                if not stop.is_set(): raise DataError('collector ended unexpectedly')
                break
            now = clock()
            health = processor.health(now)
            runner.set_health(health['status'], now)
            # Only process confirmed bars after repair is complete. Rejected
            # health always clears any pending trading intent.
            if health['status'] == 'healthy':
                rows = processor.store.candles(runner.config.product, 300, finalized=True)
                for c in aggregate(rows, runner.config.seconds):
                    if c.epoch in runner.candles: continue
                    # Catch-up can span many bars. Yield between operations so
                    # heartbeat/recovery tasks run, then recertify actual health.
                    await asyncio.sleep(0)
                    if stop.is_set(): break
                    now = clock()
                    health = processor.health(now)
                    runner.set_health(health['status'], now)
                    if health['status'] != 'healthy': break
                    observed = []
                    for start in range(c.epoch, c.end_epoch, 300):
                        row = processor.store.get(c.product, 300, start)
                        observed.append(timestamp(row['observed_at']))
                    runner.consume(c, as_of=now, received_at=max(observed))
                    if runner.fault: raise DataError('risk accounting continuity fault; stopped closed')
            LOG.info('paper_health %s', json.dumps({'run_id': runner.config.run_id, 'as_of': now.isoformat(),
                     'feed': health, 'risk': runner.ledger.snapshot()}, sort_keys=True))
            try: await asyncio.wait_for(stop.wait(), timeout=poll)
            except asyncio.TimeoutError: pass
    finally:
        stop.set()
        if task is not None:
            # Drain the collector's outstanding REST page before database close.
            try: await task
            finally:
                if runner.active and not runner.failed: runner.stop(clock())
        elif runner.active and not runner.failed:
            runner.stop(clock())
        if timer is not None: timer.cancel()
        for sig in installed: loop.remove_signal_handler(sig)
    summary = runner.summary()
    summary['feed_health'] = processor.health(clock())
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--run-id', required=True, help='stable ID reused only with the same state directory/config')
    parser.add_argument('--strategy', choices=('sma_trend', 'macd_trend'), required=True)
    parser.add_argument('--product', default='BTC-USD')
    parser.add_argument('--seconds', type=int, choices=sorted(x for x in GRANULARITIES if x >= 300), default=300)
    parser.add_argument('--quantity', type=Decimal, default=Decimal('0.0005'))
    parser.add_argument('--sma-period', type=int, default=20)
    parser.add_argument('--duration', type=float, help='seconds; omit for continuous collection')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    runner, store, handler = None, None, None
    try:
        config = ForwardConfig(args.run_id, args.strategy, product=args.product, seconds=args.seconds,
                               quantity=args.quantity, features=FeatureConfig(sma=args.sma_period))
        runner = ForwardRunner(args.state_dir, config)
        handler = RotatingFileHandler(args.state_dir/'operations.log', maxBytes=10_000_000, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        logging.getLogger().addHandler(handler)
        store = MarketStore(args.state_dir/'market.db')
        summary = asyncio.run(forward(runner, FeedProcessor(store, config.product), duration=args.duration))
        raw = json.dumps(summary, sort_keys=True, indent=2)
        (args.state_dir/'summary.json').write_text(raw+'\n', encoding='utf-8')
        print(raw)
        return 0
    except KeyboardInterrupt:
        LOG.info('paper_interrupted; durable state will reconcile on restart')
        return 130
    except (DataError, OSError) as exc:
        LOG.error('paper_failed_closed %s', exc)
        return 1
    finally:
        if runner is not None: runner.close()
        if store is not None: store.close()
        if handler is not None:
            logging.getLogger().removeHandler(handler); handler.close()


if __name__ == '__main__':
    raise SystemExit(main())
