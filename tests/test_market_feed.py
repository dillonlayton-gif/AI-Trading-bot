import asyncio
import json
import unittest
from datetime import timedelta
from pathlib import Path
from test_market_history import T, row, candle
import test_market_history as history
from trading_bot.market_data.feed import FeedProcessor
from trading_bot.market_data.live import LiveCollector, WS_URL
from trading_bot.market_data.models import DataError
from trading_bot.market_data.replay import replay_journal
from trading_bot.market_data.rest import HistoricalClient, RetryPolicy
from trading_bot.market_data.store import MarketStore


def frame(channel='candles', seq=0, sec=0, rows=None, counter=None, event='update'):
    events = [{'type': event, 'candles': rows if rows is not None else [row()]}] if channel == 'candles' else [
        {'current_time': (T + timedelta(seconds=sec)).isoformat(), 'heartbeat_counter': seq if counter is None else counter}]
    return json.dumps({'channel': channel, 'sequence_num': seq,
                      'timestamp': (T + timedelta(seconds=sec)).isoformat(), 'events': events})


class FeedTests(unittest.TestCase):
    setUp, tearDown = history.StoreTests.setUp, history.StoreTests.tearDown
    def feed(self):
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('session-1', T)
        return feed

    def test_fresh_feed_and_read_time_staleness(self):
        feed = self.feed()
        self.assertEqual(feed.health(T)['status'], 'warming')
        feed.ingest(frame('heartbeats'), T)
        feed.ingest(frame(), T)
        self.assertEqual(feed.health(T)['status'], 'healthy')
        self.assertEqual(feed.health(T + timedelta(seconds=16))['status'], 'stale')
        self.assertEqual(list(self.store.candles('BTC-USD', 300)), [])
        self.assertEqual(len(list(self.store.candles('BTC-USD', 300, finalized=False))), 1)

    def test_duplicate_out_of_order_stale_and_future_frames(self):
        feed = self.feed()
        feed.ingest(frame(seq=2), T)
        for raw, received, expected in ((frame(seq=2), T, 'duplicate'), (frame(seq=1), T, 'out_of_order'),
                (frame(seq=3), T + timedelta(seconds=16), 'stale'), (frame(seq=3, sec=10), T, 'future')):
            self.assertEqual(feed.ingest(raw, received)['status'], expected)
        self.assertEqual(feed.sequences['candles'], 2)
        self.assertEqual(feed.state['last_candle'], T.isoformat())
        self.assertEqual(len(list(self.store.journal_records())), 6)

    def test_revision_conflict_latches_health(self):
        feed = self.feed()
        feed.ingest(frame('heartbeats'), T)
        feed.ingest(frame(), T)
        good = feed.ingest(frame(seq=1, sec=1, rows=[row(close='102', volume='2')]), T + timedelta(seconds=1))
        self.assertEqual(good['candles'], ['revision'])
        bad = feed.ingest(frame(seq=2, sec=2, rows=[row(volume='1')]), T + timedelta(seconds=2))
        self.assertEqual(bad['candles'], ['conflict'])
        self.assertEqual(feed.health(T + timedelta(seconds=2))['status'], 'degraded')
        self.assertEqual(next(self.store.candles('BTC-USD', 300, finalized=False)).volume, 2)

    def test_sequences_reset_by_session_and_channel(self):
        feed = self.feed()
        feed.ingest(frame(seq=99), T)
        self.assertEqual(feed.ingest(frame('heartbeats'), T)['status'], 'accepted')
        feed.disconnected(T, 'network')
        feed.begin_session('session-2', T)
        self.assertEqual(feed.ingest(frame(), T)['status'], 'accepted')
        self.assertEqual(feed.state['reconnects'], 1)
        self.assertIsNotNone(feed.state['repair'])

    def test_gaps_require_complete_backfill(self):
        feed = self.feed()
        feed.ingest(frame(), T)
        self.assertEqual(feed.ingest(frame(seq=2, sec=600, rows=[row(600)]), T + timedelta(seconds=600))['status'], 'sequence_gap')
        self.assertEqual(feed.state['counts']['sequence_gap'], 1)
        self.assertEqual(feed.state['repair'], {'start': int(T.timestamp()), 'end': int(T.timestamp()) + 600})
        self.assertFalse(feed.backfill_complete(T + timedelta(seconds=600)))
        self.store.put(candle(), T, finalized=True, source='rest')
        self.assertFalse(feed.backfill_complete(T + timedelta(seconds=600)))
        self.store.put(candle(300), T, finalized=True, source='rest')
        self.assertTrue(feed.backfill_complete(T + timedelta(seconds=600)))
        feed.ingest(frame('heartbeats', sec=600, counter=1), T + timedelta(seconds=600))
        self.assertEqual(feed.ingest(frame('heartbeats', seq=1, sec=601, counter=3), T + timedelta(seconds=601))['status'], 'sequence_gap')

    def test_out_of_order_bucket_never_replaces_newer_data(self):
        feed = self.feed()
        feed.ingest(frame(sec=300, rows=[row(300)]), T + timedelta(seconds=300))
        outcome = feed.ingest(frame(seq=1, sec=301), T + timedelta(seconds=301))
        self.assertEqual(outcome['candles'], ['out_of_order'])
        self.assertIsNone(self.store.get('BTC-USD', 300, int(T.timestamp())))
        self.assertEqual(feed.state['last_candle'], (T + timedelta(seconds=300)).isoformat())

    def test_snapshot_history_provisional_and_product_mismatch_rejected(self):
        feed = self.feed()
        feed.ingest(frame(sec=600, rows=[row(600), row(0), row(300)], event='snapshot'), T + timedelta(seconds=600))
        self.assertEqual(len(list(self.store.candles('BTC-USD', 300, finalized=False))), 3)
        self.assertEqual(list(self.store.candles('BTC-USD', 300)), [])
        self.assertIsNotNone(feed.state['repair'])
        self.assertEqual(feed.ingest(frame(seq=1, sec=601, rows=[row(600, product_id='ETH-USD')]), T + timedelta(seconds=601))['status'], 'invalid')

    def test_invalid_frames_recorded_without_freshness(self):
        feed = self.feed()
        for raw in ('{', '[]', frame(rows=[]), frame(rows=[row(close='NaN')]), frame('heartbeats', counter=-1),
                    json.dumps({'channel':'candles'}), b'binary', 'x' * 1_048_577):
            self.assertEqual(feed.ingest(raw, T)['status'], 'invalid')
        self.assertIsNone(feed.state['last_candle'])
        self.assertEqual(feed.ingest('{}', T)['status'], 'ignored')
        self.assertEqual(feed.ingest('{"type":"error"}', T)['status'], 'server_error')

    def test_restart_marks_repair(self):
        feed = self.feed()
        feed.ingest(frame(), T)
        restarted = FeedProcessor(self.store, 'BTC-USD')
        restarted.begin_session('restart', T + timedelta(seconds=600))
        self.assertEqual(restarted.state['reconnects'], 1)
        self.assertEqual(restarted.state['repair']['start'], int(T.timestamp()))
        self.assertEqual(restarted.health(T + timedelta(seconds=600))['status'], 'warming')

    def test_journal_replay_matches_outcomes_archive_and_health(self):
        HistoricalClient(transport=lambda *_: json.dumps({'candles':[row(-300)]}), clock=lambda:T).fetch(
            'BTC-USD', T - timedelta(seconds=300), T, store=self.store)
        feed = self.feed()
        for raw in (frame('heartbeats'), frame(), frame(), 'malformed'):
            feed.ingest(raw, T)
        feed.disconnected(T + timedelta(seconds=1), 'network')
        feed.begin_session('session-2', T + timedelta(seconds=2))
        feed.ingest(frame('heartbeats', sec=2), T + timedelta(seconds=2))
        feed.ingest(frame(sec=2), T + timedelta(seconds=2))
        feed.disconnected(T + timedelta(seconds=3), 'collector_stopped', stopped=True)
        destination = MarketStore(Path(self.temp.name) / 'replay.db')
        try:
            self.assertTrue(replay_journal(self.store, destination)['matched'])
            self.assertEqual(destination.get_meta('feed:BTC-USD'), self.store.get_meta('feed:BTC-USD'))
            self.assertEqual(destination.replay_digest('BTC-USD', 300), self.store.replay_digest('BTC-USD', 300))
            with self.assertRaises(ValueError):
                replay_journal(self.store, destination)
        finally:
            destination.close()

    def test_rejected_rest_payload_journaled_and_replayable(self):
        with self.assertRaises(DataError):
            HistoricalClient(transport=lambda *_: 'invalid', clock=lambda:T + timedelta(seconds=300)).fetch(
                'BTC-USD', T, T + timedelta(seconds=300), store=self.store)
        destination = MarketStore(Path(self.temp.name) / 'replay.db')
        try:
            self.assertTrue(replay_journal(self.store, destination)['matched'])
        finally:
            destination.close()


