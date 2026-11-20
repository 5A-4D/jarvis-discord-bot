import tempfile
import asyncio
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jarvis_bot.assistant import JarvisAssistant, ParsedCodexEvents
from jarvis_bot.bot import JarvisDiscordClient
from jarvis_bot.handoff import (
    R_BOT_USER_ID, R_HANDOFF_MARKER, R_HANDOFF_POLICY, R_HANDOFF_PREFIX,
    format_r_handoff, is_r_handoff_reply, safe_handoff_request, split_handoff_answer,
)
from jarvis_bot.memory import MemoryStore


class HandoffPolicyTests(unittest.TestCase):
    def test_shareable_public_link_is_allowed(self):
        self.assertTrue(safe_handoff_request('Summarise https://example.com/article'))

    def test_sensitive_or_action_requests_are_not_forwarded(self):
        for prompt in (
            'Install a package', 'Read the files on my desktop', '/forget', '',
            'Summarise http://localhost/admin', 'Summarise https://100.80.1.2/admin',
            'Summarise https://10.0.0.2', 'Summarise https://service.internal',
            'Summarise https://user:pass@example.com',
            'Summarise https://example.com/?access_token=abc',
            'My password is abc, help', 'Read /home/your-user/.env', 'x' * 1201,
        ):
            with self.subTest(prompt=prompt[:60]):
                self.assertFalse(safe_handoff_request(prompt))

    def test_marker_only_works_on_own_final_line(self):
        self.assertEqual(split_handoff_answer('Blocked.\n' + R_HANDOFF_MARKER), ('Blocked.', True))
        for text in ('Here is ' + R_HANDOFF_MARKER, '```\n' + R_HANDOFF_MARKER + '\n```'):
            self.assertEqual(split_handoff_answer(text), (text, False))

    def test_payload_is_original_request_not_context_and_escapes_mentions(self):
        rendered = format_r_handoff('Explain @everyone <@123456789012345678>', 321, 'https://discord.com/channels/1/2/3')
        self.assertTrue(rendered.startswith(R_HANDOFF_PREFIX))
        self.assertNotIn('@everyone', rendered)
        self.assertNotIn('<@123456789012345678>', rendered)
        self.assertIn('No host/account actions', rendered)
        self.assertIn('your own permission checks apply', rendered)
        self.assertIn('handbacks or Jarvis pings', rendered)
        self.assertIn('For <@321>', rendered)
        self.assertIn('[original request](<https://discord.com/channels/1/2/3>)', rendered)
        self.assertIn('\n\n>>> Explain ', rendered)

    def test_multiline_request_keeps_original_text_within_discord_limit(self):
        prompt = 'Explain this:\n' + 'a\n' * 590
        self.assertTrue(safe_handoff_request(prompt))
        rendered = format_r_handoff(prompt, 321, 'https://discord.com/channels/1/2/3')
        self.assertTrue(rendered.endswith('>>> ' + prompt.strip()))
        self.assertLessEqual(len(rendered), 2000)


class HandoffDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setup_message(self, *, bot=False, guild=True, view=True, send=True):
        peer = SimpleNamespace(bot=True, id=R_BOT_USER_ID)
        message = SimpleNamespace(
            id=100, author=SimpleNamespace(id=321, bot=bot),
            guild=SimpleNamespace(get_member=lambda _: peer) if guild else None,
            channel=SimpleNamespace(
                permissions_for=lambda _: SimpleNamespace(view_channel=view, send_messages=send),
                send=AsyncMock(),
            ),
            jump_url='https://discord.com/channels/1/2/100', reply=AsyncMock(),
        )
        async def reply(content, **kwargs):
            return SimpleNamespace(id=200, content=content)
        message.reply.side_effect = reply
        memory = SimpleNamespace(
            reserve_r_handoff=AsyncMock(return_value='reserved'),
            finish_r_handoff=AsyncMock(),
        )
        client = SimpleNamespace(_r_handoff_attempts={}, assistant=SimpleNamespace(memory=memory))
        return client, message

    async def test_verified_once_and_only_r_can_be_pinged(self):
        client, message = self.setup_message()
        self.assertTrue(await JarvisDiscordClient._ask_r(client, message, 'Summarise https://example.com'))
        self.assertFalse(await JarvisDiscordClient._ask_r(client, message, 'Summarise https://example.com'))
        message.reply.assert_awaited_once()
        mentions = message.reply.await_args.kwargs['allowed_mentions'].to_dict()
        self.assertEqual(mentions['users'], [R_BOT_USER_ID])
        self.assertFalse(mentions.get('replied_user', False))
        self.assertNotIn('everyone', mentions['parse'])
        self.assertNotIn('roles', mentions['parse'])
        client.assistant.memory.reserve_r_handoff.assert_awaited_once_with(message.id)
        client.assistant.memory.finish_r_handoff.assert_awaited_once_with(message.id, delivered=True)

    async def test_daily_limit_and_database_failure_prevent_delivery(self):
        for outcome in ('daily_limit', 'duplicate', sqlite3.OperationalError('unavailable')):
            with self.subTest(outcome=outcome):
                client, message = self.setup_message()
                if isinstance(outcome, Exception):
                    client.assistant.memory.reserve_r_handoff.side_effect = outcome
                else:
                    client.assistant.memory.reserve_r_handoff.return_value = outcome
                self.assertFalse(await JarvisDiscordClient._ask_r(client, message, 'Explain this'))
                message.reply.assert_not_awaited()

    async def test_failed_send_keeps_reserved_slot(self):
        client, message = self.setup_message()
        message.reply.side_effect = None
        message.reply.return_value = None
        self.assertFalse(await JarvisDiscordClient._ask_r(client, message, 'Explain this'))
        client.assistant.memory.finish_r_handoff.assert_awaited_once_with(message.id, delivered=False)

    async def test_dms_bots_permissions_and_sensitive_prompts_do_not_send(self):
        for kwargs, prompt in (
            ({'guild': False}, 'Explain this'), ({'bot': True}, 'Explain this'),
            ({'view': False}, 'Explain this'), ({'send': False}, 'Explain this'),
            ({}, 'Install software'), ({}, 'My password is private'),
        ):
            with self.subTest(kwargs=kwargs, prompt=prompt):
                client, message = self.setup_message(**kwargs)
                self.assertFalse(await JarvisDiscordClient._ask_r(client, message, prompt))
                message.reply.assert_not_awaited()

    async def test_no_claim_of_delivery_without_returned_message(self):
        client, message = self.setup_message()
        message.reply.side_effect = None
        message.reply.return_value = None
        self.assertFalse(await JarvisDiscordClient._ask_r(client, message, 'Explain this'))

    async def test_answer_control_marker_routes_original_prompt(self):
        client, message = self.setup_message()
        client._ask_r = AsyncMock(return_value=True)
        await JarvisDiscordClient._send_answer(
            client, message, 'I could not fetch the page.\n' + R_HANDOFF_MARKER,
            handoff_prompt='Summarise https://example.com',
        )
        self.assertEqual(message.reply.await_args.args[0], 'I could not fetch the page.')
        client._ask_r.assert_awaited_once_with(message, 'Summarise https://example.com')

    async def test_bot_answer_cannot_delegate(self):
        client, message = self.setup_message(bot=True)
        client._ask_r = AsyncMock()
        await JarvisDiscordClient._send_answer(client, message, 'Blocked.\n' + R_HANDOFF_MARKER)
        client._ask_r.assert_not_awaited()

    async def test_r_reply_is_terminal_for_handoff_even_after_restart(self):
        parent = SimpleNamespace(author=SimpleNamespace(id=42), content='')
        message = SimpleNamespace(
            author=SimpleNamespace(id=R_BOT_USER_ID),
            reference=SimpleNamespace(resolved=None, message_id=100),
            channel=SimpleNamespace(fetch_message=AsyncMock(return_value=parent)),
        )
        for content in (
            format_r_handoff('Explain this', 321, 'https://discord.com/channels/1/2/3'),
            f'<@{R_BOT_USER_ID}> Jarvis fallback for human user 321',
        ):
            with self.subTest(content=content):
                parent.content = content
                self.assertTrue(await is_r_handoff_reply(message, 42))
        parent.content = 'An ordinary reply.'
        self.assertFalse(await is_r_handoff_reply(message, 42))

    async def test_rule_is_injected_for_humans_not_bot_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / 'memory.sqlite3', 10)
            await memory.initialize()
            assistant = JarvisAssistant(
                codex_binary=Path('/usr/bin/codex'), workdir=Path(directory),
                sandbox='read-only', timeout_seconds=10, persona='Jarvis', memory=memory,
            )
            assistant._run = AsyncMock(return_value=ParsedCodexEvents(
                answer='Done', session_id=None, error=None, model=None, finish_reason=None,
                input_tokens=None, cached_input_tokens=None, reasoning_tokens=None, output_tokens=None,
            ))
            for public, bot in ((False, False), (True, False), (False, True)):
                await assistant.respond('scope', 321, 'Explain this', restricted_public=public, restricted_bot=bot)
                prompt = assistant._run.await_args.args[1]
                self.assertEqual(R_HANDOFF_POLICY in prompt, not bot)
                if bot:
                    self.assertIn('Never delegate a bot request', prompt)


class PersistentHandoffLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'memory.sqlite3'
        self.memory = MemoryStore(self.path, 10)
        await self.memory.initialize()
        self.now = datetime(2026, 10, 27, 23, 59, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_exactly_three_globally_then_stop(self):
        for uid in (100, 200, 300):
            self.assertEqual(await self.memory.reserve_r_handoff(uid, now=self.now), 'reserved')
        self.assertEqual(await self.memory.reserve_r_handoff(400, now=self.now), 'daily_limit')

    async def test_count_and_dedup_survive_new_store_instance(self):
        for uid in (100, 200, 300):
            await self.memory.reserve_r_handoff(uid, now=self.now)
        restarted = MemoryStore(self.path, 10)
        await restarted.initialize()
        self.assertEqual(await restarted.reserve_r_handoff(400, now=self.now), 'daily_limit')
        self.assertEqual(await restarted.reserve_r_handoff(100, now=self.now), 'duplicate')

    async def test_failed_and_unconfirmed_sends_still_consume_quota(self):
        for uid in (100, 200, 300):
            await self.memory.reserve_r_handoff(uid, now=self.now)
        await self.memory.finish_r_handoff(100, delivered=False)
        await self.memory.finish_r_handoff(200, delivered=True)
        self.assertEqual(await self.memory.reserve_r_handoff(400, now=self.now), 'daily_limit')

    async def test_midnight_utc_reset_and_timezone_conversion(self):
        for uid in (100, 200, 300):
            await self.memory.reserve_r_handoff(uid, now=self.now)
        same_day = datetime(2026, 10, 28, 1, 59, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(await self.memory.reserve_r_handoff(400, now=same_day), 'daily_limit')
        tomorrow = self.now + timedelta(minutes=1)
        self.assertEqual(await self.memory.reserve_r_handoff(400, now=tomorrow), 'reserved')
        self.assertEqual(await self.memory.reserve_r_handoff(100, now=tomorrow), 'duplicate')

    async def test_independent_concurrent_connections_cannot_exceed_three(self):
        def reserve(uid):
            return asyncio.run(MemoryStore(self.path, 10).reserve_r_handoff(uid, now=self.now))
        outcomes = await asyncio.gather(*(asyncio.to_thread(reserve, uid) for uid in range(20)))
        self.assertEqual(outcomes.count('reserved'), 3)
        self.assertEqual(outcomes.count('daily_limit'), 17)

    async def test_migration_is_idempotent_and_does_not_clear_existing_count(self):
        await self.memory.reserve_r_handoff(100, now=self.now)
        await self.memory.initialize()
        self.assertEqual(await self.memory.reserve_r_handoff(100, now=self.now), 'duplicate')

    async def test_forget_does_not_reset_daily_budget(self):
        for uid in (100, 200, 300):
            await self.memory.reserve_r_handoff(uid, now=self.now)
        await self.memory.clear('some-user-scope')
        self.assertEqual(await self.memory.reserve_r_handoff(400, now=self.now), 'daily_limit')

    async def test_delivery_wiring_shares_budget_across_users_and_channels(self):
        sends = []
        for source in range(10, 14):
            client, message = HandoffDeliveryTests().setup_message()
            client.assistant.memory = MemoryStore(self.path, 10)
            message.id = source
            message.author.id = source * 10
            message.channel.id = source * 100
            sends.append(await JarvisDiscordClient._ask_r(client, message, 'Summarise https://example.com'))
        self.assertEqual(sends, [True, True, True, False])
