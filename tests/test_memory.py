import sqlite3
import tempfile
import unittest
from pathlib import Path

from jarvis_bot.memory import ChannelMessage, MemoryStore, VoiceSegment


class MemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "memory.sqlite3"
        self.memory = MemoryStore(
            self.database_path,
            max_stored_messages=4,
            max_channel_messages=4,
        )
        await self.memory.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_memory_survives_new_store_instance(self) -> None:
        await self.memory.append_turn("scope", "My name is Pepper", "Noted.")
        reopened = MemoryStore(self.database_path, max_stored_messages=4)
        await reopened.initialize()
        messages = await reopened.recent("scope", 10)
        self.assertEqual(
            [(item.role, item.content) for item in messages],
            [("user", "My name is Pepper"), ("assistant", "Noted.")],
        )

    async def test_prunes_old_messages(self) -> None:
        await self.memory.append_turn("scope", "one", "two")
        await self.memory.append_turn("scope", "three", "four")
        await self.memory.append_turn("scope", "five", "six")
        messages = await self.memory.recent("scope", 10)
        self.assertEqual([item.content for item in messages], ["three", "four", "five", "six"])

    async def test_scopes_are_isolated_and_clearable(self) -> None:
        await self.memory.append_turn("a", "hello", "there")
        await self.memory.append_turn("b", "different", "memory")
        removed = await self.memory.clear("a")
        self.assertEqual(removed, 2)
        self.assertEqual(await self.memory.count("a"), 0)
        self.assertEqual(await self.memory.count("b"), 2)

    async def test_codex_session_mapping_is_persistent_and_clearable(self) -> None:
        await self.memory.set_session_id("scope", "session-123")
        reopened = MemoryStore(self.database_path, max_stored_messages=4)
        await reopened.initialize()
        self.assertEqual(await reopened.get_session_id("scope"), "session-123")
        await reopened.clear("scope")
        self.assertIsNone(await reopened.get_session_id("scope"))

    async def test_runtime_setting_survives_new_store_instance(self) -> None:
        self.assertIsNone(await self.memory.get_runtime_setting("bot_access"))
        await self.memory.set_runtime_setting("bot_access", "true")

        reopened = MemoryStore(self.database_path, max_stored_messages=4)
        await reopened.initialize()

        self.assertEqual(
            await reopened.get_runtime_setting("bot_access"), "true"
        )

    async def test_channel_history_stores_metadata_orders_and_soft_deletes(self) -> None:
        records = [
            ChannelMessage(
                message_id=message_id,
                guild_id=100,
                guild_name="Avengers",
                channel_id=200,
                channel_name="general",
                user_id=300 + message_id,
                user_name=f"user-{message_id}",
                content=f"message-{message_id}",
                created_at=f"2026-01-01T00:00:0{message_id}+00:00",
            )
            for message_id in range(1, 6)
        ]
        await self.memory.store_channel_messages(records)

        recent = await self.memory.recent_channel_messages(200, 10)
        self.assertEqual([item.message_id for item in recent], [1, 2, 3, 4, 5])
        self.assertEqual(recent[-1].guild_name, "Avengers")
        self.assertEqual(recent[-1].user_name, "user-5")

        before = await self.memory.recent_channel_messages(
            200, 10, before_message_id=5
        )
        self.assertEqual([item.message_id for item in before], [1, 2, 3, 4])
        self.assertEqual(await self.memory.channel_message_count(200), 5)

        marked = await self.memory.mark_channel_message_deleted(3)
        self.assertTrue(marked)
        recent = await self.memory.recent_channel_messages(200, 10)
        self.assertEqual([item.message_id for item in recent], [1, 2, 4, 5])

        removed = await self.memory.clear_channel_messages(200)
        self.assertEqual(removed, 4)
        self.assertEqual(await self.memory.channel_message_count(200), 0)

        with sqlite3.connect(self.database_path) as db:
            total, deleted = db.execute(
                "SELECT COUNT(*), SUM(is_deleted) FROM channel_messages"
            ).fetchone()
        self.assertEqual((total, deleted), (5, 5))

    async def test_guild_history_is_limited_to_requester_visible_channels(self) -> None:
        records = [
            ChannelMessage(
                message_id=message_id,
                guild_id=100,
                guild_name="Avengers",
                channel_id=channel_id,
                channel_name=f"channel-{channel_id}",
                user_id=300,
                user_name="user",
                content=f"message-{message_id}",
                created_at=f"2026-01-01T00:00:0{message_id}+00:00",
            )
            for message_id, channel_id in ((1, 200), (2, 201), (3, 202))
        ]
        await self.memory.store_channel_messages(records)

        visible = await self.memory.recent_guild_messages(
            100, {200, 202}, 10
        )

        self.assertEqual([item.message_id for item in visible], [1, 3])

    async def test_voice_segments_store_query_delete_and_prune(self) -> None:
        for index, ended_at in enumerate(
            (
                "2026-01-01T00:01:00+00:00",
                "2026-01-01T00:11:00+00:00",
                "2026-01-01T00:21:00+00:00",
            ),
            start=1,
        ):
            await self.memory.store_voice_segment(
                VoiceSegment(
                    id=None,
                    session_id="session",
                    guild_id=100,
                    voice_channel_id=200,
                    user_id=300,
                    user_name="Speaker",
                    started_at=ended_at,
                    ended_at=ended_at,
                    transcript=f"segment {index}",
                    is_wake_request=index == 2,
                    request_text="status" if index == 2 else None,
                )
            )

        recent = await self.memory.recent_voice_segments(
            100,
            200,
            since="2026-01-01T00:10:00+00:00",
            until="2026-01-01T00:30:00+00:00",
        )
        self.assertEqual([item.transcript for item in recent], ["segment 2", "segment 3"])
        self.assertTrue(recent[0].is_wake_request)
        all_segments = await self.memory.recent_voice_segments(
            100,
            200,
            since=None,
            until="2026-01-01T00:30:00+00:00",
        )
        self.assertEqual(len(all_segments), 3)
        self.assertEqual(await self.memory.voice_segment_count(100, 200), 3)

        removed = await self.memory.delete_voice_segments_since(
            100, 200, "2026-01-01T00:20:00+00:00"
        )
        self.assertEqual(removed, 1)
        self.assertEqual(
            await self.memory.prune_voice_segments("2026-01-01T00:10:00+00:00"),
            1,
        )
        self.assertEqual(await self.memory.clear_voice_segments(100, 200), 1)


if __name__ == "__main__":
    unittest.main()
