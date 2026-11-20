from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MemoryMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ChannelMessage:
    message_id: int
    guild_id: int
    guild_name: str
    channel_id: int
    channel_name: str
    user_id: int
    user_name: str
    content: str
    created_at: str


@dataclass(frozen=True, slots=True)
class VoiceSegment:
    id: int | None
    session_id: str
    guild_id: int
    voice_channel_id: int
    user_id: int
    user_name: str
    started_at: str
    ended_at: str
    transcript: str
    is_wake_request: bool = False
    request_text: str | None = None


class MemoryStore:
    def __init__(
        self,
        database_path: Path,
        max_stored_messages: int,
        max_channel_messages: int = 10_000,
    ) -> None:
        self.database_path = database_path
        self.max_stored_messages = max_stored_messages
        self.max_channel_messages = max_channel_messages

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_scope_id "
                "ON messages(scope_key, id DESC)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_sessions (
                    scope_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_messages (
                    message_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    guild_name TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT
                )
                """
            )
            channel_columns = {
                row[1] for row in db.execute("PRAGMA table_info(channel_messages)")
            }
            if "is_deleted" not in channel_columns:
                db.execute(
                    "ALTER TABLE channel_messages "
                    "ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0"
                )
            if "deleted_at" not in channel_columns:
                db.execute(
                    "ALTER TABLE channel_messages ADD COLUMN deleted_at TEXT"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_channel_messages_channel_id "
                "ON channel_messages(channel_id, message_id DESC)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    voice_channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    transcript TEXT NOT NULL,
                    is_wake_request INTEGER NOT NULL DEFAULT 0,
                    request_text TEXT
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_voice_segments_window "
                "ON voice_segments(guild_id, voice_channel_id, ended_at)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS r_handoff_attempts (
                    source_message_id TEXT PRIMARY KEY,
                    day_utc TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('reserved', 'delivered', 'failed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_r_handoff_day "
                "ON r_handoff_attempts(day_utc)"
            )

    async def reserve_r_handoff(
        self, source_message_id: int, *, now: datetime | None = None,
    ) -> str:
        """Atomically reserve one of three global UTC-day attempts; fail closed."""
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("A timezone-aware instant is required")
        day = instant.astimezone(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(self.database_path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM r_handoff_attempts WHERE source_message_id = ?",
                (str(source_message_id),),
            ).fetchone():
                return "duplicate"
            count = db.execute(
                "SELECT COUNT(*) FROM r_handoff_attempts WHERE day_utc = ?", (day,),
            ).fetchone()[0]
            if count >= 3:
                return "daily_limit"
            db.execute(
                "INSERT INTO r_handoff_attempts(source_message_id, day_utc, status) "
                "VALUES (?, ?, 'reserved')", (str(source_message_id), day),
            )
        return "reserved"

    async def finish_r_handoff(self, source_message_id: int, *, delivered: bool) -> None:
        # Failed or ambiguous sends retain their slot: never refund and risk a fourth ask.
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.execute(
                "UPDATE r_handoff_attempts SET status = ? WHERE source_message_id = ?",
                ("delivered" if delivered else "failed", str(source_message_id)),
            )

    async def get_runtime_setting(self, key: str) -> str | None:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            row = db.execute(
                "SELECT value FROM runtime_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    async def set_runtime_setting(self, key: str, value: str) -> None:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.execute(
                """
                INSERT INTO runtime_settings(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )

    async def consume_runtime_setting(self, key: str, expected: str) -> bool:
        """Atomically claim a one-use setting across processes and restarts."""
        with closing(sqlite3.connect(self.database_path)) as db, db:
            result = db.execute(
                "DELETE FROM runtime_settings WHERE key = ? AND value = ?",
                (key, expected),
            )
            return result.rowcount == 1

    async def recent(self, scope_key: str, limit: int) -> list[MemoryMessage]:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            rows = db.execute(
                """
                SELECT role, content
                FROM messages
                WHERE scope_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (scope_key, limit),
            ).fetchall()
        return [MemoryMessage(role=row[0], content=row[1]) for row in reversed(rows)]

    async def append_turn(self, scope_key: str, user_text: str, assistant_text: str) -> None:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.executemany(
                "INSERT INTO messages(scope_key, role, content) VALUES (?, ?, ?)",
                (
                    (scope_key, "user", user_text),
                    (scope_key, "assistant", assistant_text),
                ),
            )
            db.execute(
                """
                DELETE FROM messages
                WHERE scope_key = ?
                  AND id NOT IN (
                      SELECT id FROM messages
                      WHERE scope_key = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (scope_key, scope_key, self.max_stored_messages),
            )

    async def clear(self, scope_key: str) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            cursor = db.execute("DELETE FROM messages WHERE scope_key = ?", (scope_key,))
            db.execute("DELETE FROM codex_sessions WHERE scope_key = ?", (scope_key,))
        return max(cursor.rowcount, 0)

    async def count(self, scope_key: str) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            row = db.execute(
                "SELECT COUNT(*) FROM messages WHERE scope_key = ?", (scope_key,)
            ).fetchone()
        return int(row[0]) if row else 0

    async def get_session_id(self, scope_key: str) -> str | None:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            row = db.execute(
                "SELECT session_id FROM codex_sessions WHERE scope_key = ?", (scope_key,)
            ).fetchone()
        return str(row[0]) if row else None

    async def set_session_id(self, scope_key: str, session_id: str) -> None:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.execute(
                """
                INSERT INTO codex_sessions(scope_key, session_id, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(scope_key) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (scope_key, session_id),
            )

    async def store_channel_message(self, message: ChannelMessage) -> None:
        await self.store_channel_messages([message])

    async def store_channel_messages(self, messages: list[ChannelMessage]) -> None:
        if not messages:
            return

        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.executemany(
                """
                INSERT INTO channel_messages(
                    message_id, guild_id, guild_name, channel_id, channel_name,
                    user_id, user_name, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    guild_name = excluded.guild_name,
                    channel_id = excluded.channel_id,
                    channel_name = excluded.channel_name,
                    user_id = excluded.user_id,
                    user_name = excluded.user_name,
                    content = excluded.content,
                    created_at = excluded.created_at
                """,
                [
                    (
                        str(message.message_id),
                        str(message.guild_id),
                        message.guild_name,
                        str(message.channel_id),
                        message.channel_name,
                        str(message.user_id),
                        message.user_name,
                        message.content,
                        message.created_at,
                    )
                    for message in messages
                ],
            )
    async def recent_channel_messages(
        self,
        channel_id: int,
        limit: int,
        *,
        before_message_id: int | None = None,
    ) -> list[ChannelMessage]:
        where = "channel_id = ? AND is_deleted = 0"
        parameters: list[str | int] = [str(channel_id)]
        if before_message_id is not None:
            where += " AND CAST(message_id AS INTEGER) < ?"
            parameters.append(before_message_id)
        parameters.append(limit)

        with closing(sqlite3.connect(self.database_path)) as db, db:
            rows = db.execute(
                f"""
                SELECT message_id, guild_id, guild_name, channel_id, channel_name,
                       user_id, user_name, content, created_at
                FROM channel_messages
                WHERE {where}
                ORDER BY CAST(message_id AS INTEGER) DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        return [
            ChannelMessage(
                message_id=int(row[0]),
                guild_id=int(row[1]),
                guild_name=row[2],
                channel_id=int(row[3]),
                channel_name=row[4],
                user_id=int(row[5]),
                user_name=row[6],
                content=row[7],
                created_at=row[8],
            )
            for row in reversed(rows)
        ]

    async def recent_guild_messages(
        self,
        guild_id: int,
        channel_ids: set[int],
        limit: int,
        *,
        before_message_id: int | None = None,
    ) -> list[ChannelMessage]:
        """Return recent undeleted messages from channels visible to a requester."""
        if not channel_ids:
            return []

        placeholders = ",".join("?" for _ in channel_ids)
        where = (
            f"guild_id = ? AND channel_id IN ({placeholders}) "
            "AND is_deleted = 0"
        )
        parameters: list[str | int] = [
            str(guild_id),
            *(str(channel_id) for channel_id in sorted(channel_ids)),
        ]
        if before_message_id is not None:
            where += " AND CAST(message_id AS INTEGER) < ?"
            parameters.append(before_message_id)
        parameters.append(limit)

        with closing(sqlite3.connect(self.database_path)) as db, db:
            rows = db.execute(
                f"""
                SELECT message_id, guild_id, guild_name, channel_id, channel_name,
                       user_id, user_name, content, created_at
                FROM channel_messages
                WHERE {where}
                ORDER BY CAST(message_id AS INTEGER) DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        return [
            ChannelMessage(
                message_id=int(row[0]),
                guild_id=int(row[1]),
                guild_name=row[2],
                channel_id=int(row[3]),
                channel_name=row[4],
                user_id=int(row[5]),
                user_name=row[6],
                content=row[7],
                created_at=row[8],
            )
            for row in reversed(rows)
        ]

    async def channel_message_count(self, channel_id: int) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            row = db.execute(
                "SELECT COUNT(*) FROM channel_messages "
                "WHERE channel_id = ? AND is_deleted = 0",
                (str(channel_id),),
            ).fetchone()
        return int(row[0]) if row else 0

    async def clear_channel_messages(self, channel_id: int) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            cursor = db.execute(
                """
                UPDATE channel_messages
                SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP
                WHERE channel_id = ? AND is_deleted = 0
                """,
                (str(channel_id),),
            )
        return max(cursor.rowcount, 0)

    async def mark_channel_message_deleted(self, message_id: int) -> bool:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            cursor = db.execute(
                """
                UPDATE channel_messages
                SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP
                WHERE message_id = ? AND is_deleted = 0
                """,
                (str(message_id),),
            )
        return cursor.rowcount > 0

    async def mark_channel_messages_deleted(self, message_ids: set[int]) -> int:
        if not message_ids:
            return 0
        with closing(sqlite3.connect(self.database_path)) as db, db:
            cursor = db.executemany(
                """
                UPDATE channel_messages
                SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP
                WHERE message_id = ? AND is_deleted = 0
                """,
                [(str(message_id),) for message_id in message_ids],
            )
        return max(cursor.rowcount, 0)

    async def store_voice_segment(self, segment: VoiceSegment) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            cursor = db.execute(
                """
                INSERT INTO voice_segments(
                    session_id, guild_id, voice_channel_id, user_id, user_name,
                    started_at, ended_at, transcript, is_wake_request, request_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment.session_id,
                    str(segment.guild_id),
                    str(segment.voice_channel_id),
                    str(segment.user_id),
                    segment.user_name,
                    segment.started_at,
                    segment.ended_at,
                    segment.transcript,
                    int(segment.is_wake_request),
                    segment.request_text,
                ),
            )
            return int(cursor.lastrowid)

    async def recent_voice_segments(
        self,
        guild_id: int,
        voice_channel_id: int,
        *,
        since: str | None,
        until: str,
    ) -> list[VoiceSegment]:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            if since is None:
                rows = db.execute(
                    """
                    SELECT id, session_id, guild_id, voice_channel_id, user_id,
                           user_name, started_at, ended_at, transcript,
                           is_wake_request, request_text
                    FROM voice_segments
                    WHERE guild_id = ? AND voice_channel_id = ?
                      AND started_at <= ?
                    ORDER BY started_at ASC, id ASC
                    """,
                    (str(guild_id), str(voice_channel_id), until),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                SELECT id, session_id, guild_id, voice_channel_id, user_id,
                       user_name, started_at, ended_at, transcript,
                       is_wake_request, request_text
                FROM voice_segments
                WHERE guild_id = ? AND voice_channel_id = ?
                  AND ended_at >= ? AND started_at <= ?
                ORDER BY started_at ASC, id ASC
                """,
                (str(guild_id), str(voice_channel_id), since, until),
                ).fetchall()
        return [
            VoiceSegment(
                id=int(row[0]),
                session_id=row[1],
                guild_id=int(row[2]),
                voice_channel_id=int(row[3]),
                user_id=int(row[4]),
                user_name=row[5],
                started_at=row[6],
                ended_at=row[7],
                transcript=row[8],
                is_wake_request=bool(row[9]),
                request_text=row[10],
            )
            for row in rows
        ]

    async def voice_segment_count(
        self, guild_id: int, voice_channel_id: int
    ) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            row = db.execute(
                "SELECT COUNT(*) FROM voice_segments "
                "WHERE guild_id = ? AND voice_channel_id = ?",
                (str(guild_id), str(voice_channel_id)),
            ).fetchone()
        return int(row[0]) if row else 0

    async def delete_voice_segments_since(
        self, guild_id: int, voice_channel_id: int, since: str
    ) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            cursor = db.execute(
                "DELETE FROM voice_segments WHERE guild_id = ? "
                "AND voice_channel_id = ? AND ended_at >= ?",
                (str(guild_id), str(voice_channel_id), since),
            )
        return max(cursor.rowcount, 0)

    async def clear_voice_segments(
        self, guild_id: int, voice_channel_id: int
    ) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            cursor = db.execute(
                "DELETE FROM voice_segments "
                "WHERE guild_id = ? AND voice_channel_id = ?",
                (str(guild_id), str(voice_channel_id)),
            )
        return max(cursor.rowcount, 0)

    async def prune_voice_segments(self, before: str) -> int:
        with closing(sqlite3.connect(self.database_path)) as db, db:
            cursor = db.execute(
                "DELETE FROM voice_segments WHERE ended_at < ?", (before,)
            )
        return max(cursor.rowcount, 0)
