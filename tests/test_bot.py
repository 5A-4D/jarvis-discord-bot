import asyncio
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jarvis_bot.bot import (
    BOT_CONVERSATION_END_WORD,
    JarvisDiscordClient,
    avatar_command_action,
    apply_user_response_prefix,
    bot_access_command_action,
    bot_usage_allows_invocation,
    extract_mention_prompt,
    extract_prompt,
    format_channel_context,
    invocation_prompt,
    is_bot_conversation_end_message,
    is_usage_command,
    is_usage_exhaustion_error,
    normalize_bot_conversation_answer,
    reply_mentions_user,
    reply_chain_root_id,
    split_message,
    discord_progress_text,
    strict_public_invocation_prompt,
    supported_avatar_attachment,
    usage_exhausted_message,
    voice_message_attachment,
)
from jarvis_bot.config import DEFAULT_PERSONA
from jarvis_bot.memory import ChannelMessage, VoiceSegment
from jarvis_bot.public_access import PublicDecision, classify_public_request
from jarvis_bot.voice_notes import VoiceNotesQuery


class TriggerTests(unittest.TestCase):
    def test_extracts_prompt_case_insensitively(self) -> None:
        self.assertEqual(extract_prompt("jArViS, status report", "Jarvis"), "status report")

    def test_accepts_short_alias_and_common_typos(self) -> None:
        for name in ("jar", "jarvas", "javis", "jarivs", "jarviss"):
            with self.subTest(name=name):
                self.assertEqual(extract_prompt(f"{name}, status", "Jarvis"), "status")

    def test_accepts_trigger_anywhere_and_preserves_context(self) -> None:
        self.assertEqual(
            extract_prompt("Hey Jarvis, can you check something?", "Jarvis"),
            "Hey can you check something?",
        )
        self.assertEqual(
            extract_prompt(
                "I wonder how many boats are in the ocean, Jarvis check that.",
                "Jarvis",
            ),
            "I wonder how many boats are in the ocean, check that.",
        )

    def test_accepts_typo_away_from_start(self) -> None:
        self.assertEqual(
            extract_prompt("Could you check this, jarvas?", "Jarvis"),
            "Could you check this,",
        )

    def test_does_not_match_longer_word(self) -> None:
        self.assertIsNone(extract_prompt("Jarvisian greeting", "Jarvis"))

    def test_does_not_match_short_similar_words(self) -> None:
        self.assertIsNone(extract_prompt("jars on the shelf", "Jarvis"))
        self.assertIsNone(extract_prompt("javascript help", "Jarvis"))

    def test_does_not_match_name_inside_url(self) -> None:
        self.assertIsNone(extract_prompt("Read https://jarvis.example/help", "Jarvis"))

    def test_empty_prompt(self) -> None:
        self.assertEqual(extract_prompt("Jarvis!", "Jarvis"), "")

    def test_extracts_prompt_from_standard_and_nickname_mentions(self) -> None:
        self.assertEqual(extract_mention_prompt("<@123> status", 123), "status")
        self.assertEqual(extract_mention_prompt("<@!123>, status", 123), "status")

    def test_extracts_prompt_when_mention_is_not_first(self) -> None:
        self.assertEqual(
            extract_mention_prompt("Could you help, <@123>?", 123),
            "Could you help?",
        )

    def test_rejects_mentions_of_other_users(self) -> None:
        self.assertIsNone(extract_mention_prompt("<@456> status", 123))

    def test_reply_content_becomes_prompt(self) -> None:
        self.assertEqual(
            invocation_prompt(
                "run the tests", "Jarvis", 123, is_reply_to_bot=True
            ),
            "run the tests",
        )

    def test_reply_ping_is_detected_from_discord_mentions(self) -> None:
        message = SimpleNamespace(mentions=[SimpleNamespace(id=123)])
        self.assertTrue(reply_mentions_user(message, 123))
        self.assertFalse(reply_mentions_user(message, 456))

    def test_reply_without_at_on_reply_does_not_ping(self) -> None:
        self.assertFalse(reply_mentions_user(SimpleNamespace(mentions=[]), 123))

    def test_non_invocation_is_ignored(self) -> None:
        self.assertIsNone(invocation_prompt("ordinary chat", "Jarvis", 123))

    def test_public_trigger_requires_exact_name_or_mention(self) -> None:
        self.assertEqual(
            strict_public_invocation_prompt("Jarvis, what time is it?", "Jarvis", 123),
            "what time is it?",
        )
        self.assertEqual(
            strict_public_invocation_prompt("<@123> what time is it?", "Jarvis", 123),
            "what time is it?",
        )
        self.assertIsNone(
            strict_public_invocation_prompt("Jarvas, what time is it?", "Jarvis", 123)
        )


