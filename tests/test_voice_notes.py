import io
import unittest
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from jarvis_bot.memory import VoiceSegment
from jarvis_bot.voice_notes import (
    LiveVoiceNotes,
    extract_voice_prompt,
    format_voice_context,
    pcm_to_wav,
    voice_notes_command,
    voice_notes_query,
    voice_summary_minutes,
)


class VoiceCommandParsingTests(unittest.TestCase):
    def test_extracts_exact_wake_phrase(self) -> None:
        self.assertEqual(
            extract_voice_prompt("Hey Jarvis, what time is it?", "Hey Jarvis"),
            "what time is it",
        )
        self.assertEqual(extract_voice_prompt("Hey Jarvis", "Hey Jarvis"), "")
        self.assertEqual(
            extract_voice_prompt("Hey, Jarvis... what time is it?", "Hey Jarvis"),
            "what time is it",
        )
        self.assertIsNone(
            extract_voice_prompt("Jarvis was watching the stream", "Hey Jarvis")
        )
        self.assertIsNone(
            extract_voice_prompt("Hey Jarvisian systems", "Hey Jarvis")
        )

    def test_parses_voice_summary_windows(self) -> None:
        self.assertEqual(voice_summary_minutes("/vc-summary"), 30)
        self.assertEqual(voice_summary_minutes("/vc-summary minutes:45"), 45)
        self.assertEqual(
            voice_summary_minutes("summarise the last 2 hours"), 120
        )
        self.assertEqual(
            voice_summary_minutes(
                "could you summarise what was said in the last 45 minutes"
            ),
            45,
        )
        self.assertIsNone(voice_summary_minutes("what happened yesterday"))
        self.assertIsNone(
            voice_summary_minutes(
                "walk me through how summarising the last 30 minutes would work"
            )
        )
        with self.assertRaises(ValueError):
            voice_summary_minutes("/vc-summary eventually")

    def test_parses_voice_note_controls(self) -> None:
        self.assertEqual(voice_notes_command("/vc-notes"), ("status", None))
        self.assertEqual(voice_notes_command("/vc-notes pause"), ("pause", None))
        self.assertEqual(voice_notes_command(" NToggle "), ("toggle", None))
        self.assertEqual(
            voice_notes_command("/vc-notes delete minutes:30"),
            ("delete", 30),
        )
        self.assertEqual(
            voice_notes_command("/vc-notes delete all"), ("delete_all", None)
        )
        self.assertIsNone(voice_notes_command("ordinary chat"))
        self.assertIsNone(voice_notes_command("notes"))
        self.assertIsNone(voice_notes_command("/vc toggle"))
        self.assertIsNone(voice_notes_command("/vc-summary 30"))

    def test_parses_conversational_voice_note_controls(self) -> None:
        self.assertEqual(
            voice_notes_command("please pause the voice notes"),
            ("pause", None),
        )
        self.assertEqual(
            voice_notes_command("start logging voice notes again"),
            ("resume", None),
        )
        self.assertEqual(
            voice_notes_command("are the VC notes currently working?"),
            ("status", None),
        )
        self.assertEqual(
            voice_notes_command("are you logging the void voice channel?"),
            ("status", None),
        )
        self.assertEqual(
            voice_notes_command("delete all stored voice transcripts"),
            ("delete_all", None),
        )
        self.assertEqual(
            voice_notes_command("delete voice notes from the last 2 hours"),
            ("delete", 120),
        )
        self.assertEqual(
            voice_notes_command("could you stop listening to the void for now?"),
            ("pause", None),
        )
        self.assertEqual(
            voice_notes_command("go back to listening to the void voice chat"),
            ("resume", None),
        )
        self.assertIsNone(voice_notes_command("pause the music"))

    def test_parses_voice_note_dates_times_and_topics_in_server_timezone(self) -> None:
        now = datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)
        query = voice_notes_query(
            "notes yesterday from 2pm to 4pm about cyberpunk",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNotNone(query)
        self.assertEqual(query.since.isoformat(), "2026-09-14T19:00:00+00:00")
        self.assertEqual(query.until.isoformat(), "2026-09-14T21:00:00+00:00")
        self.assertEqual(query.topic, "cyberpunk")
        self.assertIn("CDT", query.label)

    def test_parses_voice_notes_for_named_days_and_context(self) -> None:
        now = datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)
        monday = voice_notes_query(
            "notes monday",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertEqual(monday.since.isoformat(), "2026-09-14T05:00:00+00:00")
        self.assertEqual(monday.until.isoformat(), "2026-09-15T05:00:00+00:00")

        topic = voice_notes_query(
            "notes about the server migration",
            now=now,
            timezone_name="America/Chicago",
            retention_days=7,
        )
        self.assertEqual(topic.topic, "the server migration")
        self.assertEqual(topic.since, now - timedelta(days=7))
        self.assertEqual(topic.until, now)

    def test_bare_notes_defaults_to_last_thirty_minutes(self) -> None:
        now = datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)
        query = voice_notes_query("notes", now=now)
        self.assertEqual(query.since, now - timedelta(minutes=30))
        self.assertEqual(query.until, now)

    def test_non_note_request_is_not_claimed(self) -> None:
        self.assertIsNone(voice_notes_query("tell me a joke"))
        self.assertIsNone(
            voice_notes_query("did memberthree say anything about dinner?")
        )
        self.assertIsNone(
            voice_notes_query(
                "walk me through the logistics behind implementing audible feedback "
                "in a jarvis voice in that voicechat and text chat. what are the "
                "limitations and latency?"
            )
        )
        self.assertIsNone(
            voice_notes_query("how does voice transcription work?")
        )
        self.assertIsNone(
            voice_notes_query("how does the voice-notes summary system work?")
        )

    def test_parses_conversational_speaker_query(self) -> None:
        now = datetime(2026, 9, 15, 21, 45, tzinfo=timezone.utc)
        query = voice_notes_query(
            "did memberthree say anything to you in the last 30 min?",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNotNone(query)
        self.assertEqual(query.since, now - timedelta(minutes=30))
        self.assertEqual(query.until, now)
        self.assertEqual(query.topic, "memberthree")

        without_time = voice_notes_query(
            "did memberthree say anything in voice?",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNotNone(without_time)
        self.assertEqual(without_time.since, now - timedelta(minutes=30))
        self.assertEqual(without_time.topic, "memberthree")

    def test_parses_conversational_voice_recap(self) -> None:
        now = datetime(2026, 9, 15, 21, 45, tzinfo=timezone.utc)
        query = voice_notes_query(
            "what happened in the void over the last 2 hours?",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNotNone(query)
        self.assertEqual(query.since, now - timedelta(hours=2))
        self.assertIsNone(query.topic)

        notes = voice_notes_query(
            "give me the notes from the last 45 minutes",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNotNone(notes)
        self.assertEqual(notes.since, now - timedelta(minutes=45))

        today = voice_notes_query(
            "what all was said today in the vc?",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNotNone(today)
        self.assertEqual(today.since.isoformat(), "2026-09-15T05:00:00+00:00")

        catch_up = voice_notes_query(
            "catch me up on the voice chat today",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNotNone(catch_up)
        self.assertEqual(catch_up.since.isoformat(), "2026-09-15T05:00:00+00:00")

    def test_topic_and_all_queries_cover_unbounded_retained_history(self) -> None:
        now = datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)
        topic = voice_notes_query(
            "notes about deployment",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNone(topic.since)
        self.assertEqual(topic.until, now)
        self.assertEqual(topic.label, "all retained history")

        everything = voice_notes_query(
            "notes all",
            now=now,
            timezone_name="America/Chicago",
        )
        self.assertIsNone(everything.since)
        self.assertIsNone(everything.topic)
        self.assertEqual(everything.label, "all retained history")


class VoiceAudioTests(unittest.TestCase):
    def test_live_capture_uses_account_username_not_server_nickname(self) -> None:
        notes = LiveVoiceNotes(
            settings=SimpleNamespace(),
            memory=SimpleNamespace(),
            transcriber=SimpleNamespace(),
            request_handler=AsyncMock(),
        )
        notes._loop = SimpleNamespace(call_soon_threadsafe=Mock())
        user = SimpleNamespace(
            id=3,
            name="account_username",
            display_name="Shared Server Nickname",
            bot=False,
        )
        notes._sink_write(user, SimpleNamespace(pcm=b"\x00\x00"))

        callback = notes._loop.call_soon_threadsafe
        callback.assert_called_once_with(
            notes._accept_packet,
            3,
            "account_username",
            b"\x00\x00",
        )

    def test_wraps_discord_pcm_as_stereo_48khz_wav(self) -> None:
        audio = pcm_to_wav(b"\x00\x00" * 960 * 2)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getframerate(), 48_000)
            self.assertEqual(wav.getsampwidth(), 2)

    def test_voice_context_keeps_recent_segments_under_limit(self) -> None:
        segments = [
            VoiceSegment(
                id=index,
                session_id="session",
                guild_id=1,
                voice_channel_id=2,
                user_id=3,
                user_name="Speaker",
                started_at=f"start-{index}",
                ended_at=f"end-{index}",
                transcript=f"message {index}",
            )
            for index in range(3)
        ]
        context = format_voice_context(segments, max_chars=90)
        self.assertIn("message 2", context)
        self.assertNotIn("message 0", context)

    def test_voice_context_prioritises_topic_and_nearby_segments(self) -> None:
        segments = [
            VoiceSegment(
                id=index,
                session_id="session",
                guild_id=1,
                voice_channel_id=2,
                user_id=3,
                user_name="Speaker",
                started_at=f"start-{index}",
                ended_at=f"end-{index}",
                transcript=(
                    "the cyberpunk deck discussion"
                    if index == 2
                    else f"unrelated message {index}"
                ),
            )
            for index in range(8)
        ]
        context = format_voice_context(segments, max_chars=400, topic="cyberpunk")
        self.assertIn("cyberpunk deck discussion", context)
        self.assertIn("unrelated message 1", context)
        self.assertNotIn("unrelated message 7", context)

    def test_voice_context_can_match_a_speaker_name(self) -> None:
        segments = [
            VoiceSegment(
                id=index,
                session_id="session",
                guild_id=1,
                voice_channel_id=2,
                user_id=3 + index,
                user_name="examplemember" if index == 2 else "Speaker",
                started_at=f"start-{index}",
                ended_at=f"end-{index}",
                transcript=f"message {index}",
            )
            for index in range(8)
        ]
        context = format_voice_context(segments, max_chars=400, topic="examplemember")
        self.assertIn("examplemember", context)
        self.assertIn("message 1", context)
        self.assertNotIn("message 7", context)

    def test_voice_context_prefers_resolved_discord_username(self) -> None:
        segment = VoiceSegment(
            id=1,
            session_id="session",
            guild_id=1,
            voice_channel_id=2,
            user_id=3,
            user_name="Shared Server Nickname",
            started_at="start",
            ended_at="end",
            transcript="hello",
        )
        context = format_voice_context(
            [segment],
            max_chars=400,
            user_names={3: "account_username"},
        )
        self.assertIn("account_username (user 3)", context)
        self.assertNotIn("Shared Server Nickname", context)


class VoiceWakeFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_wake_phrase_claims_next_turn_from_same_speaker(self) -> None:
        memory = SimpleNamespace(store_voice_segment=AsyncMock(side_effect=[1, 2]))
        handler = AsyncMock()
        settings = SimpleNamespace(
            live_voice_wake_phrase="Hey Jarvis",
            live_voice_followup_seconds=10,
            live_voice_channel_id=22,
        )
        notes = LiveVoiceNotes(
            settings=settings,
            memory=memory,
            transcriber=SimpleNamespace(),
            request_handler=handler,
        )
        notes.notify = AsyncMock()
        now = datetime.now(timezone.utc)

        await notes._record_transcript(5, "Pepper", now, now, "Hey Jarvis")
        await notes._record_transcript(
            5, "Pepper", now, now, "What time is it?"
        )

        notes.notify.assert_awaited_once()
        handler.assert_awaited_once()
        utterance, prompt = handler.await_args.args
        self.assertEqual(utterance.user_id, 5)
        self.assertEqual(prompt, "What time is it?")


class VoiceConnectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_voice_client_is_cleaned_before_reconnecting(self) -> None:
        class FakeVoiceChannel:
            def __init__(self, replacement: object) -> None:
                self.id = 22
                self.name = "Void"
                self.members = [SimpleNamespace(bot=False)]
                self.connect = AsyncMock(return_value=replacement)

        replacement = SimpleNamespace()
        channel = FakeVoiceChannel(replacement)
        client = SimpleNamespace(
            get_channel=Mock(return_value=channel),
            fetch_channel=AsyncMock(),
        )
        stale = SimpleNamespace(
            is_connected=Mock(return_value=False),
            is_listening=Mock(return_value=True),
            stop_listening=Mock(),
            disconnect=AsyncMock(),
        )
        notes = LiveVoiceNotes(
            settings=SimpleNamespace(live_voice_channel_id=22),
            memory=SimpleNamespace(get_runtime_setting=AsyncMock(return_value="true")),
            transcriber=SimpleNamespace(),
            request_handler=AsyncMock(),
        )
        notes.client = client
        notes.voice_client = stale
        notes._enabled = True
        notes._begin_listening = Mock()

        with patch("jarvis_bot.voice_notes.discord.VoiceChannel", FakeVoiceChannel):
            connected = await notes.ensure_connected()

        self.assertTrue(connected)
        stale.disconnect.assert_awaited_once_with(force=True)
        channel.connect.assert_awaited_once()
        self.assertIs(notes.voice_client, replacement)
        notes._begin_listening.assert_called_once_with(replacement)


class VoiceEmptyChannelTests(unittest.IsolatedAsyncioTestCase):
    def make_notes(self):
        memory = SimpleNamespace(
            get_runtime_setting=AsyncMock(return_value="true"),
            set_runtime_setting=AsyncMock(),
        )
        notes = LiveVoiceNotes(
            settings=SimpleNamespace(live_voice_channel_id=22, live_voice_enabled=True),
            memory=memory,
            transcriber=SimpleNamespace(),
            request_handler=AsyncMock(),
        )
        notes.voice_client = SimpleNamespace(
            is_listening=Mock(return_value=True),
            stop_listening=Mock(),
            disconnect=AsyncMock(),
        )
        notes._flush_all = AsyncMock()
        return notes

    async def test_leaves_only_after_five_minutes_and_stays_disabled(self):
        notes = self.make_notes()
        voice = notes.voice_client
        channel = SimpleNamespace(id=22, members=[SimpleNamespace(bot=True)])
        with patch("jarvis_bot.voice_notes.monotonic", side_effect=[100, 399, 400]):
            self.assertFalse(await notes._leave_if_empty(channel))
            self.assertFalse(await notes._leave_if_empty(channel))
            voice.disconnect.assert_not_awaited()
            self.assertTrue(await notes._leave_if_empty(channel))
        notes.memory.set_runtime_setting.assert_awaited_once_with(
            "live_voice_notes_enabled", "false"
        )
        voice.disconnect.assert_awaited_once_with(force=True)
        notes._flush_all.assert_awaited_once()
        self.assertIsNone(notes.voice_client)
        notes.memory.get_runtime_setting.return_value = "false"
        self.assertFalse(await notes.ensure_connected())

    async def test_human_presence_resets_the_full_timeout(self):
        notes = self.make_notes()
        channel = SimpleNamespace(id=22, members=[])
        with patch("jarvis_bot.voice_notes.monotonic", side_effect=[100, 500, 799, 800]):
            self.assertFalse(await notes._leave_if_empty(channel))
            channel.members = [SimpleNamespace(bot=False)]
            self.assertFalse(await notes._leave_if_empty(channel))
            self.assertIsNone(notes._empty_since)
            channel.members = []
            self.assertFalse(await notes._leave_if_empty(channel))
            self.assertFalse(await notes._leave_if_empty(channel))
            self.assertTrue(await notes._leave_if_empty(channel))

    async def test_short_visit_between_checks_resets_timeout(self):
        notes = self.make_notes()
        channel = SimpleNamespace(id=22, members=[])
        notes._empty_since = 100
        notes.observe_voice_state(
            SimpleNamespace(bot=False),
            SimpleNamespace(channel=None),
            SimpleNamespace(channel=channel),
        )
        notes.observe_voice_state(
            SimpleNamespace(bot=False),
            SimpleNamespace(channel=channel),
            SimpleNamespace(channel=None),
        )
        with patch("jarvis_bot.voice_notes.monotonic", return_value=500):
            self.assertFalse(await notes._leave_if_empty(channel))
        self.assertEqual(notes._empty_since, 500)
        notes.voice_client.disconnect.assert_not_awaited()

    async def test_bots_mutes_and_other_channels_do_not_reset_timer(self):
        notes = self.make_notes()
        notes._empty_since = 100
        for bot, before_id, after_id in [(True, None, 22), (False, 22, 22), (False, 3, 4)]:
            notes.observe_voice_state(
                SimpleNamespace(bot=bot),
                SimpleNamespace(channel=SimpleNamespace(id=before_id)),
                SimpleNamespace(channel=SimpleNamespace(id=after_id)),
            )
            self.assertEqual(notes._empty_since, 100)

    async def test_external_disable_disconnects_despite_cached_enabled_value(self):
        notes = self.make_notes()
        voice = notes.voice_client
        notes._enabled = True
        notes.memory.get_runtime_setting.return_value = "false"
        self.assertFalse(await notes.ensure_connected())
        voice.disconnect.assert_awaited_once_with(force=True)
        notes._flush_all.assert_awaited_once()

    async def test_explicit_join_starts_a_fresh_empty_period(self):
        notes = self.make_notes()
        notes._empty_since = 100
        notes.ensure_connected = AsyncMock(return_value=True)
        await notes.set_enabled(True)
        self.assertIsNone(notes._empty_since)
        notes.memory.set_runtime_setting.assert_awaited_once_with(
            "live_voice_notes_enabled", "true"
        )
        notes.ensure_connected.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
