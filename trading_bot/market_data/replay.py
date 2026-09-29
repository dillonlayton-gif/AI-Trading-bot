"""Replay a public-data journal with recorded receipt times; no network calls."""
from __future__ import annotations

import json
from datetime import datetime
from .feed import FeedPolicy, FeedProcessor
from .models import DataError, MarketCandle, timestamp
from .rest import HistoricalClient
from .store import MarketStore


def replay_journal(source: MarketStore, destination: MarketStore) -> dict:
    if destination.db.execute("SELECT COUNT(*) FROM md_journal").fetchone()[0] or destination.db.execute(
            "SELECT COUNT(*) FROM md_candles").fetchone()[0]:
        raise ValueError("journal replay requires an empty destination")
    processors = {}
    sessions = {}
    count = 0
    for record in source.journal_records():
        now, raw = timestamp(record['received_at']), record['raw']
        kind, expected = record['source'], json.loads(record['outcome'])
        if kind == 'control':
            event = json.loads(raw)
            product = event['product']
            if event['kind'] == 'connect':
                processor = processors.setdefault(product, FeedProcessor(destination, product,
                    policy=FeedPolicy(**event['policy'])))
                processor.begin_session(record['session'], now)
                sessions[record['session']] = processor
            else:
                processor = processors[product]
                if event['kind'] == 'disconnect':
                    processor.disconnected(now, event['error'], stopped=event['stopped'])
                elif event['kind'] in ('backfill_complete', 'backfill_progress'):
                    completed = processor.backfill_complete(now)
                    if completed != (event['kind'] == 'backfill_complete'):
                        raise DataError('replay backfill diverged')
                else:
                    raise DataError('unknown control record')
        elif kind == 'ws':
            processor = sessions[record['session']]
            if expected.get('reason') in ('non_text_frame', 'message_size'):
                processor._record(raw, now, expected)
            else:
                processor.ingest(raw, now)
        elif kind == 'rest':
            client = HistoricalClient(transport=lambda *_: raw, clock=lambda: now, page_limit=expected['limit'])
            try:
                client.fetch(expected['product'], datetime.fromtimestamp(expected['start'], now.tzinfo),
                    datetime.fromtimestamp(expected['end'], now.tzinfo), expected['seconds'], store=destination)
            except DataError:
                if expected.get('status') != 'invalid' and 'conflict' not in expected.get('statuses', []):
                    raise
        elif kind == 'recovery':
            processor = sessions[record['session']]
            with destination.transaction():
                statuses = [destination.put(MarketCandle.from_dict(c), now, finalized=True, source='rest')
                            for c in json.loads(raw)['candles']]
                destination.journal(now, kind, record['session'], raw, {'kind': 'backfill', 'statuses': statuses})
                if 'conflict' in statuses:
                    processor.state['integrity_error'] = True
                    processor._save()
        elif kind == 'recovery_failure':
            processor = sessions[record['session']]
            with destination.transaction():
                processor.state['last_error'] = expected['error']
                destination.journal(now, kind, record['session'], raw, expected)
                processor._save()
        else:
            raise DataError('unknown journal source')
        latest = destination.db.execute('SELECT outcome FROM md_journal ORDER BY id DESC LIMIT 1').fetchone()
        if latest is None or json.loads(latest[0]) != expected:
            raise DataError(f"journal outcome diverged at record {record['id']}")
        count += 1
    original = [(r['product'], r['seconds'], r['start'], r['payload'], r['finalized']) for r in
                source.db.execute('SELECT * FROM md_candles ORDER BY product,seconds,start')]
    result = [(r['product'], r['seconds'], r['start'], r['payload'], r['finalized']) for r in
              destination.db.execute('SELECT * FROM md_candles ORDER BY product,seconds,start')]
    if result != original:
        raise DataError('replay archive differs from recording')
    source_health = list(source.db.execute("SELECT key,value FROM md_meta WHERE key LIKE 'feed:%' ORDER BY key"))
    replay_health = list(destination.db.execute("SELECT key,value FROM md_meta WHERE key LIKE 'feed:%' ORDER BY key"))
    if [tuple(row) for row in source_health] != [tuple(row) for row in replay_health]:
        raise DataError('replay feed state differs from recording')
    return {'records': count, 'candles': len(result), 'matched': True}
