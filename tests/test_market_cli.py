import asyncio
import signal
import unittest
from unittest.mock import ANY, AsyncMock, Mock, call, patch

from trading_bot.market_data.cli import collect


class SignalPortabilityTests(unittest.IsolatedAsyncioTestCase):
    def loop_proxy(self, unsupported=()):
        real_loop = asyncio.get_running_loop()
        loop = Mock(spec=['add_signal_handler', 'remove_signal_handler', 'call_later'])
        handlers, timers = {}, []
        def register(sig, callback):
            if sig in unsupported:
                raise NotImplementedError('unsupported by this event loop')
            handlers[sig] = callback
        def schedule(delay, callback):
            timer = real_loop.call_later(delay, callback)
            timers.append(timer)
            return timer
        loop.add_signal_handler.side_effect = register
        loop.call_later.side_effect = schedule
        return loop, handlers, timers

    async def test_windows_unsupported_handlers_preserve_duration_and_drain(self):
        loop, _, timers = self.loop_proxy((signal.SIGINT, signal.SIGTERM))
        drained = []
        async def run(stop):
            await stop.wait()
            await asyncio.sleep(0)  # Collector still gets to finish async cleanup.
            drained.append(True)
        collector = Mock(run=AsyncMock(side_effect=run))
        with patch('trading_bot.market_data.cli.asyncio.get_running_loop', return_value=loop), patch(
                'trading_bot.market_data.cli.LiveCollector', return_value=collector):
            await asyncio.wait_for(collect(object(), .01), 1)
        self.assertEqual(drained, [True])
        self.assertEqual(loop.add_signal_handler.call_args_list, [call(signal.SIGINT, ANY),
                                                                 call(signal.SIGTERM, ANY)])
        loop.remove_signal_handler.assert_not_called()
        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].cancelled())

    async def test_partial_support_removes_only_installed_handler(self):
        loop, handlers, timers = self.loop_proxy((signal.SIGTERM,))
        async def run(stop):
            handlers[signal.SIGINT]()
            await stop.wait()
        with patch('trading_bot.market_data.cli.asyncio.get_running_loop', return_value=loop), patch(
                'trading_bot.market_data.cli.LiveCollector', return_value=Mock(run=AsyncMock(side_effect=run))):
            await collect(object(), 60)
        loop.remove_signal_handler.assert_called_once_with(signal.SIGINT)
        self.assertTrue(timers[0].cancelled())

    async def test_supported_signal_stops_continuous_collection(self):
        loop, handlers, _ = self.loop_proxy()
        async def run(stop):
            handlers[signal.SIGTERM]()
            await stop.wait()
        with patch('trading_bot.market_data.cli.asyncio.get_running_loop', return_value=loop), patch(
                'trading_bot.market_data.cli.LiveCollector', return_value=Mock(run=AsyncMock(side_effect=run))):
            await collect(object(), None)
        self.assertEqual(loop.remove_signal_handler.call_args_list, [call(signal.SIGINT), call(signal.SIGTERM)])
        loop.call_later.assert_not_called()

    async def test_unsupported_continuous_collection_does_not_crash(self):
        loop, _, _ = self.loop_proxy((signal.SIGINT, signal.SIGTERM))
        started = asyncio.Event()
        events = []
        async def run(stop):
            events.append(stop)
            started.set()
            await stop.wait()
        with patch('trading_bot.market_data.cli.asyncio.get_running_loop', return_value=loop), patch(
                'trading_bot.market_data.cli.LiveCollector', return_value=Mock(run=AsyncMock(side_effect=run))):
            task = asyncio.create_task(collect(object(), None))
            try:
                await asyncio.wait_for(started.wait(), 1)
                events[0].set()
                await asyncio.wait_for(task, 1)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        loop.call_later.assert_not_called()
        loop.remove_signal_handler.assert_not_called()

    async def test_collector_failure_cancels_timer_and_removes_handlers(self):
        loop, _, timers = self.loop_proxy()
        with patch('trading_bot.market_data.cli.asyncio.get_running_loop', return_value=loop), patch(
                'trading_bot.market_data.cli.LiveCollector', return_value=Mock(run=AsyncMock(side_effect=RuntimeError('failure')))):
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                await collect(object(), 60)
        self.assertTrue(timers[0].cancelled())
        self.assertEqual(loop.remove_signal_handler.call_args_list, [call(signal.SIGINT), call(signal.SIGTERM)])

    async def test_partial_setup_failure_cleans_installed_handler(self):
        loop, _, _ = self.loop_proxy()
        loop.add_signal_handler.side_effect = [None, RuntimeError('setup failed')]
        with patch('trading_bot.market_data.cli.asyncio.get_running_loop', return_value=loop), patch(
                'trading_bot.market_data.cli.LiveCollector') as factory:
            with self.assertRaisesRegex(RuntimeError, 'setup failed'):
                await collect(object(), 60)
        loop.remove_signal_handler.assert_called_once_with(signal.SIGINT)
        loop.call_later.assert_not_called()
        factory.assert_not_called()


class LiveCollectionDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_windows_duration_drains_real_collector_repair(self):
        import tempfile
        from pathlib import Path
        from test_market_feed import frame
        from test_market_history import T
        from trading_bot.market_data.feed import FeedProcessor
        from trading_bot.market_data.live import LiveCollector
        from trading_bot.market_data.store import MarketStore

        loop, _, timers = SignalPortabilityTests.loop_proxy(self, (signal.SIGINT, signal.SIGTERM))
        events, drained = [], []
        repair_started = asyncio.Event()
        class Socket:
            calls = 0
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, raw): pass
            async def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return frame('heartbeats')
                await events[0].wait()
                return frame('heartbeats', seq=1)
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / 'market.db')
            try:
                feed = FeedProcessor(store, 'BTC-USD')
                collector = LiveCollector(feed, connector=lambda _:Socket(), clock=lambda:T)
                original_run = collector.run
                async def run(stop):
                    events.append(stop)
                    await original_run(stop)
                async def repair():
                    repair_started.set()
                    await events[0].wait()
                    await asyncio.sleep(0)
                    drained.append(True)
                collector.run = AsyncMock(side_effect=run)
                collector.repair = AsyncMock(side_effect=repair)
                with patch('trading_bot.market_data.cli.asyncio.get_running_loop', return_value=loop), patch(
                        'trading_bot.market_data.cli.LiveCollector', return_value=collector):
                    await asyncio.wait_for(collect(feed, .02), 1)
                self.assertTrue(repair_started.is_set())
                self.assertEqual(drained, [True])
                self.assertEqual(feed.state['connection'], 'stopped')
                self.assertEqual(feed.state['last_error'], 'collector_stopped')
                self.assertTrue(timers[0].cancelled())
                loop.remove_signal_handler.assert_not_called()
            finally:
                store.close()