class FakeSocket:
    def __init__(self, frames, stop=None):
        self.frames, self.sent, self.stop = list(frames), [], stop
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def send(self, raw):
        self.sent.append(json.loads(raw))
    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        if self.stop:
            self.stop.set()
            return frame('heartbeats', seq=1)
        raise OSError('connection lost')


class LiveTests(unittest.IsolatedAsyncioTestCase):
    setUp, tearDown = history.StoreTests.setUp, history.StoreTests.tearDown
    async def test_reconnect_resubscribes_with_capped_backoff_without_auth(self):
        stop = asyncio.Event()
        sockets = [FakeSocket([]), FakeSocket([]), FakeSocket([]), FakeSocket([], stop)]
        urls, delays = [], []
        def connector(url):
            urls.append(url)
            return sockets[len(urls)-1]
        async def wait(event, delay):
            delays.append(delay)
        feed = FeedProcessor(self.store, 'BTC-USD')
        await LiveCollector(feed, connector=connector, clock=lambda:T, wait=wait,
                            policy=RetryPolicy(base=1, cap=2)).run(stop, max_connections=4)
        self.assertEqual(delays, [1,2,2])
        self.assertEqual(urls, [WS_URL]*4)
        for socket in sockets:
            self.assertEqual(socket.sent, [{'type':'subscribe','channel':'heartbeats'},
                {'type':'subscribe','channel':'candles','product_ids':['BTC-USD']}])
        self.assertEqual(feed.state['reconnects'], 3)
        self.assertEqual(feed.state['connection'], 'stopped')

    async def test_stop_interrupts_backoff(self):
        stop = asyncio.Event()
        feed = FeedProcessor(self.store, 'BTC-USD')
        def connector(_):
            raise OSError('offline')
        task = asyncio.create_task(LiveCollector(feed, connector=connector, clock=lambda:T,
                                  policy=RetryPolicy(base=30, cap=30)).run(stop))
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(feed.state['connection'], 'stopped')

    async def test_stale_and_invalid_frames_force_reconnect(self):
        for frames in (['invalid']*3, [frame()]*2, ['{"type":"error"}']):
            feed = FeedProcessor(self.store, 'BTC-USD')
            ticks = iter([0,31,32,33,34,35])
            await LiveCollector(feed, connector=lambda _:FakeSocket(frames), clock=lambda:T + timedelta(seconds=40),
                monotonic=lambda:next(ticks)).run(asyncio.Event(), max_connections=1)
            errors = [json.loads(r['raw'])['error'] for r in self.store.journal_records()
                      if r['source']=='control' and '"error"' in r['raw']]
            self.assertIn('FeedFailure', errors)

    async def test_rest_recovery_finalizes_and_replays(self):
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('session', T)
        feed.ingest(frame(), T)
        feed.ingest(frame(seq=2, sec=600, rows=[row(600)]), T + timedelta(seconds=600))
        client = HistoricalClient(transport=lambda *_:json.dumps({'candles':[row(),row(300)]}), clock=lambda:T + timedelta(seconds=600))
        await LiveCollector(feed, historical=client, clock=lambda:T + timedelta(seconds=600)).repair()
        self.assertIsNone(feed.state['repair'])
        self.assertEqual(len(list(self.store.candles('BTC-USD', 300))), 2)
        destination = MarketStore(Path(self.temp.name) / 'replay.db')
        try:
            self.assertTrue(replay_journal(self.store, destination)['matched'])
        finally:
            destination.close()

    async def test_incomplete_recovery_keeps_gap(self):
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('session', T)
        feed.ingest(frame(), T)
        feed.ingest(frame(seq=2, sec=600, rows=[row(600)]), T + timedelta(seconds=600))
        client = HistoricalClient(transport=lambda *_:json.dumps({'candles':[row()]}), clock=lambda:T + timedelta(seconds=600))
        await LiveCollector(feed, historical=client, clock=lambda:T + timedelta(seconds=600)).repair()
        self.assertIsNotNone(feed.state['repair'])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    setUp, tearDown = history.StoreTests.setUp, history.StoreTests.tearDown
    async def test_real_websocket_transport_against_local_server(self):
        from websockets.asyncio.server import serve
        from websockets.asyncio.client import connect
        from datetime import datetime, timezone
        stop = asyncio.Event()
        received = []
        async def handler(socket):
            received.extend([json.loads(await socket.recv()), json.loads(await socket.recv())])
            now = datetime.now(timezone.utc)
            start = int(now.timestamp()) // 300 * 300
            await socket.send(json.dumps({'channel':'heartbeats','sequence_num':0,'timestamp':now.isoformat(),
                'events':[{'heartbeat_counter':1,'current_time':now.isoformat()}]}))
            await socket.send(json.dumps({'channel':'candles','sequence_num':0,'timestamp':now.isoformat(),
                'events':[{'type':'update','candles':[dict(row(), start=str(start), product_id='BTC-USD')]}]}))
            await asyncio.sleep(.1)
            stop.set()
            await socket.wait_closed()
        feed = FeedProcessor(self.store, 'BTC-USD')
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            await asyncio.wait_for(LiveCollector(feed, connector=lambda _:connect(f'ws://127.0.0.1:{port}')).run(stop), 5)
        self.assertEqual([m['channel'] for m in received], ['heartbeats','candles'])
        self.assertEqual(feed.state['counts']['accepted'], 2)
        self.assertEqual(feed.state['connection'], 'stopped')


