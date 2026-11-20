import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from jarvis_bot.bot import JarvisDiscordClient
from jarvis_bot.memory import MemoryStore
from jarvis_bot.reply_gif import GIF_URL, SETTING_KEY, TARGET_USER_ID


class ReplyGifTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "memory.sqlite3"
        self.memory = MemoryStore(self.path, 10)
        await self.memory.initialize()
        await self.memory.set_runtime_setting(SETTING_KEY, "armed")
        self.client = SimpleNamespace(assistant=SimpleNamespace(memory=self.memory))
        self.message = SimpleNamespace(
            author=SimpleNamespace(id=TARGET_USER_ID, bot=False),
            reply=AsyncMock(), channel=SimpleNamespace(send=AsyncMock()),
        )

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def answer(self, prompt="what is a server", answer="A computer providing services."):
        await JarvisDiscordClient._send_answer(
            self.client, self.message, answer, handoff_prompt=prompt,
        )

    async def test_follows_complete_answer_and_does_not_repeat_after_restart(self):
        order = []
        self.message.reply.side_effect = lambda *a, **k: order.append("answer")
        self.message.channel.send.side_effect = lambda text, **k: order.append(text)
        await self.answer()
        self.assertEqual(order, ["answer", GIF_URL])
        self.assertFalse(self.message.channel.send.await_args.kwargs["suppress_embeds"])
        self.assertFalse(self.message.channel.send.await_args.kwargs["allowed_mentions"].everyone)
        self.client.assistant.memory = MemoryStore(self.path, 10)
        await self.answer()
        self.assertEqual(order, ["answer", GIF_URL, "answer"])

    async def test_other_users_bots_greetings_and_commands_preserve_trigger(self):
        for user_id, bot, prompt in (
            (123, False, "why?"), (TARGET_USER_ID, True, "why?"),
            (TARGET_USER_ID, False, "thanks"), (TARGET_USER_ID, False, "hello"),
            (TARGET_USER_ID, False, "/help"),
        ):
            self.message.author.id, self.message.author.bot = user_id, bot
            await self.answer(prompt)
            self.assertEqual(await self.memory.get_runtime_setting(SETTING_KEY), "armed")
        self.message.channel.send.assert_not_awaited()

    async def test_failed_answer_does_not_consume_trigger(self):
        self.message.reply.side_effect = RuntimeError("answer failed")
        with self.assertRaises(RuntimeError):
            await self.answer()
        self.assertEqual(await self.memory.get_runtime_setting(SETTING_KEY), "armed")

    async def test_ambiguous_gif_failure_is_never_retried(self):
        self.message.channel.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason="Unavailable"), "Unavailable",
        )
        with self.assertLogs("jarvis_bot.reply_gif", level="WARNING"):
            await self.answer()
        self.assertIsNone(await self.memory.get_runtime_setting(SETTING_KEY))
        await self.answer()
        self.message.channel.send.assert_awaited_once()

    async def test_two_answers_only_send_one_gif(self):
        await asyncio.gather(self.answer(), self.answer())
        self.message.channel.send.assert_awaited_once()

    async def test_gif_follows_all_answer_chunks(self):
        order = []
        self.message.reply.side_effect = lambda text, **k: order.append(text)
        self.message.channel.send.side_effect = lambda text, **k: order.append(text)
        await self.answer(answer="Detail. " * 500)
        self.assertEqual(order[-1], GIF_URL)
        self.assertGreater(len(order), 2)

    async def test_unarmed_setting_does_not_send(self):
        await self.memory.set_runtime_setting(SETTING_KEY, "disabled")
        await self.answer()
        self.message.channel.send.assert_not_awaited()
        self.assertEqual(await self.memory.get_runtime_setting(SETTING_KEY), "disabled")
