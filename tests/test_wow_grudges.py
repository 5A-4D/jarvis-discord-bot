import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jarvis_bot.config import Settings
from jarvis_bot.bot import JarvisDiscordClient
from jarvis_bot.wow_grudges import GrudgeError, GrudgeRequest, GrudgeStore, handle_request, parse_request, render


class GrudgeParsingTests(unittest.TestCase):
    def test_explicit_and_wow_channel_commands(self):
        for text, implicit in (
            ('grudge add Silent Wind — Ganked me', False),
            ('add Silent Wind to the wow grudge list because Ganked me', False),
            ('add Silent Wind. Ganked me', True),
            ('/grudge add Silent Wind for Ganked me', False),
        ):
            self.assertEqual(parse_request(text, implicit_add=implicit), GrudgeRequest('Silent Wind', 'Ganked me'))
        for text in ('grudge list', 'wow grudge list', 'show the grudge list'):
            self.assertEqual(parse_request(text), GrudgeRequest())
        self.assertIsNone(parse_request('add Silent Wind — Ganked me'))
        self.assertIsNone(parse_request('remove Silent Wind from the grudge list'))
        self.assertIsNone(parse_request('What is a grudge?'))

    def test_ambiguous_references_and_invalid_or_long_input_do_not_become_names(self):
        for text in ('grudge add this guy too', 'grudge add /home/your-user/.env',
                     'grudge add <@123>', 'grudge add A — ' + 'x' * 501,
                     'grudge add ' + 'a' * 81):
            with self.subTest(text=text[:60]), self.assertRaises(GrudgeError):
                parse_request(text)

    def test_render_escapes_markdown_and_splits_long_lists(self):
        pages = render([{'name': 'Name', 'reason': '**quoted** ' + 'x' * 480} for _ in range(20)])
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 1900 for page in pages))
        self.assertIn(r'\*\*quoted\*\*', pages[0])


class GrudgeStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_entries_survive_restarts_and_duplicates_preserve_the_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wow-grudges.json'
            store = GrudgeStore(path, 10)
            self.assertTrue(await store.add(GrudgeRequest('Silent Wind', 'Original'), 1, 100))
            restarted = GrudgeStore(path, 10)
            self.assertFalse(await restarted.add(GrudgeRequest('silent  wind', 'Replacement'), 2, 200))
            entries = await restarted.entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]['reason'], 'Original')
            self.assertEqual(entries[0]['added_by'], '1')
            self.assertEqual(entries[0]['message_id'], '100')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_concurrent_writers_do_not_lose_additions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wow-grudges.json'
            await asyncio.gather(*(GrudgeStore(path, 10).add(GrudgeRequest('Player ' + chr(65+i), 'Test'), i, i) for i in range(20)))
            self.assertEqual(len(await GrudgeStore(path, 10).entries()), 20)

    async def test_corrupt_or_other_guild_data_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wow-grudges.json'
            for content in ('{broken', json.dumps({'version': 1, 'guild_id': '999', 'entries': []})):
                path.write_text(content)
                with self.assertRaises(GrudgeError):
                    await GrudgeStore(path, 10).add(GrudgeRequest('Player'), 1, 100)
                self.assertEqual(path.read_text(), content)

    async def test_disk_failure_does_not_claim_or_replace_an_addition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wow-grudges.json'
            store = GrudgeStore(path, 10)
            await store.add(GrudgeRequest('Existing'), 1, 1)
            before = path.read_bytes()
            with patch('jarvis_bot.wow_grudges.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    await store.add(GrudgeRequest('New Player'), 2, 2)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob('.wow-grudges-*')), [])


class GrudgeRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setup_request(self, root, *, public=True, guild=10, channel=20):
        client = SimpleNamespace(
            settings=SimpleNamespace(wow_grudge_guild_id=10, wow_grudge_channel_id=20,
                                     wow_grudge_public_enabled=public),
            _wow_grudges=GrudgeStore(root / 'wow-grudges.json', 10),
            _public_channel_allowed=lambda _: channel == 20,
            assistant=SimpleNamespace(respond=AsyncMock()),
        )
        message = SimpleNamespace(id=100, author=SimpleNamespace(id=2, bot=False),
                                  guild=SimpleNamespace(id=guild) if guild else None,
                                  channel=SimpleNamespace(id=channel), reply=AsyncMock(), webhook_id=None)
        return client, message

    async def test_public_member_can_add_and_view_without_using_the_model(self):
        with tempfile.TemporaryDirectory() as directory:
            client, message = self.setup_request(Path(directory))
            self.assertTrue(await handle_request(client, message, 'add Silent Wind — Ganked me', is_owner=False))
            self.assertIn('Added **Silent Wind**', message.reply.await_args.args[0])
            await handle_request(client, message, 'grudge list', is_owner=False)
            self.assertIn('Ganked me', message.reply.await_args.args[0])
            self.assertFalse(message.reply.await_args.kwargs['allowed_mentions'].everyone)
            client.assistant.respond.assert_not_awaited()

    async def test_discord_dispatch_routes_a_non_owner_to_the_shared_store(self):
        with tempfile.TemporaryDirectory() as directory:
            client, message = self.setup_request(Path(directory))
            client.user = SimpleNamespace(id=999)
            client.settings.authorized_user_ids = frozenset({1})
            client.settings.trigger = 'Jarvis'
            client._channel_allowed = lambda _: True
            client._store_discord_message = AsyncMock()
            client._is_reply_to_self = AsyncMock(return_value=False)
            message.content = 'Jarvis grudge add Silent Wind — Ganked me'
            message.attachments = []
            await JarvisDiscordClient.on_message(client, message)
            self.assertEqual((await client._wow_grudges.entries())[0]['name'], 'Silent Wind')
            client.assistant.respond.assert_not_awaited()

    async def test_disabled_public_access_wrong_guild_dm_and_channel_do_not_add(self):
        for kwargs in ({'public': False}, {'guild': 99}, {'guild': None}, {'channel': 99}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                client, message = self.setup_request(Path(directory), **kwargs)
                await handle_request(client, message, 'grudge add Silent Wind — Test', is_owner=False)
                self.assertEqual(await client._wow_grudges.entries(), [])

    async def test_bots_and_webhooks_do_not_add(self):
        for bot, webhook in ((True, None), (False, 123)):
            with tempfile.TemporaryDirectory() as directory:
                client, message = self.setup_request(Path(directory))
                message.author.bot, message.webhook_id = bot, webhook
                self.assertFalse(await handle_request(client, message, 'grudge add Silent Wind', is_owner=False))
                self.assertEqual(await client._wow_grudges.entries(), [])

    async def test_write_failure_produces_a_clear_failure_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            client, message = self.setup_request(Path(directory))
            client._wow_grudges.add = AsyncMock(side_effect=OSError('disk error'))
            await handle_request(client, message, 'grudge add Silent Wind', is_owner=False)
            self.assertIn('No addition was confirmed', message.reply.await_args.args[0])


class GrudgeConfigurationTests(unittest.TestCase):
    def load(self, **values):
        environment = {'DISCORD_BOT_TOKEN': 'synthetic', 'AUTHORIZED_USER_IDS': '1', **values}
        with patch.dict(os.environ, environment, clear=True), patch('jarvis_bot.config.load_dotenv'):
            return Settings.from_env()

    def test_opt_in_requires_explicit_guild_and_wow_channel(self):
        self.assertFalse(self.load().wow_grudge_public_enabled)
        with self.assertRaises(ValueError):
            self.load(WOW_GRUDGE_PUBLIC_ENABLED='true')
        settings = self.load(WOW_GRUDGE_PUBLIC_ENABLED='true', WOW_GRUDGE_GUILD_ID='10', WOW_GRUDGE_CHANNEL_ID='20')
        self.assertTrue(settings.wow_grudge_public_enabled)
        self.assertEqual((settings.wow_grudge_guild_id, settings.wow_grudge_channel_id), (10, 20))
