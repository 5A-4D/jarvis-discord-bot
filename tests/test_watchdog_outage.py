import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from jarvis_bot.watchdog import NotificationMonitor, discord_is_reachable, run


class OutageDeliveryTests(unittest.TestCase):
    def monitor(self, path):
        return NotificationMonitor(path, failure_threshold=2, recovery_threshold=2)

    def test_failed_offline_delivery_survives_recovery_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{"state":"online"}')
            monitor = self.monitor(path)
            monitor.observe(False, now=100)
            monitor.observe(False, now=130)
            with patch('jarvis_bot.watchdog.notify', side_effect=OSError('offline')):
                monitor.deliver(123, Path('off.gif'), Path('on.gif'), 'token')
            self.assertEqual(json.loads(path.read_text())['state'], 'offline')
            monitor = self.monitor(path)
            monitor.observe(True, now=160)
            monitor.observe(True, now=190)
            self.assertEqual([e['state'] for e in monitor.pending], ['offline', 'online'])
            with patch('jarvis_bot.watchdog.notify', return_value='789') as notify:
                monitor.deliver(123, Path('off.gif'), Path('on.gif'), 'token')
                self.assertEqual([c.args[1] for c in notify.call_args_list], ['offline', 'online'])
                self.assertEqual([c.kwargs['observed_at'] for c in notify.call_args_list], [130, 190])
            self.assertEqual(self.monitor(path).pending, [])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_partial_delivery_retries_only_unsent_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{"state":"online"}')
            monitor = self.monitor(path)
            for healthy in [False, False, True, True]:
                monitor.observe(healthy)
            with patch('jarvis_bot.watchdog.notify', side_effect=['1', TimeoutError()]):
                monitor.deliver(123, Path('off'), Path('on'), 'token')
            restored = self.monitor(path)
            self.assertEqual([e['state'] for e in restored.pending], ['online'])
            with patch('jarvis_bot.watchdog.notify', return_value='2') as notify:
                restored.deliver(123, Path('off'), Path('on'), 'token')
                self.assertEqual(notify.call_count, 1)
                self.assertEqual(notify.call_args.args[1], 'online')

    def test_initial_baseline_is_quiet_and_transient_failure_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.monitor(Path(directory) / 'state.json')
            for healthy in [True, True, False, True]:
                monitor.observe(healthy)
            self.assertEqual(monitor.tracker.state, 'online')
            self.assertEqual(monitor.pending, [])

    def test_repeated_outages_are_preserved_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{"state":"online"}')
            monitor = self.monitor(path)
            for healthy in [False, False, False, True, True, False, False]:
                monitor.observe(healthy)
            self.assertEqual([e['state'] for e in self.monitor(path).pending],
                             ['offline', 'online', 'offline'])

    def test_probe_rejects_network_failure_and_non_gateway_response(self):
        with patch('jarvis_bot.watchdog.urllib.request.urlopen', side_effect=OSError()):
            self.assertFalse(discord_is_reachable())
        for body, expected in [(b'{"url":"wss://gateway.discord.gg"}', True),
                               (b'{}', False), (b'<html>portal</html>', False)]:
            with patch('jarvis_bot.watchdog.urllib.request.urlopen') as urlopen:
                urlopen.return_value.__enter__.return_value = io.BytesIO(body)
                self.assertEqual(discord_is_reachable(), expected)
                self.assertEqual(urlopen.call_args.kwargs['timeout'], 5)

    def test_run_detects_internet_outage_even_when_heartbeat_stays_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{"state":"online"}')
            args = Namespace(state_file=path, heartbeat_file=Path('heartbeat'),
                             failure_threshold=2, recovery_threshold=2, interval=30,
                             maximum_heartbeat_age=45, channel_id=123,
                             offline_gif=Path('off'), online_gif=Path('on'), once=False)
            with (patch('jarvis_bot.watchdog.discord_token', return_value='token'),
                  patch('jarvis_bot.watchdog.heartbeat_is_healthy', return_value=True),
                  patch('jarvis_bot.watchdog.discord_is_reachable', side_effect=[False, False, True, True]),
                  patch('jarvis_bot.watchdog.time.sleep', side_effect=[None, None, None, StopIteration]),
                  patch('jarvis_bot.watchdog.notify', return_value='123') as notify):
                with self.assertRaises(StopIteration):
                    run(args)
                self.assertEqual([c.args[1] for c in notify.call_args_list], ['offline', 'online'])
            self.assertEqual(json.loads(path.read_text()), {'state': 'online', 'pending': []})


if __name__ == '__main__':
    unittest.main()