class SplitMessageTests(unittest.TestCase):
    def test_short_message_is_unchanged(self) -> None:
        self.assertEqual(split_message("hello"), ["hello"])

    def test_long_message_is_split_within_limit(self) -> None:
        chunks = split_message("word " * 1_000, limit=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))

    def test_discord_progress_is_verbose_safe_and_within_limit(self) -> None:
        payload = {
            "active": [
                {
                    "request_id": "request-1",
                    "mode": "chat",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "events": [
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "summary": "Reasoning summary",
                            "detail": ("detail ``` " * 500),
                        }
                    ],
                }
            ],
            "last_completed": None,
        }

        rendered = discord_progress_text(payload)

        self.assertLessEqual(len(rendered), 2_000)
        self.assertTrue(rendered.startswith("```text\nJarvis Codex progress"))
        self.assertEqual(rendered.count("```"), 2)
        self.assertIn("earlier activity omitted", rendered)


class LiveVoiceResponseRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_response_is_sent_to_voice_channel_chat(self) -> None:
        voice_channel = SimpleNamespace(send=AsyncMock())
        voice_notes = SimpleNamespace(
            voice_chat_channel=AsyncMock(return_value=voice_channel),
            output_channel=AsyncMock(),
        )
        client = SimpleNamespace(voice_notes=voice_notes)

        await JarvisDiscordClient._send_live_voice_answer(
            client, "Speaker", "Hello from voice."
        )

        voice_notes.voice_chat_channel.assert_awaited_once_with()
        voice_notes.output_channel.assert_not_awaited()
        voice_channel.send.assert_awaited_once()
        sent_text = voice_channel.send.await_args.args[0]
        self.assertIn("Speaker asked in VC", sent_text)
        self.assertIn("Hello from voice.", sent_text)
        self.assertTrue(voice_channel.send.await_args.kwargs["suppress_embeds"])


class VoiceSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unbounded_topic_query_reads_all_retained_segments(self) -> None:
        segment = VoiceSegment(
            id=1,
            session_id="session",
            guild_id=100,
            voice_channel_id=200,
            user_id=300,
            user_name="Speaker",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:01:00+00:00",
            transcript="deployment discussion",
        )
        memory = SimpleNamespace(
            recent_voice_segments=AsyncMock(return_value=[segment])
        )
        assistant = SimpleNamespace(
            memory=memory,
            respond=AsyncMock(return_value="summary"),
        )
        client = SimpleNamespace(
            settings=SimpleNamespace(
                live_voice_summary_max_minutes=10_080,
                live_voice_channel_id=200,
                live_voice_retention_days=0,
                chat_context_max_chars=24_000,
                public_memory_messages=20,
            ),
            assistant=assistant,
            get_guild=lambda guild_id: SimpleNamespace(
                get_member=lambda user_id: SimpleNamespace(name="account_username")
            ),
            get_user=lambda user_id: None,
        )
        query = VoiceNotesQuery(
            since=None,
            until=datetime(2026, 9, 15, tzinfo=timezone.utc),
            label="all retained history",
            topic="deployment",
        )

        answer = await JarvisDiscordClient._generate_voice_summary(
            client,
            guild_id=100,
            user_id=300,
            query=query,
        )

        self.assertEqual(answer, "summary")
        self.assertIsNone(memory.recent_voice_segments.await_args.kwargs["since"])
        response_kwargs = assistant.respond.await_args.kwargs
        self.assertIn("account_username (user 300)", response_kwargs["channel_context"])
        self.assertNotIn("Speaker (user 300)", response_kwargs["channel_context"])
        self.assertIn("exact Discord usernames", response_kwargs["prompt"])


class AnswerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_delivery_suppresses_discord_embeds(self) -> None:
        message = SimpleNamespace(
            reply=AsyncMock(),
            channel=SimpleNamespace(send=AsyncMock()),
        )

        await JarvisDiscordClient._send_answer(
            SimpleNamespace(), message, "https://example.com/details"
        )

        message.reply.assert_awaited_once()
        self.assertTrue(message.reply.await_args.kwargs["suppress_embeds"])

    async def test_answer_delivery_preserves_generated_text(self) -> None:
        message = SimpleNamespace(
            author=SimpleNamespace(id=900000000000000005),
            reply=AsyncMock(),
            channel=SimpleNamespace(send=AsyncMock()),
        )

        await JarvisDiscordClient._send_answer(
            SimpleNamespace(), message, "The answer is 42."
        )

        self.assertEqual(message.reply.await_args.args[0], "The answer is 42.")

    def test_r_prefix_is_added_for_configured_user(self) -> None:
        self.assertEqual(
            apply_user_response_prefix("Evidence noted.", 139, frozenset({139})),
            "right—Evidence noted.",
        )

    def test_r_prefix_is_not_duplicated(self) -> None:
        self.assertEqual(
            apply_user_response_prefix("Records checked.", 139, frozenset({139})),
            "Records checked.",
        )

    def test_r_prefix_does_not_affect_other_users(self) -> None:
        self.assertEqual(
            apply_user_response_prefix("Evidence noted.", 200, frozenset({139})),
            "Evidence noted.",
        )

    def test_terminal_handshake_is_not_decorated_with_r_prefix(self) -> None:
        summary = "ADJOURN"
        self.assertEqual(
            apply_user_response_prefix(summary, 139, frozenset({139})),
            summary,
        )


class BotConversationEndTests(unittest.TestCase):
    def test_recognises_exact_end_word_case_insensitively(self) -> None:
        self.assertTrue(is_bot_conversation_end_message("adjourn"))
        self.assertTrue(is_bot_conversation_end_message("  ADJOURN  "))
        self.assertTrue(is_bot_conversation_end_message("adjourned"))
        self.assertTrue(is_bot_conversation_end_message("  ADJOURNED  "))

    def test_rejects_prefixed_end_word(self) -> None:
        self.assertFalse(is_bot_conversation_end_message("right—ADJOURN"))

    def test_rejects_summary_with_end_word_on_final_line(self) -> None:
        self.assertFalse(
            is_bot_conversation_end_message(
                "Summary: Collect three keys, then clear three towers.\n\nADJOURN"
            )
        )

    def test_does_not_end_on_sentence_containing_word(self) -> None:
        self.assertFalse(
            is_bot_conversation_end_message("The meeting is adjourned now.")
        )

    def test_normalises_terminal_answer_to_exact_word(self) -> None:
        self.assertEqual(
            normalize_bot_conversation_answer(
                "Summary: Agreement reached.\n\nADJOURN"
            ),
            "ADJOURN",
        )
        self.assertEqual(
            normalize_bot_conversation_answer(
                "Summary: Agreement reached.\n\nADJOURNED"
            ),
            "ADJOURN",
        )

    def test_does_not_normalise_non_terminal_answer(self) -> None:
        answer = "One material question remains."
        self.assertEqual(normalize_bot_conversation_answer(answer), answer)


class ChannelContextTests(unittest.TestCase):
    def test_formats_metadata_and_keeps_most_recent_messages(self) -> None:
        messages = [
            ChannelMessage(
                message_id=index,
                guild_id=1,
                guild_name="Server",
                channel_id=2,
                channel_name="general",
                user_id=10 + index,
                user_name=f"User {index}",
                content=f"message {index}",
                created_at=f"time-{index}",
            )
            for index in range(1, 4)
        ]
        context = format_channel_context(messages, max_chars=80)
        self.assertIn("User 3 (user 13): message 3", context)
        self.assertNotIn("User 1 (user 11): message 1", context)