class AdditionalTests(unittest.TestCase):
    setUp, tearDown = history.StoreTests.setUp, history.StoreTests.tearDown
    def test_normal_bucket_rollover_schedules_finalization(self):
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('s', T)
        feed.ingest(frame(), T)
        feed.ingest(frame(seq=1, sec=300, rows=[row(300)]), T + timedelta(seconds=300))
        self.assertEqual(feed.state['repair'], {'start':int(T.timestamp()),'end':int(T.timestamp())+300})
        self.assertNotIn('bucket_gap', feed.state['counts'])

    def test_omitted_invalid_frames_replay(self):
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('s', T)
        feed.ingest(b'binary', T)
        feed.ingest('x' * 1_048_577, T)
        destination = MarketStore(Path(self.temp.name) / 'replay.db')
        try:
            self.assertTrue(replay_journal(self.store, destination)['matched'])
            self.assertEqual(destination.get_meta('feed:BTC-USD'), self.store.get_meta('feed:BTC-USD'))
        finally:
            destination.close()

    def test_public_http_request_contains_no_authorization(self):
        from unittest.mock import patch
        from trading_bot.market_data.rest import public_get
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{"candles":[]}'
        with patch('trading_bot.market_data.rest.urlopen', return_value=Response()) as open_url:
            public_get('https://api.coinbase.com/example', 10)
            request = open_url.call_args.args[0]
            self.assertEqual(request.method, 'GET')
            self.assertEqual(set(k.lower() for k in request.headers), {'accept', 'user-agent'})


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp, tearDown = history.StoreTests.setUp, history.StoreTests.tearDown
    async def test_failed_backfill_is_persisted_and_replays(self):
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('s', T)
        feed.ingest(frame(), T)
        feed.ingest(frame(seq=2, sec=300, rows=[row(300)]), T + timedelta(seconds=300))
        await LiveCollector(feed, historical=HistoricalClient(transport=lambda *_:'bad', clock=lambda:T+timedelta(seconds=300)),
                            clock=lambda:T+timedelta(seconds=300)).repair()
        self.assertEqual(feed.state['last_error'], 'DataError')
        self.assertIsNotNone(feed.state['repair'])
        destination = MarketStore(Path(self.temp.name) / 'replay.db')
        try:
            self.assertTrue(replay_journal(self.store, destination)['matched'])
            self.assertEqual(destination.get_meta('feed:BTC-USD'), self.store.get_meta('feed:BTC-USD'))
        finally:
            destination.close()

    async def test_backfill_never_overwrites_finalized_archive(self):
        HistoricalClient(transport=lambda *_:json.dumps({'candles':[row()]}), clock=lambda:T+timedelta(seconds=300)).fetch(
            'BTC-USD', T, T+timedelta(seconds=300), store=self.store)
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('s', T+timedelta(seconds=300))
        await LiveCollector(feed, historical=HistoricalClient(transport=lambda *_:json.dumps({'candles':[row(close='102')]}),
            clock=lambda:T+timedelta(seconds=300)), clock=lambda:T+timedelta(seconds=300)).repair()
        self.assertTrue(feed.state['integrity_error'])
        self.assertEqual(next(self.store.candles('BTC-USD',300)).close, 101)
        destination = MarketStore(Path(self.temp.name) / 'replay.db')
        try:
            self.assertTrue(replay_journal(self.store, destination)['matched'])
        finally:
            destination.close()

    async def test_ingestion_continues_during_rest_backfill(self):
        import threading
        started, release = threading.Event(), threading.Event()
        stop = asyncio.Event()
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('seed', T)
        feed.ingest(frame(), T)
        def transport(*_):
            started.set()
            if not release.wait(2):
                raise RuntimeError('live ingestion paused during recovery')
            return json.dumps({'candles':[row()]})
        class ConcurrentSocket(FakeSocket):
            def __init__(self):
                super().__init__([])
                self.calls = 0
            async def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return frame('heartbeats', sec=300)
                if self.calls == 2:
                    while not started.is_set():
                        await asyncio.sleep(.001)
                    return frame(sec=300, rows=[row(300)])
                if self.calls == 3:
                    self.assertion = feed.state['last_candle'] == (T+timedelta(seconds=300)).isoformat()
                    release.set()
                    stop.set()
                    return frame('heartbeats', seq=1, sec=300)
        socket = ConcurrentSocket()
        try:
            await asyncio.wait_for(LiveCollector(feed, historical=HistoricalClient(transport=transport,
                clock=lambda:T+timedelta(seconds=300)), connector=lambda _:socket,
                clock=lambda:T+timedelta(seconds=300)).run(stop), 3)
            self.assertTrue(socket.assertion)
            self.assertEqual(len(list(self.store.candles('BTC-USD',300))),1)
        finally:
            release.set()


