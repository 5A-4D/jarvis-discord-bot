import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from jarvis_bot.dm_copy import copy_incoming_dm, dm_copy_text


class DMCopyTests(unittest.IsolatedAsyncioTestCase):
    def setup_copy(self):
        owner = SimpleNamespace(send=AsyncMock())
        client = SimpleNamespace(
            settings=SimpleNamespace(
                dm_copy_owner_id=100, authorized_user_ids=frozenset({100}),
                work_calendar_ics_url="",
            ),
            get_user=lambda _: owner,
            fetch_user=AsyncMock(return_value=owner),
        )
        message = SimpleNamespace(
            id=123, guild=None, webhook_id=None,
            author=SimpleNamespace(id=200, bot=False, name="sender"),
            created_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
            content="Yes, please!", reference=SimpleNamespace(message_id=122),
            attachments=[], stickers=[],
        )
        return client, message, owner

    async def test_plain_reply_and_attachment_only_dm_are_copied(self):
        client, message, owner = self.setup_copy()
        await copy_incoming_dm(client, message)
        sent = owner.send.await_args.args[0]
        self.assertIn("Yes, please!", sent)
        self.assertIn("user ID: 200", sent)
        self.assertIn("Reply to message: 122", sent)
        self.assertTrue(owner.send.await_args.kwargs["suppress_embeds"])
        self.assertEqual(owner.send.await_args.kwargs["allowed_mentions"].to_dict(),
                         discord.AllowedMentions.none().to_dict())
        message.content = ""
        message.attachments = [SimpleNamespace(
            filename="photo.png", content_type="image/png", url="https://cdn.example/photo.png",
        )]
        await copy_incoming_dm(client, message)
        self.assertIn("https://cdn.example/photo.png", owner.send.await_args.args[0])

    async def test_excludes_server_owner_bot_webhook_and_unauthorized_recipient(self):
        for case in ("server", "owner", "bot", "webhook", "unauthorized"):
            with self.subTest(case=case):
                client, message, owner = self.setup_copy()
                if case == "server":
                    message.guild = SimpleNamespace(id=5)
                elif case == "owner":
                    message.author.id = 100
                elif case == "bot":
                    message.author.bot = True
                elif case == "webhook":
                    message.webhook_id = 7
                else:
                    client.settings.authorized_user_ids = frozenset({300})
                await copy_incoming_dm(client, message)
                owner.send.assert_not_awaited()
                client.fetch_user.assert_not_awaited()

    async def test_long_unicode_content_is_complete_and_within_discord_limit(self):
        client, message, owner = self.setup_copy()
        message.content = "😀  line\n" * 2000
        await copy_incoming_dm(client, message)
        chunks = [call.args[0] for call in owner.send.await_args_list]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), dm_copy_text(message))
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 1900 for chunk in chunks))

    async def test_blocked_dms_log_no_content_and_do_not_raise(self):
        client, message, owner = self.setup_copy()
        error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                  {"message": "private response body", "code": 50007})
        owner.send.side_effect = error
        with self.assertLogs("jarvis_bot.dm_copy", level="WARNING") as logged:
            await copy_incoming_dm(client, message)
        self.assertNotIn(message.content, "".join(logged.output))
        self.assertNotIn("private response body", "".join(logged.output))
        self.assertIn("source=123 owner=100", "".join(logged.output))

    async def test_fetches_uncached_owner(self):
        client, message, owner = self.setup_copy()
        client.get_user = lambda _: None
        await copy_incoming_dm(client, message)
        client.fetch_user.assert_awaited_once_with(100)
        owner.send.assert_awaited_once()

    def test_calendar_secrets_raw_data_and_calendar_links_are_withheld(self):
        _, message, _ = self.setup_copy()
        secret = "synthetic-secret-token-123"
        feed = f"https://outlook.office365.com/owa/calendar/{secret}/calendar.ics"
        message.content = f"Please check {feed} token {secret}"
        message.attachments = [SimpleNamespace(
            filename="schedule.ics", content_type="text/calendar", url="https://cdn.example/raw.ics",
        )]
        text = dm_copy_text(message, feed)
        self.assertNotIn(secret, text)
        self.assertNotIn(feed, text)
        self.assertNotIn("https://cdn.example/raw.ics", text)
        message.content = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Private\nEND:VCALENDAR"
        self.assertEqual(dm_copy_text(message, feed), "[Calendar content withheld]")


if __name__ == "__main__":
    unittest.main()