class VoiceMessageTests(unittest.TestCase):
    def test_selects_voice_message_attachment(self) -> None:
        ordinary = SimpleNamespace(is_voice_message=lambda: False)
        voice = SimpleNamespace(is_voice_message=lambda: True)
        self.assertIs(voice_message_attachment([ordinary, voice]), voice)

    def test_returns_none_without_voice_message(self) -> None:
        ordinary = SimpleNamespace(is_voice_message=lambda: False)
        self.assertIsNone(voice_message_attachment([ordinary]))


class ReplyChainTests(unittest.IsolatedAsyncioTestCase):
    @dataclass
    class FakeMessage:
        message_id: int
        author_id: int
        parent: "ReplyChainTests.FakeMessage | None" = None
        channel: object | None = None

        @property
        def author(self) -> SimpleNamespace:
            return SimpleNamespace(id=self.author_id)

        @property
        def id(self) -> int:
            return self.message_id

        @property
        def reference(self) -> SimpleNamespace | None:
            if self.parent is None:
                return None
            return SimpleNamespace(
                message_id=self.parent.message_id,
                resolved=self.parent,
            )

    async def test_finds_oldest_resolved_message_as_chain_id(self) -> None:
        first = self.FakeMessage(1, 200)
        middle = self.FakeMessage(2, 100, first)
        incoming = self.FakeMessage(3, 200, middle)

        root_id = await reply_chain_root_id(incoming)

        self.assertEqual(root_id, 1)


class BotAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_bot_handshake_is_stored_without_reply(self) -> None:
        message = SimpleNamespace(
            id=10,
            author=SimpleNamespace(id=200, bot=True),
            webhook_id=None,
            channel=SimpleNamespace(id=1),
            content="ADJOURN",
            attachments=[],
        )
        client = SimpleNamespace(
            _channel_allowed=lambda channel_id: True,
            _store_discord_message=AsyncMock(),
            _bot_access_enabled=AsyncMock(return_value=True),
            _handle_bot_request=AsyncMock(),
            user=SimpleNamespace(id=100),
        )

        await JarvisDiscordClient.on_message(client, message)

        client._store_discord_message.assert_awaited_once_with(message)
        client._bot_access_enabled.assert_not_awaited()
        client._handle_bot_request.assert_not_awaited()

    async def test_logs_then_ignores_bot_message_when_access_is_disabled(self) -> None:
        message = SimpleNamespace(
            author=SimpleNamespace(id=200, bot=True), webhook_id=None,
            channel=SimpleNamespace(id=1),
        )
        client = SimpleNamespace(
            _channel_allowed=lambda channel_id: True,
            _store_discord_message=AsyncMock(),
            _bot_access_enabled=AsyncMock(return_value=False),
            user=SimpleNamespace(id=100),
        )
        await JarvisDiscordClient.on_message(client, message)
        client._store_discord_message.assert_awaited_once_with(message)

    async def test_even_enabled_bot_cannot_invoke_exact_trigger(self) -> None:
        message = SimpleNamespace(
            id=10,
            author=SimpleNamespace(id=200, bot=True),
            webhook_id=None,
            channel=SimpleNamespace(id=1),
            content="Jarvis, discuss the API design",
            attachments=[],
        )
        client = SimpleNamespace(
            _channel_allowed=lambda channel_id: True,
            _store_discord_message=AsyncMock(),
            _bot_access_enabled=AsyncMock(return_value=True),
            _is_reply_to_self=AsyncMock(return_value=False),
            _handle_bot_request=AsyncMock(),
            user=SimpleNamespace(id=100),
            settings=SimpleNamespace(
                trigger="Jarvis", authorized_user_ids=frozenset({300})
            ),
        )

        await JarvisDiscordClient.on_message(client, message)

        client._handle_bot_request.assert_not_awaited()
        client._is_reply_to_self.assert_not_awaited()

    async def test_configured_bot_reply_cannot_start_or_continue_conversation(self) -> None:
        message = SimpleNamespace(
            id=10,
            author=SimpleNamespace(id=200, bot=True),
            webhook_id=None,
            channel=SimpleNamespace(id=1),
            content="The evidence was planted.",
            attachments=[],
            mentions=[],
        )
        client = SimpleNamespace(
            _channel_allowed=lambda channel_id: True,
            _store_discord_message=AsyncMock(),
            _bot_access_enabled=AsyncMock(return_value=True),
            _is_reply_to_self=AsyncMock(return_value=True),
            _handle_bot_request=AsyncMock(),
            user=SimpleNamespace(id=100),
            settings=SimpleNamespace(
                trigger="Jarvis",
                authorized_user_ids=frozenset({300}),
                bot_conversation_user_ids=frozenset({200}),
            ),
        )

        await JarvisDiscordClient.on_message(client, message)

        client._handle_bot_request.assert_not_awaited()
        client._is_reply_to_self.assert_not_awaited()

    async def test_r_mentions_are_context_only_without_a_reply_reference(self) -> None:
        message = SimpleNamespace(
            id=10, author=SimpleNamespace(id=900000000000000001, bot=True),
            webhook_id=None, channel=SimpleNamespace(id=1),
            content='<@1547293335436402750> Jarvis, ask R to try again',
            reference=None,
        )
        client = SimpleNamespace(
            _channel_allowed=lambda _: True, _store_discord_message=AsyncMock(),
            _handle_bot_request=AsyncMock(), _bot_access_enabled=AsyncMock(return_value=True),
            user=SimpleNamespace(id=1547293335436402750),
        )
        await JarvisDiscordClient.on_message(client, message)
        client._store_discord_message.assert_awaited_once_with(message)
        client._handle_bot_request.assert_not_awaited()
        client._bot_access_enabled.assert_not_awaited()

    async def test_legacy_bots_on_cannot_reenable_bot_conversations(self) -> None:
        message = SimpleNamespace(reply=AsyncMock())
        client = SimpleNamespace(_set_bot_access_enabled=AsyncMock())
        await JarvisDiscordClient._handle_bot_access_command(client, message, 'on')
        client._set_bot_access_enabled.assert_not_awaited()
        self.assertIn('disabled by owner policy', message.reply.await_args.args[0])

    async def test_runtime_or_environment_flags_cannot_override_boundary(self) -> None:
        self.assertFalse(await JarvisDiscordClient._bot_access_enabled(SimpleNamespace()))

    async def test_ignores_own_bot_messages_even_when_enabled(self) -> None:
        message = SimpleNamespace(
            author=SimpleNamespace(id=100, bot=True), webhook_id=None,
            channel=SimpleNamespace(id=1),
        )
        client = SimpleNamespace(
            _channel_allowed=lambda channel_id: True,
            _store_discord_message=AsyncMock(),
            _bot_access_enabled=AsyncMock(return_value=True),
            user=SimpleNamespace(id=100),
        )

        await JarvisDiscordClient.on_message(client, message)

        client._store_discord_message.assert_awaited_once_with(message)

    async def test_logs_then_ignores_webhook_messages_at_the_boundary(self) -> None:
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False), webhook_id=123,
            channel=SimpleNamespace(id=1),
        )
        client = SimpleNamespace(
            _channel_allowed=lambda channel_id: True,
            _store_discord_message=AsyncMock(),
        )
        await JarvisDiscordClient.on_message(client, message)
        client._store_discord_message.assert_awaited_once_with(message)


