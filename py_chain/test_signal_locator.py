import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import signal_locator as locator, webapp


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class SignalLocatorTests(unittest.TestCase):
    def setUp(self):
        self.signals = webapp.SignalLog()
        self.sample = {'time': 100, 'markRes': '3', 'periodX': '15', 'direction': 'long', 'strategyKey': 'one'}
        self.row = self.signals.append_signal('backtest', self.sample, symbol='OANDA:XAUUSD')
        self.lock = webapp.ChartLock()
        self.emit = Mock()
        self.manager = locator.LocateManager(self.signals, self.lock, self.emit)

    def test_original_symbol_survives_fill_and_configuration_change(self):
        filled = self.signals.fill_trade('backtest', {**self.sample, 'signalTime': 100}, symbol='OTHER')
        self.assertEqual(filled['symbol'], 'OANDA:XAUUSD')
        worker = webapp.ModeWorker(self.signals, SimpleNamespace(emit=Mock()))
        worker.cfg = {'symbol': 'FIRST'}
        worker._on_signal(self.sample)
        worker.cfg = {'symbol': 'SECOND'}
        self.assertEqual(self.signals.list()[-1]['symbol'], 'FIRST')
        created = self.signals.fill_trade('replay', self.sample, symbol='THIRD')
        self.assertEqual(created['symbol'], 'THIRD')

    def test_bad_metadata_and_mode_never_touch_chart(self):
        with self.assertRaises(LookupError):
            self.manager.start('live', self.row['id'])
        for changes in ({'symbol': None}, {'time': None}, {'time': float('nan')}, {'time': True}, {'markRes': ''}, {'markRes': 'bad'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                locator.validate_signal({**self.row, **changes})
        self.signals.clear('backtest')
        with self.assertRaises(LookupError):
            self.manager.start('backtest', self.row['id'])
        self.assertFalse(self.lock.locked())

    def test_clear_does_not_reuse_a_pending_record_id(self):
        self.signals.clear()
        new = self.signals.append_signal('backtest', self.sample, symbol='SECOND')
        self.assertNotEqual(new['id'], self.row['id'])
        self.assertIsNone(self.signals.get(self.row['id'], 'backtest'))

    def test_busy_rejection_and_concurrent_request(self):
        with patch.object(webapp, '_active_mode', 'replay'), patch.object(webapp, '_active_owner', -1):
            with self.assertRaises(RuntimeError):
                self.manager.start('backtest', self.row['id'])
        with patch.object(locator.threading, 'Thread') as thread:
            self.manager.start('backtest', self.row['id'])
            try:
                with self.assertRaises(RuntimeError):
                    self.manager.start('backtest', self.row['id'])
                thread.assert_called_once()
            finally:
                self.lock.release()

    def test_completion_and_error_always_release_lock(self):
        for failure in (None, RuntimeError('connection lost')):
            with patch.object(locator.threading, 'Thread', ImmediateThread), patch.object(locator, 'locate_signal', return_value={'time': 100}, side_effect=failure):
                job = self.manager.start('backtest', self.row['id'])
            done = self.manager.snapshot()
            self.assertEqual(done['jobId'], job['jobId'])
            self.assertEqual(done['state'], 'error' if failure else 'done')
            self.assertFalse(self.lock.locked())
            self.emit.assert_called_with('locate_done', done)

    def test_thread_start_failure_releases_lock(self):
        with patch.object(locator.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('cannot start')
            with self.assertRaises(RuntimeError):
                self.manager.start('backtest', self.row['id'])
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.manager.snapshot()['state'], 'error')

    def test_history_missing_timeout_and_connection_failure(self):
        for data, timeout in (({'bar': None, 'end': True}, 360), (None, 0)):
            client = Mock()
            client.evaluate.return_value = data
            with patch.object(locator, 'CDPClient') as cls:
                cls.return_value.__enter__.return_value = client
                with self.assertRaises(locator.CDPError):
                    locator.locate_signal(self.row, timeout=timeout)
            self.assertFalse(any('setTimeViewport' in str(call) for call in client.evaluate.call_args_list))
        with patch.object(locator, 'CDPClient') as cls:
            cls.return_value.__enter__.side_effect = locator.CDPError('disconnected')
            with self.assertRaises(locator.CDPError):
                locator.locate_signal(self.row)

    def test_timestamp_inside_latest_candle_and_unavailable_gap(self):
        for stamp, valid in ((110, True), (300, False)):
            client = Mock()
            client.evaluate.side_effect = [None, None,
                {'bar': {'time': 100, 'index': 9}, 'last': 100, 'barEnd': 280},
                {'time': 100, 'index': 9}]
            with patch.object(locator, 'CDPClient') as cls:
                cls.return_value.__enter__.return_value = client
                if valid:
                    self.assertEqual(locator.locate_signal({**self.row, 'time': stamp})['time'], 100)
                else:
                    with self.assertRaises(locator.CDPError):
                        locator.locate_signal({**self.row, 'time': stamp})

    def test_api_validates_input_and_uses_server_record(self):
        app = SimpleNamespace(locator=self.manager)
        def request(body):
            replies = []
            handler = SimpleNamespace(path='/api/signals/locate', _read_body=lambda: body,
                                      _tune=lambda _: False, _send_json=lambda obj, code=200: replies.append((obj, code)))
            with patch.object(webapp.analysis_api, 'handle', return_value=False):
                webapp.make_handler(app).do_POST(handler)
            return replies[0]
        for body in (None, {}, {'mode': 'backtest', 'id': True}, {'mode': 'bad', 'id': 1}):
            self.assertEqual(request(body)[1], 400)
        self.assertEqual(request({'mode': 'live', 'id': 1})[1], 404)
        with patch.object(locator.threading, 'Thread', ImmediateThread), patch.object(locator, 'locate_signal') as locate:
            self.assertEqual(request({'mode': 'backtest', 'id': 1, 'symbol': 'EVIL', 'time': 999})[1], 202)
            self.assertEqual(locate.call_args.args[0], self.row)


if __name__ == '__main__':
    unittest.main()
