import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from jarvis_bot.assistant import JarvisAssistant, ParsedCodexEvents
from jarvis_bot.bot import JarvisDiscordClient
from jarvis_bot.memory import MemoryStore
from jarvis_bot.public_access import PublicDecision, classify_public_request
from jarvis_bot.public_images import public_image_answer


THREAD = '01a1c1cf-e9a6-7ec0-b97d-4158937541cb'


class PublicImagePolicyTests(unittest.TestCase):
    def test_creation_and_edits_pass_the_action_filter(self):
        for prompt in ('Create an image of a turtle astronaut',
                       'Can you create a portrait of Fluffy?',
                       'Edit this photo to make the sky purple',
                       'Draw a turtle'):
            with self.subTest(prompt=prompt):
                self.assertIs(classify_public_request(prompt), PublicDecision.ALLOW)

    def test_host_and_account_actions_still_fail(self):
        for prompt in ('Create an image and restart the server',
                       'Create a portrait and email it to everyone',
                       'Create a picture using http://127.0.0.1:8080/',
                       'Create an image after reading my files',
                       'Delete an image from /home/your-user',
                       'Create an image and delete the database'):
            with self.subTest(prompt=prompt):
                self.assertIs(classify_public_request(prompt), PublicDecision.DENY_ACTION)

    def test_only_real_images_in_this_turns_directory_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / THREAD
            current.mkdir()
            good = current / 'good.png'
            Image.new('RGB', (2, 2), 'blue').save(good)
            old = root / 'old.png'
            old.write_bytes(good.read_bytes())
            corrupt = current / 'corrupt.png'
            corrupt.write_bytes(b'\x89PNG\r\n\x1a\ninvalid')
            symlink = current / 'link.png'
            symlink.symlink_to(old)
            answer = 'Done.\n' + '\n'.join(f'[[JARVIS_IMAGE:{p}]]' for p in (old, corrupt, symlink, good, good))
            self.assertEqual(public_image_answer(answer, THREAD, root=root), ('Done.', [good]))
            for thread in (None, '../', 'invented-thread'):
                self.assertEqual(public_image_answer(answer, thread, root=root), ('Done.', []))

    def test_symlinked_thread_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = root / 'private'
            other.mkdir()
            image = other / 'image.png'
            Image.new('RGB', (2, 2)).save(image)
            (root / THREAD).symlink_to(other, target_is_directory=True)
            answer = f'[[JARVIS_IMAGE:{root / THREAD / "image.png"}]]'
            self.assertEqual(public_image_answer(answer, THREAD, root=root), ('', []))


class PublicImageAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_generation_is_ephemeral_without_owner_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryStore(Path(directory) / 'memory.sqlite3', 20)
            await memory.initialize()
            await memory.set_session_id('scope', 'owner-session')
            assistant = JarvisAssistant(
                codex_binary=Path('/usr/bin/codex'), workdir=Path(directory),
                sandbox='danger-full-access', timeout_seconds=900,
                persona='OWNER HOST AUTHORITY', memory=memory,
            )
            assistant._run = AsyncMock(return_value=ParsedCodexEvents(
                answer='Done.\n[[JARVIS_IMAGE:/old/image.png]]', session_id=THREAD,
                error=None, model=None, finish_reason=None, input_tokens=None,
                cached_input_tokens=None, reasoning_tokens=None, output_tokens=None,
            ))
            answer = await assistant.respond('scope', 222, 'Create an image of a turtle',
                                             restricted_public=True, mode='public_chat')
            command, prompt = assistant._run.await_args.args
            self.assertEqual(command[command.index('image_generation')-1], '--enable')
            for feature in ('shell_tool', 'apps', 'mcp_2026_07_28', 'view_image', 'computer_use'):
                self.assertEqual(command[command.index(feature)-1], '--disable')
            self.assertIn('--ephemeral', command)
            self.assertIn('--ignore-user-config', command)
            self.assertIn('read-only', command)
            self.assertNotIn('OWNER HOST AUTHORITY', prompt)
            self.assertIn('OWNER-AUTHORISED PUBLIC IMAGE CAPABILITY', prompt)
            self.assertIn('PUBLIC USER QUESTION:', prompt)
            self.assertEqual(answer, 'Done.')
            self.assertEqual(await memory.get_session_id('scope'), 'owner-session')
            self.assertNotIn('JARVIS_IMAGE', (await memory.recent('scope', 20))[-1].content)
            await assistant.respond('scope', 222, 'hello', restricted_public=True, mode='voice')
            command, prompt = assistant._run.await_args.args
            self.assertEqual(command[command.index('image_generation')-1], '--disable')
            self.assertNotIn('OWNER-AUTHORISED PUBLIC IMAGE CAPABILITY', prompt)


class PublicImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setup_request(self, remaining=90):
        message = SimpleNamespace(
            id=10, author=SimpleNamespace(id=222), guild=None, reference=None,
            attachments=[], content='Jarvis create an image of a turtle',
            channel=SimpleNamespace(id=2, typing=lambda: asyncio.Lock()), reply=AsyncMock(),
        )
        client = SimpleNamespace(
            settings=SimpleNamespace(
                public_max_prompt_chars=2000, public_min_five_hour_remaining_percent=25,
                public_guild_context_messages=10, chat_context_max_chars=1000,
                public_memory_messages=20,
            ),
            _public_request_lock=asyncio.Lock(), _five_hour_remaining=AsyncMock(return_value=remaining),
            _send_answer=AsyncMock(), _ask_r=AsyncMock(), _visible_text_channel_ids=lambda _: set(),
            assistant=SimpleNamespace(
                memory=SimpleNamespace(recent_guild_messages=AsyncMock(return_value=[])),
                respond=AsyncMock(return_value='Generated.'),
            ),
        )
        return client, message

    async def test_public_image_is_attached_to_the_request(self):
        client, message = self.setup_request()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'image.png'
            Image.new('RGB', (2, 2), 'blue').save(image)
            with patch('jarvis_bot.bot.image_answer', return_value=('Generated.', [image])):
                await JarvisDiscordClient._handle_public_request(client, message, 'Create an image of a turtle')
            kwargs = message.reply.await_args.kwargs
            self.assertEqual(len(kwargs['files']), 1)
            self.assertFalse(kwargs['mention_author'])
            self.assertEqual(kwargs['files'][0].filename, 'jarvis-image-1.png')
        self.assertTrue(client.assistant.respond.await_args.kwargs['restricted_public'])
        self.assertEqual(client.assistant.respond.await_args.kwargs['mode'], 'public_chat')
        client._send_answer.assert_not_awaited()

    async def test_low_or_unavailable_quota_prevents_generation(self):
        for remaining in (25, 0):
            client, message = self.setup_request(remaining)
            await JarvisDiscordClient._handle_public_request(client, message, 'Create an image of a turtle')
            client.assistant.respond.assert_not_awaited()
            self.assertIn('paused', message.reply.await_args.args[0])

    async def test_generation_failure_is_reported_without_a_false_attachment(self):
        client, message = self.setup_request()
        client.assistant.respond.return_value = 'The image tool is unavailable.'
        await JarvisDiscordClient._handle_public_request(client, message, 'Create an image of a turtle')
        self.assertIn('No image attachment was produced', client._send_answer.await_args.args[1])
        message.reply.assert_not_awaited()
        client._ask_r.assert_not_awaited()

    async def test_host_actions_do_not_reach_the_model(self):
        client, message = self.setup_request()
        await JarvisDiscordClient._handle_public_request(client, message, 'Create an image and restart the server')
        client.assistant.respond.assert_not_awaited()