class PublicChannelAccessTests(unittest.TestCase):
    def test_global_public_access_allows_every_channel(self) -> None:
        client = SimpleNamespace(
            settings=SimpleNamespace(
                public_access_all_channels=True,
                public_channel_ids=frozenset(),
            )
        )
        self.assertTrue(JarvisDiscordClient._public_channel_allowed(client, 999))

    def test_channel_allowlist_still_works_when_global_access_is_off(self) -> None:
        client = SimpleNamespace(
            settings=SimpleNamespace(
                public_access_all_channels=False,
                public_channel_ids=frozenset({123}),
            )
        )
        self.assertTrue(JarvisDiscordClient._public_channel_allowed(client, 123))
        self.assertFalse(JarvisDiscordClient._public_channel_allowed(client, 999))


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class HumanReplyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dm_is_copied_before_channel_filter_and_without_invocation(self):
        from unittest.mock import patch
        client, message = self.setup_request("yes, thanks", owner=False, reply_to_jarvis=False, dm=True)
        client.settings.dm_copy_owner_id = 100
        for allowed in (False, True):
            client._channel_allowed = lambda _: allowed
            with patch("jarvis_bot.bot.copy_incoming_dm", new_callable=AsyncMock) as copy:
                copy.side_effect = lambda *_: client._handle_public_request.assert_not_awaited()
                await JarvisDiscordClient.on_message(client, message)
            copy.assert_awaited_once_with(client, message)
            client._handle_public_request.assert_awaited_once_with(message, "yes, thanks")
            client._handle_public_request.reset_mock()
        client.assistant.respond.assert_not_awaited()

    def setup_request(self, content, *, owner=True, reply_to_jarvis=True, dm=False):
        message = SimpleNamespace(
            id=10,
            author=SimpleNamespace(id=300, bot=False),
            webhook_id=None,
            channel=SimpleNamespace(id=1, typing=lambda: _AsyncContext()),
            guild=None if dm else SimpleNamespace(id=50),
            reference=None,
            content=content,
            attachments=[],
            mentions=[],
            reply=AsyncMock(),
        )
        client = SimpleNamespace(
            _channel_allowed=lambda _: True,
            _store_discord_message=AsyncMock(),
            _is_reply_to_self=AsyncMock(return_value=reply_to_jarvis),
            _public_channel_allowed=lambda _: True,
            _handle_public_request=AsyncMock(),
            _send_answer=AsyncMock(),
            user=SimpleNamespace(id=100),
            settings=SimpleNamespace(
                trigger="Jarvis",
                authorized_user_ids=frozenset({300}) if owner else frozenset(),
                live_voice_timezone="UTC",
                live_voice_retention_days=0,
                max_prompt_chars=4000,
                chat_context_messages=20,
                chat_context_max_chars=24000,
            ),
            assistant=SimpleNamespace(
                memory=SimpleNamespace(recent_channel_messages=AsyncMock(return_value=[])),
                respond=AsyncMock(return_value="Ready."),
            ),
        )
        return client, message

    async def test_owner_reply_without_ping_reaches_assistant(self):
        for content in ("Jarvis, status", "status", "jar, status"):
            with self.subTest(content=content):
                client, message = self.setup_request(content)
                await JarvisDiscordClient.on_message(client, message)
                self.assertEqual(client.assistant.respond.await_args.kwargs["prompt"], "status")
                client._send_answer.assert_awaited_once_with(message, "Ready.", handoff_prompt="status")

    async def test_public_reply_without_ping_stays_in_public_handler(self):
        client, message = self.setup_request("status", owner=False)
        await JarvisDiscordClient.on_message(client, message)
        client._handle_public_request.assert_awaited_once_with(message, "status")
        client.assistant.respond.assert_not_awaited()

    async def test_unaddressed_chat_or_reply_to_another_user_is_ignored(self):
        for owner in (True, False):
            with self.subTest(owner=owner):
                client, message = self.setup_request("ordinary chat", owner=owner, reply_to_jarvis=False)
                await JarvisDiscordClient.on_message(client, message)
                client._handle_public_request.assert_not_awaited()
                client.assistant.respond.assert_not_awaited()
                message.reply.assert_not_awaited()

    async def test_named_request_in_reply_to_another_user_still_works(self):
        client, message = self.setup_request("Jarvis, status", reply_to_jarvis=False)
        await JarvisDiscordClient.on_message(client, message)
        self.assertEqual(client.assistant.respond.await_args.kwargs["prompt"], "status")

    async def test_plain_owner_dm_invokes_assistant_without_reply_or_trigger(self):
        client, message = self.setup_request("hello there", reply_to_jarvis=False, dm=True)
        client._channel_allowed = lambda _: False
        await JarvisDiscordClient.on_message(client, message)
        self.assertEqual(client.assistant.respond.await_args.kwargs["prompt"], "hello there")
        client._send_answer.assert_awaited_once_with(message, "Ready.", handoff_prompt="hello there")

    async def test_plain_public_dm_uses_restricted_handler_without_channel_opt_in(self):
        client, message = self.setup_request("hi, how are you?", owner=False,
                                             reply_to_jarvis=False, dm=True)
        client._channel_allowed = lambda _: False
        client._public_channel_allowed = lambda _: False
        await JarvisDiscordClient.on_message(client, message)
        client._handle_public_request.assert_awaited_once_with(message, "hi, how are you?")
        client.assistant.respond.assert_not_awaited()

    async def test_server_channel_filter_still_blocks_invocations(self):
        client, message = self.setup_request("Jarvis, status")
        client._channel_allowed = lambda _: False
        await JarvisDiscordClient.on_message(client, message)
        client._store_discord_message.assert_not_awaited()
        client.assistant.respond.assert_not_awaited()
        client._handle_public_request.assert_not_awaited()


class PublicRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_public_requests_have_no_fixed_cooldown(self) -> None:
        channel = SimpleNamespace(id=1, typing=lambda: _AsyncContext())
        message = SimpleNamespace(
            id=10,
            author=SimpleNamespace(id=200),
            channel=channel,
            guild=None,
            reference=None,
        )
        assistant = SimpleNamespace(
            memory=SimpleNamespace(
                recent_guild_messages=AsyncMock(return_value=[]),
            ),
            respond=AsyncMock(side_effect=["First answer", "Second answer"]),
        )
        client = SimpleNamespace(
            settings=SimpleNamespace(
                public_max_prompt_chars=2_000,
                public_min_five_hour_remaining_percent=25,
                public_guild_context_messages=50,
                chat_context_max_chars=24_000,
                public_memory_messages=20,
            ),
            assistant=assistant,
            _public_request_lock=asyncio.Lock(),
            _five_hour_remaining=AsyncMock(return_value=100.0),
            _visible_text_channel_ids=lambda _: set(),
            _send_answer=AsyncMock(),
        )

        await JarvisDiscordClient._handle_public_request(client, message, "First")
        await JarvisDiscordClient._handle_public_request(client, message, "Second")

        self.assertEqual(assistant.respond.await_count, 2)
        self.assertEqual(client._send_answer.await_count, 2)

class PublicPolicyTests(unittest.TestCase):
    def test_allows_questions_and_information_lookup(self) -> None:
        for prompt in (
            "What is the capital of France?",
            "Find the latest weather forecast",
            "Recap what happened in this server",
            "Jarvis and <@900000000000000001>, what are the benefits of AirPods 5 vs 4",
            "Could you search the internet for current AirPods battery life",
            "Check this page https://example.com/product and compare the specifications",
            "im like 97% sure we used this mod https://thunderstore.io/package/example",
        ):
            with self.subTest(prompt=prompt):
                self.assertIs(classify_public_request(prompt), PublicDecision.ALLOW)

    def test_allows_technical_and_coding_inquiries(self) -> None:
        for prompt in (
            "What is Python?",
            "Help me debug this code",
            "Explain an SQL join",
            "Pull up the live helicopter feed for Polaris helicopter",
            "Walk me through r2modman setup",
        ):
            with self.subTest(prompt=prompt):
                self.assertIs(classify_public_request(prompt), PublicDecision.ALLOW)

    def test_setup_guidance_is_available_to_everyone_but_actions_are_not(self) -> None:
        for prompt in (
            "How do I install mods to my Valheim dedicated server?",
            "okay im in valheim dedicated server",
            "Walk me through r2modman setup",
        ):
            with self.subTest(prompt=prompt):
                self.assertIs(
                    classify_public_request(prompt),
                    PublicDecision.ALLOW,
                )
        self.assertIs(
            classify_public_request("Can you install the Valheim mods for me?"),
            PublicDecision.DENY_ACTION,
        )
        self.assertIs(
            classify_public_request("Write a Python script for my Valheim server"),
            PublicDecision.ALLOW,
        )

    def test_denies_actions_and_allows_ambiguous_read_only_statements(self) -> None:
        self.assertIs(
            classify_public_request("Delete a message from this channel"),
            PublicDecision.DENY_ACTION,
        )
        self.assertIs(
            classify_public_request("Search my files for passwords"),
            PublicDecision.DENY_ACTION,
        )
        self.assertIs(
            classify_public_request("Check http://127.0.0.1:8080/admin"),
            PublicDecision.DENY_ACTION,
        )
        self.assertIs(
            classify_public_request("A rather curious proposition"),
            PublicDecision.ALLOW,
        )


