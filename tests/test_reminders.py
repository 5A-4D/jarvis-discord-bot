import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jarvis_bot.bot import JarvisDiscordClient
from jarvis_bot.handoff import safe_handoff_request
from jarvis_bot.reminders import (
    MAX_PENDING, ReminderRequest, ReminderStore, deliver_one,
    handle_reminder_request, is_reminder_command, parse_reminder, reminder_payload,
)

NOW = datetime(2026, 11, 17, 19, 26, 12, 640000, tzinfo=timezone.utc)


class ReminderParsingTests(unittest.TestCase):
    def test_original_request_uses_message_time_not_processing_time(self):
        request = parse_reminder('remind me to do this in 3.5 hours https://x.com/maimai_wuwa/status/2122623868334649698', now=NOW)
        self.assertEqual(request.due_at.isoformat(), '2026-11-17T22:56:12.640000+00:00')
        self.assertEqual(request.text, 'do this https://x.com/maimai_wuwa/status/2122623868334649698')

    def test_prefix_suffix_compound_and_common_units(self):
        cases = (
            ('remind me in 30 minutes to stretch', 1800, 'stretch'),
            ('remind me to stretch in 30 mins', 1800, 'stretch'),
            ('Please remind me in 1 hour and 30 minutes to check it', 5400, 'check it'),
            ('could you remind me in an hour to check it', 3600, 'check it'),
            ('/remind in two days to check it', 172800, 'check it'),
            ('remind me in 2h 30m to check it', 9000, 'check it'),
            ('remind me in 1 week to check it', 604800, 'check it'),
            ('remind me in 5 secs to check it', 5, 'check it'),
        )
        for prompt, seconds, text in cases:
            with self.subTest(prompt=prompt):
                request = parse_reminder(prompt, now=NOW)
                self.assertEqual(request.due_at, NOW + timedelta(seconds=seconds))
                self.assertEqual(request.text, text)

    def test_recall_and_unrelated_text_are_not_scheduler_commands(self):
        for prompt in ('remind me those speakers I asked for', 'remind me how to sign in', 'what is a reminder?', 'remind Bob in 3 hours to work'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(parse_reminder(prompt, now=NOW))

    def test_unsupported_or_invalid_times_do_not_fall_through_to_model(self):
        for prompt in ('remind me tomorrow to stretch', 'remind me at 5 to stretch', 'remind me in -1 hour to stretch', 'remind me in 0 hours to stretch', 'remind me in 400 days to stretch', '/remind stretch', 'remind me in 1 hour'):
            with self.subTest(prompt=prompt):
                self.assertTrue(is_reminder_command(prompt))
                with self.assertRaises(ValueError):
                    parse_reminder(prompt, now=NOW)

    def test_reminders_are_never_delegated_to_r(self):
        for prompt in ('remind me in 3.5 hours to revisit https://example.com', 'remind me tomorrow to stretch', '/reminders'):
            self.assertFalse(safe_handoff_request(prompt))

    def test_due_time_handles_midnight_and_timezone_offsets(self):
        start = datetime(2026, 11, 17, 22, 30, tzinfo=timezone(timedelta(hours=-6)))
        request = parse_reminder('remind me in 3.5 hours to stretch', now=start)
        self.assertEqual(request.due_at, datetime(2026, 11, 18, 8, tzinfo=timezone.utc))


class ReminderStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ReminderStore(Path(self.directory.name) / 'reminders.sqlite3')
        self.store.initialize()

    def tearDown(self):
        self.directory.cleanup()

    def create(self, source_id=123, user_id=300, channel_id=200, text='check it', due_at=NOW):
        return self.store.create(source_id=source_id, guild_id=100, channel_id=channel_id,
            user_id=user_id, request=ReminderRequest(text, due_at), source_url='https://discord.com/channels/100/200/123')

    def test_pending_reminder_survives_new_store_and_recovery(self):
        row = self.create()
        restarted = ReminderStore(self.store.path)
        restarted.initialize()
        restarted.recover()
        self.assertEqual(restarted.list_for(300, 200)[0]['id'], row['id'])
        self.assertIsNotNone(restarted.claim(now=NOW))

    def test_source_message_is_idempotent(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.store.list_for(300, 200)), 1)
        with self.assertRaises(ValueError):
            self.create(user_id=301)

    def test_list_and_cancel_are_scoped_to_requester_and_conversation(self):
        row = self.create()
        self.create(source_id=124, user_id=301, text='other user')
        self.create(source_id=125, channel_id=201, text='private conversation')
        self.assertEqual([x['text'] for x in self.store.list_for(300, 200)], ['check it'])
        self.assertFalse(self.store.cancel(row['id'], 301, 200))
        self.assertFalse(self.store.cancel(row['id'], 300, 201))
        self.assertTrue(self.store.cancel(row['id'], 300, 200))
        self.assertIsNone(self.store.claim(now=NOW - timedelta(seconds=1)))

    def test_limit_and_due_time_are_enforced(self):
        for source in range(MAX_PENDING):
            self.create(source_id=source, due_at=NOW + timedelta(hours=1))
        with self.assertRaises(ValueError):
            self.create(source_id=999)
        self.assertIsNone(self.store.claim(now=NOW))
        self.create(source_id=999, user_id=301)

    async def test_concurrent_duplicate_creation_and_claim_are_atomic(self):
        rows = await asyncio.gather(*(asyncio.to_thread(self.create) for _ in range(5)))
        self.assertEqual(len({r['id'] for r in rows}), 1)
        claims = await asyncio.gather(*(asyncio.to_thread(self.store.claim, now=NOW) for _ in range(5)))
        self.assertEqual(sum(x is not None for x in claims), 1)

    async def test_pending_limit_is_atomic_across_connections(self):
        async def create(source):
            try:
                return await asyncio.to_thread(self.create, source_id=source)
            except ValueError:
                return None
        rows = await asyncio.gather(*(create(i) for i in range(15)))
        self.assertEqual(sum(row is not None for row in rows), MAX_PENDING)

    def test_restart_does_not_duplicate_a_send_interrupted_after_acceptance(self):
        self.create()
        self.store.claim(now=NOW)
        self.store.recover()
        self.assertIsNone(self.store.claim(now=NOW))
        self.assertEqual(self.store.list_for(300, 200)[0]['status'], 'uncertain')

    def test_only_requester_can_be_pinged(self):
        payload = reminder_payload(self.create(text='@everyone <@400000000000000000> <@&500000000000000000>'))
        self.assertEqual(payload['allowed_mentions']['users'], ['300'])
        self.assertEqual(payload['allowed_mentions']['parse'], [])
        self.assertNotIn('@everyone', payload['content'])
        self.assertNotIn('<@400000000000000000>', payload['content'])
        self.assertTrue(payload['enforce_nonce'])

    async def test_delivery_is_verified_and_sent_once(self):
        row = self.create()
        calls = []
        async def sender(channel, payload):
            calls.append((channel, payload))
            return {'id': '700', 'channel_id': str(channel), 'content': payload['content']}
        self.assertTrue(await deliver_one(self.store, sender, now=NOW))
        self.assertFalse(await deliver_one(self.store, sender, now=NOW))
        self.assertEqual(len(calls), 1)
        with closing(self.store.connect()) as db:
            saved = db.execute('SELECT * FROM reminders WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(saved['status'], 'sent')
        self.assertEqual(saved['delivered_message_id'], '700')

    async def test_ambiguous_or_mismatched_delivery_is_not_reported_sent(self):
        row = self.create()
        async def sender(channel, payload):
            return {'id': '700', 'channel_id': '999', 'content': payload['content']}
        await deliver_one(self.store, sender, now=NOW)
        self.assertEqual(self.store.list_for(300, 200)[0]['status'], 'uncertain')
        self.assertFalse(await deliver_one(self.store, sender, now=NOW))

    async def test_transport_timeout_is_retained_for_reconciliation(self):
        self.create()
        async def sender(channel, payload):
            raise TimeoutError()
        await deliver_one(self.store, sender, now=NOW)
        self.assertEqual(self.store.list_for(300, 200)[0]['status'], 'uncertain')


class ReminderRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ReminderStore(Path(self.directory.name) / 'reminders.sqlite3')
        self.store.initialize()
        self.message = SimpleNamespace(id=123, author=SimpleNamespace(id=300, bot=False), webhook_id=None,
            channel=SimpleNamespace(id=200), guild=SimpleNamespace(id=100), reference=None,
            created_at=NOW, jump_url='https://discord.com/channels/100/200/123', reply=AsyncMock())
        self.client = SimpleNamespace(_reminders=self.store, _public_channel_allowed=lambda _: True)

    def tearDown(self):
        self.directory.cleanup()

    async def test_public_creation_uses_own_id_and_same_channel(self):
        self.assertTrue(await handle_reminder_request(self.client, self.message, 'remind me in 3.5 hours to check it', is_owner=False))
        row = self.store.list_for(300, 200)[0]
        self.assertEqual(row['due_at'], (NOW + timedelta(hours=3.5)).timestamp())
        self.assertIn('I’ll ping you here', self.message.reply.await_args.args[0])
        self.assertEqual(self.message.reply.await_args.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_public_reminder_is_handled_before_model_quota_or_host_routes(self):
        self.message.content = 'Jarvis remind me in 3.5 hours to check it'
        self.message.attachments = []
        self.client._channel_allowed = lambda _: True
        self.client._store_discord_message = AsyncMock()
        self.client._is_reply_to_self = AsyncMock(return_value=False)
        self.client.user = SimpleNamespace(id=42)
        self.client.settings = SimpleNamespace(trigger='Jarvis', authorized_user_ids=frozenset())
        self.client._handle_public_request = AsyncMock()
        self.client._ask_r = AsyncMock()
        await JarvisDiscordClient.on_message(self.client, self.message)
        self.assertEqual(len(self.store.list_for(300, 200)), 1)
        self.client._handle_public_request.assert_not_awaited()
        self.client._ask_r.assert_not_awaited()

    async def test_disabled_channel_bots_and_webhooks_cannot_create(self):
        self.client._public_channel_allowed = lambda _: False
        await handle_reminder_request(self.client, self.message, 'remind me in 1 hour to check it', is_owner=False)
        self.assertEqual(self.store.list_for(300, 200), [])
        self.client._public_channel_allowed = lambda _: True
        for bot, webhook in ((True, None), (False, 999)):
            self.message.author.bot = bot
            self.message.webhook_id = webhook
            await handle_reminder_request(self.client, self.message, 'remind me in 1 hour to check it', is_owner=False)
        self.assertEqual(self.store.list_for(300, 200), [])

    async def test_dm_reminder_stays_in_dm(self):
        self.message.guild = None
        self.client._public_channel_allowed = lambda _: False
        await handle_reminder_request(self.client, self.message, 'remind me in 1 hour to check it', is_owner=False)
        self.assertEqual(self.store.list_for(300, 200)[0]['guild_id'], '0')

    async def test_invalid_time_gives_help_without_creating(self):
        await handle_reminder_request(self.client, self.message, 'remind me tomorrow to check it', is_owner=False)
        self.assertIn('3.5 hours', self.message.reply.await_args.args[0])
        self.assertEqual(self.store.list_for(300, 200), [])

    async def test_owner_bare_reminders_still_uses_existing_checklist(self):
        self.assertFalse(await handle_reminder_request(self.client, self.message, 'reminders', is_owner=True))
        self.message.reply.assert_not_awaited()

    async def test_public_list_and_cancellation(self):
        await handle_reminder_request(self.client, self.message, 'remind me in 1 hour to check it', is_owner=False)
        row = self.store.list_for(300, 200)[0]
        await handle_reminder_request(self.client, self.message, '/reminders', is_owner=False)
        self.assertIn(f"#{row['id']}", self.message.reply.await_args.args[0])
        await handle_reminder_request(self.client, self.message, f"cancel reminder {row['id']}", is_owner=False)
        self.assertEqual(self.store.list_for(300, 200), [])


if __name__ == '__main__':
    unittest.main()