class ChunkTests(unittest.IsolatedAsyncioTestCase):
    setUp, tearDown = history.StoreTests.setUp, history.StoreTests.tearDown
    async def test_recovery_uses_one_bounded_page_and_advances_gap(self):
        from urllib.parse import parse_qs, urlparse
        feed = FeedProcessor(self.store, 'BTC-USD')
        feed.begin_session('s', T)
        feed.ingest(frame(), T)
        feed.ingest(frame(seq=2, sec=900, rows=[row(900)]), T + timedelta(seconds=900))
        ranges = []
        def transport(url, _):
            params = parse_qs(urlparse(url).query)
            start, end = int(params['start'][0]), int(params['end'][0])+1
            ranges.append((start,end))
            return json.dumps({'candles':[row(t-int(T.timestamp())) for t in range(start,end,300)]})
        tick = [0]
        collector = LiveCollector(feed, historical=HistoricalClient(transport=transport, page_limit=2,
            clock=lambda:T+timedelta(seconds=900)), clock=lambda:T+timedelta(seconds=900), monotonic=lambda:tick[0])
        await collector.repair()
        self.assertEqual(feed.state['repair']['start'], int(T.timestamp())+600)
        tick[0] = 31
        await collector.repair()
        self.assertIsNone(feed.state['repair'])
        self.assertEqual([b-a for a,b in ranges], [600,300])
        destination = MarketStore(Path(self.temp.name)/'replay.db')
        try:
            self.assertTrue(replay_journal(self.store, destination)['matched'])
        finally:
            destination.close()