class DeploymentSafetyTests(unittest.TestCase):
    def test_owner_persona_requires_restart_after_reply_helper(self) -> None:
        self.assertIn("Never stop or restart jarvis-discord.service synchronously", DEFAULT_PERSONA)
        self.assertIn("scripts/restart-after-reply", DEFAULT_PERSONA)


class AvatarCommandTests(unittest.TestCase):
    def test_parses_set_and_reset_commands(self) -> None:
        self.assertEqual(avatar_command_action("/pfp"), "set")
        self.assertEqual(avatar_command_action("/avatar RESET"), "reset")

    def test_rejects_extra_arguments_with_usage_action(self) -> None:
        self.assertEqual(avatar_command_action("/pfp something"), "usage")
        self.assertIsNone(avatar_command_action("change my pfp"))

    def test_accepts_supported_content_type(self) -> None:
        attachment = SimpleNamespace(content_type="image/png", filename="avatar.bin")
        self.assertTrue(supported_avatar_attachment(attachment))

    def test_falls_back_to_supported_extension(self) -> None:
        attachment = SimpleNamespace(content_type=None, filename="avatar.WEBP")
        self.assertTrue(supported_avatar_attachment(attachment))

    def test_rejects_unsupported_content_type(self) -> None:
        attachment = SimpleNamespace(content_type="text/plain", filename="avatar.png")
        self.assertFalse(supported_avatar_attachment(attachment))


class UsageCommandTests(unittest.TestCase):
    def test_accepts_common_usage_spellings(self) -> None:
        for command in ("usage", "useage", "/usage", "/useage", "USAGE"):
            with self.subTest(command=command):
                self.assertTrue(is_usage_command(command))

    def test_rejects_non_usage_prompts(self) -> None:
        self.assertFalse(is_usage_command("show me usage details"))


class BotAccessCommandTests(unittest.TestCase):
    def test_parses_toggle_and_status_commands(self) -> None:
        self.assertEqual(bot_access_command_action("/bots on"), "on")
        self.assertEqual(bot_access_command_action("/bot OFF"), "off")
        self.assertEqual(bot_access_command_action("/bots"), "status")
        self.assertEqual(bot_access_command_action("/bots status"), "status")

    def test_rejects_malformed_and_unrelated_commands(self) -> None:
        self.assertEqual(bot_access_command_action("/bots perhaps"), "usage")
        self.assertIsNone(bot_access_command_action("enable bots"))

    def test_fifty_percent_is_allowed_and_below_is_paused(self) -> None:
        self.assertTrue(bot_usage_allows_invocation(50.0, 50))
        self.assertFalse(bot_usage_allows_invocation(49.9, 50))


class UsageExhaustionTests(unittest.TestCase):
    def test_recognizes_common_quota_errors(self) -> None:
        for detail in (
            "You've hit your usage limit",
            "rate_limit_exceeded",
            "insufficient quota",
            "HTTP 429: Too Many Requests",
        ):
            with self.subTest(detail=detail):
                self.assertTrue(is_usage_exhaustion_error(detail))

    def test_does_not_misclassify_timeout(self) -> None:
        self.assertFalse(
            is_usage_exhaustion_error("Codex exceeded the 300-second time limit")
        )

    def test_message_includes_live_reset_schedule(self) -> None:
        schedule = "5-hour: 0% left · resets tonight"
        message = usage_exhausted_message(schedule)
        self.assertIn("Codex reserves are presently exhausted", message)
        self.assertIn(schedule, message)

    def test_message_has_fallback_when_schedule_lookup_fails(self) -> None:
        self.assertIn(
            "reset schedule is temporarily unavailable",
            usage_exhausted_message(),
        )


if __name__ == "__main__":
    unittest.main()
