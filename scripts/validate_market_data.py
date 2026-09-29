"""Offline Phase 2 smoke gate; fixtures are synthetic and no network is used."""
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_bot.market_data.feed import FeedProcessor
from trading_bot.market_data.models import timestamp
from trading_bot.market_data.replay import replay_journal
from trading_bot.market_data.rest import HistoricalClient
from trading_bot.market_data.store import MarketStore


def validate():
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'data/market_sample.json').read_text())
    now = timestamp(fixture['history_end'])
    with tempfile.TemporaryDirectory() as temp:
        source = MarketStore(Path(temp) / 'source.db')
        replay = MarketStore(Path(temp) / 'replay.db')
        try:
            HistoricalClient(transport=lambda *_: json.dumps(fixture['history']), clock=lambda:now).fetch(
                fixture['product'], timestamp(fixture['history_start']), now, store=source)
            processor = FeedProcessor(source, fixture['product'])
            processor.begin_session('synthetic-example', now)
            for message in fixture['frames']:
                processor.ingest(json.dumps(message), timestamp(message['timestamp']))
            processor.disconnected(now + timedelta(seconds=3), 'sample_complete', stopped=True)
            report = replay_journal(source, replay)
            digest = source.replay_digest(fixture['product'], 300)
            if digest != replay.replay_digest(fixture['product'], 300):
                raise RuntimeError('replay hash mismatch')
            if source.get_meta('feed:' + fixture['product']) != replay.get_meta('feed:' + fixture['product']):
                raise RuntimeError('replay health mismatch')
            print(json.dumps({**report, 'finalized_candles':len(list(source.candles(fixture['product'],300))),
                              'sha256':digest, 'network':False}, sort_keys=True))
        finally:
            source.close()
            replay.close()


if __name__ == '__main__':
    validate()
