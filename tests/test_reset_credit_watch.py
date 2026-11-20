import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis_bot.reset_credit_watch import (
    Credit, OwnerDM, check_once, due_alerts, load_state, parse_credits, read_credits,
)


NOW = 1793880000
OWNER = 900000000000000003


def row(identifier='first', hours=48, **changes):
    value = {'id': identifier, 'expiresAt': NOW + hours * 3600,
             'status': 'available', 'resetType': 'codexRateLimits'}
    value.update(changes)
    return value


def response(*rows, count=None):
    return {'rateLimitResetCredits': {'availableCount': len(rows) if count is None else count,
                                     'credits': list(rows)}}


class ResetWatchTests(unittest.TestCase):
    def test_three_warning_bands_persist_across_restarts_without_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            send = Mock(return_value='message')
            data = response(row(hours=72))
            for elapsed in (0, 1, 48 * 3600, 48 * 3600 + 1, 66 * 3600, 66 * 3600 + 1):
                check_once(data, path, OWNER, 'America/Chicago', send, now=NOW + elapsed)
            self.assertEqual(send.call_count, 3)
            self.assertEqual(len({call.args[1] for call in send.call_args_list}), 3)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn('CST', send.call_args.args[0])

    def test_starting_inside_six_hours_sends_only_one_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            send = Mock(return_value='message')
            check_once(response(row(hours=4)), Path(directory) / 'state.json',
                       OWNER, 'America/Chicago', send, now=NOW)
            self.assertEqual(send.call_count, 1)
            self.assertIn('about 4 hours', send.call_args.args[0])

    def test_failed_delivery_is_retried_without_marking_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            send = Mock(side_effect=OSError('offline'))
            with self.assertRaises(OSError):
                check_once(response(row()), path, OWNER, 'America/Chicago', send, now=NOW)
            self.assertEqual(load_state(path, OWNER)['alerts'], {})
            retry = Mock(return_value='verified')
            check_once(response(row()), path, OWNER, 'America/Chicago', retry, now=NOW)
            self.assertEqual(retry.call_args.args[1], send.call_args.args[1])

    def test_consumed_removed_expired_or_unknown_expiry_do_not_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            send = Mock(side_effect=OSError())
            with self.assertRaises(OSError):
                check_once(response(row()), path, OWNER, 'America/Chicago', send, now=NOW)
            send = Mock(return_value='message')
            for data in (response(), response(row(status='consumed')),
                         response(row(hours=-1)), response(row(expiresAt=None)),
                         response(row(), count=0)):
                check_once(data, path, OWNER, 'America/Chicago', send, now=NOW)
            send.assert_not_called()

    def test_incomplete_details_do_not_turn_authoritative_three_into_one(self):
        count, credits = parse_credits(response(row(), count=3))
        self.assertEqual(count, 3)
        self.assertEqual(len(credits), 1)
        self.assertEqual(parse_credits({'rateLimitResetCredits': {
            'availableCount': 3, 'credits': None}}), (3, None))
        with self.assertRaises(ValueError):
            parse_credits({'rateLimitResetCredits': None})

    def test_missing_details_never_reuse_old_expiry_for_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            send = Mock(return_value='message')
            check_once(response(row(hours=80)), path, OWNER, 'America/Chicago', send, now=NOW)
            report = check_once({'rateLimitResetCredits': {'availableCount': 3, 'credits': None}},
                                path, OWNER, 'America/Chicago', send, now=NOW + 24 * 3600)
            send.assert_not_called()
            self.assertFalse(report['details_available'])
            self.assertIsNone(report['expiry_dates'])

    def test_malformed_data_and_state_fail_closed(self):
        for invalid in (True, 'tomorrow', -10, float('nan')):
            with self.assertRaises(ValueError):
                parse_credits(response(row(expiresAt=invalid)))
        with self.assertRaises(ValueError):
            parse_credits(response(row(), row()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{broken')
            send = Mock()
            with self.assertRaises(ValueError):
                check_once(response(row()), path, OWNER, 'America/Chicago', send, now=NOW)
            send.assert_not_called()
            path.write_text(json.dumps({'version': 1, 'owner_id': 999, 'alerts': {}}))
            with self.assertRaises(ValueError):
                load_state(path, OWNER)

    def test_per_credit_warnings_and_expiry_changes_are_independent(self):
        state = {'alerts': {Credit('first', NOW + 48 * 3600).key: {'band_hours': 72}}}
        credits = [Credit('second', NOW + 8 * 3600), Credit('first', NOW + 48 * 3600),
                   Credit('third', NOW + 90 * 3600), Credit('first', NOW + 50 * 3600)]
        due = due_alerts(credits, state, NOW)
        self.assertEqual([(c.credit_id, band) for c, band in due], [('second', 24), ('first', 72)])

    def test_dm_verifies_owner_and_receipt_and_disables_mentions(self):
        with patch('jarvis_bot.reset_credit_watch._discord_token', return_value='private'):
            sender = OwnerDM(OWNER)
        sender.request = Mock(side_effect=[
            {'id': '123'}, {'id': '123', 'type': 1, 'recipients': [{'id': str(OWNER)}]},
            {'id': '456', 'channel_id': '123', 'content': 'test reminder'},
        ])
        self.assertEqual(sender.send('test reminder', 'stable-nonce'), '456')
        payload = sender.request.call_args.args[1]
        self.assertEqual(payload['allowed_mentions'], {'parse': []})
        self.assertTrue(payload['enforce_nonce'])

    def test_wrong_or_group_dm_is_rejected_before_send(self):
        for recipients, channel_type in [([{'id': 'wrong'}], 1),
                                          ([{'id': str(OWNER)}], 3),
                                          ([{'id': str(OWNER)}, {'id': 'wrong'}], 1)]:
            with patch('jarvis_bot.reset_credit_watch._discord_token', return_value='private'):
                sender = OwnerDM(OWNER)
            sender.request = Mock(side_effect=[{'id': '123'},
                {'id': '123', 'type': channel_type, 'recipients': recipients}])
            with self.assertRaises(RuntimeError):
                sender.send('account information', 'nonce')
            self.assertEqual(sender.request.call_count, 2)

    def test_wrong_message_receipt_is_rejected(self):
        with patch('jarvis_bot.reset_credit_watch._discord_token', return_value='private'):
            sender = OwnerDM(OWNER)
        sender.channel_id = '123'
        sender.request = Mock(return_value={'id': '456', 'channel_id': '999', 'content': 'test'})
        with self.assertRaises(RuntimeError):
            sender.send('test', 'nonce')


class ResetReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_is_read_only_and_strips_credentials_from_child_environment(self):
        # A small executable double rejects any RPC beyond initialize + limits read.
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'fake-codex'
            binary.write_text('''#!/usr/bin/env python3
import json, os, sys
assert sys.argv[1:] == ['app-server', '--listen', 'stdio://']
assert not any(os.environ.get(k) for k in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'DISCORD_BOT_TOKEN'))
for expected in ('initialize', 'initialized', 'account/rateLimits/read'):
    request = json.loads(sys.stdin.readline())
    assert request['method'] == expected
print(json.dumps({'id': 1, 'result': {'rateLimitResetCredits': {'availableCount': 0, 'credits': []}}}), flush=True)
sys.stdin.read()
''')
            binary.chmod(0o700)
            launch = asyncio.create_subprocess_exec

            async def launch_fixture(*args, **kwargs):
                # /tmp is intentionally noexec in the live bot container.
                return await launch(sys.executable, str(binary), *args[1:], **kwargs)

            with patch.dict(os.environ, {'OPENAI_API_KEY': 'private', 'CODEX_API_KEY': 'private',
                                         'DISCORD_BOT_TOKEN': 'private'}), patch(
                    'jarvis_bot.reset_credit_watch.asyncio.create_subprocess_exec', launch_fixture):
                result = await read_credits(binary, Path(directory))
            self.assertEqual(parse_credits(result), (0, []))


if __name__ == '__main__':
    unittest.main()
