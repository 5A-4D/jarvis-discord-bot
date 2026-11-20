"""Persistent, self-addressed Discord reminders, without a model or shell turn."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import discord

LOGGER = logging.getLogger(__name__)
MAX_PENDING = 10
MAX_TEXT = 1000
MAX_DELAY = timedelta(days=365)
_INTRO = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:remind\s+me|/remind)\b[\s,:]*", re.I,
)
_NUMBER = r"(?:\d+(?:\.\d+)?|(?:an?|one|two|three|four|five|six|seven|eight|nine|ten)\b)"
_UNIT = r"(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)"
_PART = rf"(?P<amount>{_NUMBER})\s*(?P<unit>{_UNIT})\b"
_DURATION = re.compile(rf"\bin\s+(?P<duration>{_NUMBER}\s*{_UNIT}\b(?:\s*(?:,|and)?\s*{_NUMBER}\s*{_UNIT}\b)*)", re.I)
_WORDS = dict(zip(('a', 'an', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'), (1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)))
_LIST = re.compile(r"^\s*(?:/reminders|(?:my\s+)?reminders(?:\s+list)?)\s*$", re.I)
_CANCEL = re.compile(r"^\s*(?:/reminders\s+cancel|cancel\s+reminder)\s+(\d+)\s*$", re.I)
USAGE = 'Use `Jarvis remind me in 3.5 hours to check that post` or `Jarvis remind me to check that post in 3.5 hours`.'


@dataclass(frozen=True)
class ReminderRequest:
    text: str
    due_at: datetime


def is_reminder_command(prompt: str) -> bool:
    """Distinguish timed reminders from recall such as 'remind me those speakers'."""
    if _LIST.fullmatch(prompt) or _CANCEL.fullmatch(prompt):
        return True
    intro = _INTRO.match(prompt)
    if intro is None:
        return False
    rest = prompt[intro.end():]
    return bool(prompt.lstrip().lower().startswith('/remind') or re.search(
        rf"\bin\s+(?:-?\d|{_NUMBER}\b)|\bat\s+\d|\b(?:tomorrow|tonight|next)\b", rest, re.I,
    ))


def parse_reminder(prompt: str, *, now: datetime) -> ReminderRequest | None:
    intro = _INTRO.match(prompt)
    if intro is None or not is_reminder_command(prompt):
        return None
    rest = prompt[intro.end():]
    duration = _DURATION.search(rest)
    if duration is None:
        raise ValueError(USAGE)
    seconds = 0.0
    for part in re.finditer(_PART, duration['duration'], re.I):
        raw = part['amount'].lower()
        amount = float(_WORDS[raw]) if raw in _WORDS else float(raw)
        unit = part['unit'].lower()
        multiplier = 1 if unit.startswith('s') else 60 if unit.startswith('m') else 3600 if unit.startswith('h') else 86400 if unit.startswith('d') else 604800
        seconds += amount * multiplier
    if not 0 < seconds <= MAX_DELAY.total_seconds():
        raise ValueError('Choose a delay greater than zero and no more than 365 days.')
    before = rest[:duration.start()].strip(' ,:;.')
    after = rest[duration.end():].strip(' ,:;.')
    text = ' '.join(part for part in (before, after) if part)
    text = re.sub(r'^to\s+', '', text, flags=re.I).strip()
    if not text or len(text) > MAX_TEXT:
        raise ValueError(f'Include what to remind you about, up to {MAX_TEXT} characters. {USAGE}')
    if now.tzinfo is None:
        raise ValueError('Reminder times require a timezone.')
    return ReminderRequest(text, now.astimezone(timezone.utc) + timedelta(seconds=seconds))


class ReminderStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL UNIQUE,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                source_url TEXT NOT NULL,
                due_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','sending','sent','cancelled','uncertain')),
                delivered_message_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status,due_at)')
        self.path.chmod(0o600)

    def create(self, *, source_id: int, guild_id: int, channel_id: int, user_id: int,
               request: ReminderRequest, source_url: str):
        if not request.text.strip() or len(request.text) > MAX_TEXT or request.due_at.tzinfo is None:
            raise ValueError('Invalid reminder text or time.')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM reminders WHERE source_id=?', (str(source_id),)).fetchone()
            if previous:
                if (previous['user_id'], previous['channel_id'], previous['guild_id']) != (str(user_id), str(channel_id), str(guild_id)):
                    raise ValueError('That source message belongs to another conversation.')
                return previous
            count = db.execute("SELECT COUNT(*) FROM reminders WHERE user_id=? AND status IN ('pending','sending','uncertain')", (str(user_id),)).fetchone()[0]
            if count >= MAX_PENDING:
                raise ValueError(f'You already have {MAX_PENDING} active reminders. Cancel one first.')
            cursor = db.execute('''INSERT INTO reminders
                (source_id,guild_id,channel_id,user_id,text,source_url,due_at)
                VALUES(?,?,?,?,?,?,?)''', (str(source_id), str(guild_id), str(channel_id), str(user_id), request.text, source_url, request.due_at.timestamp()))
            return db.execute('SELECT * FROM reminders WHERE id=?', (cursor.lastrowid,)).fetchone()

    def list_for(self, user_id: int, channel_id: int):
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM reminders WHERE user_id=? AND channel_id=? AND status IN ('pending','sending','uncertain') ORDER BY due_at,id", (str(user_id), str(channel_id))).fetchall()

    def cancel(self, reminder_id: int, user_id: int, channel_id: int) -> bool:
        with closing(self.connect()) as db, db:
            return db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND user_id=? AND channel_id=? AND status='pending'", (reminder_id, str(user_id), str(channel_id))).rowcount == 1

    def recover(self):
        # A crash after Discord accepted a send must not replay a stale nonce.
        with closing(self.connect()) as db, db:
            db.execute("UPDATE reminders SET status='uncertain' WHERE status='sending'")

    def claim(self, *, now: datetime):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM reminders WHERE status='pending' AND due_at<=? ORDER BY due_at,id LIMIT 1", (now.timestamp(),)).fetchone()
            if row:
                db.execute("UPDATE reminders SET status='sending' WHERE id=? AND status='pending'", (row['id'],))
            return row

    def finish(self, reminder_id: int, message_id: str | None):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE reminders SET status=?,delivered_message_id=? WHERE id=? AND status='sending'", ('sent' if message_id else 'uncertain', message_id, reminder_id))


def reminder_payload(row) -> dict:
    text = discord.utils.escape_mentions(row['text'])
    return {
        'content': f"⏰ <@{row['user_id']}> Reminder: {text}\n[Original request](<{row['source_url']}>)",
        'allowed_mentions': {'parse': [], 'users': [str(row['user_id'])], 'replied_user': False},
        'flags': 1 << 2,
        'nonce': row['source_id'],
        'enforce_nonce': True,
    }


async def deliver_one(store: ReminderStore, sender: Callable[[int, dict], Awaitable[dict]], *, now: datetime) -> bool:
    row = await asyncio.to_thread(store.claim, now=now)
    if row is None:
        return False
    try:
        payload = reminder_payload(row)
        result = await sender(int(row['channel_id']), payload)
        message_id = str(result.get('id', ''))
        if not message_id.isdigit() or str(result.get('channel_id')) != row['channel_id'] or result.get('content') != payload['content']:
            raise ValueError('Unexpected Discord reminder delivery response')
        await asyncio.to_thread(store.finish, row['id'], message_id)
        LOGGER.info('Reminder %s delivered and verified', row['id'])
    except Exception as error:
        await asyncio.to_thread(store.finish, row['id'], None)
        LOGGER.warning('Reminder %s requires delivery reconciliation: %s', row['id'], type(error).__name__)
    return True


async def reminder_worker(client, store: ReminderStore):
    from discord.http import Route

    await client.wait_until_ready()
    await asyncio.to_thread(store.recover)

    async def sender(channel_id, payload):
        return await client.http.request(Route('POST', '/channels/{channel_id}/messages', channel_id=channel_id), json=payload)

    while True:
        try:
            if client.is_ready():
                guard = getattr(client, '_reply_guard', None)
                if guard is None:
                    delivered = await deliver_one(store, sender, now=datetime.now(timezone.utc))
                else:
                    async with guard.hold():
                        delivered = await deliver_one(store, sender, now=datetime.now(timezone.utc))
                if delivered:
                    continue
        except (OSError, sqlite3.Error) as error:
            LOGGER.warning('Reminder queue unavailable: %s', type(error).__name__)
        await asyncio.sleep(5)


async def handle_reminder_request(client, message, prompt: str, *, is_owner: bool) -> bool:
    # Bare owner 'reminders' refers to the existing Desktop checklist.
    if is_owner and _LIST.fullmatch(prompt) and not prompt.strip().startswith('/'):
        return False
    if not is_reminder_command(prompt):
        return False
    if message.author.bot or getattr(message, 'webhook_id', None) is not None:
        return True
    reply_options = dict(mention_author=False, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none())
    if not is_owner and message.guild is not None and not client._public_channel_allowed(message.channel.id):
        await message.reply('Public reminders are not enabled in this channel.', **reply_options)
        return True
    store = getattr(client, '_reminders', None)
    if store is None:
        await message.reply('The reminder queue is unavailable. Nothing was scheduled.', **reply_options)
        return True
    try:
        if _LIST.fullmatch(prompt):
            rows = await asyncio.to_thread(store.list_for, message.author.id, message.channel.id)
            if not rows:
                await message.reply('You have no active reminders in this conversation.', **reply_options)
            for row in rows:
                await message.reply(f"**Reminder #{row['id']}** · <t:{int(row['due_at'])}:F> · {row['status']}\n{discord.utils.escape_mentions(row['text'])}", **reply_options)
            return True
        cancel = _CANCEL.fullmatch(prompt)
        if cancel:
            removed = await asyncio.to_thread(store.cancel, int(cancel[1]), message.author.id, message.channel.id)
            await message.reply('Reminder cancelled.' if removed else 'No pending reminder with that ID belongs to you in this conversation.', **reply_options)
            return True
        request = parse_reminder(prompt, now=message.created_at)
        row = await asyncio.to_thread(store.create, source_id=message.id,
            guild_id=message.guild.id if message.guild else 0, channel_id=message.channel.id,
            user_id=message.author.id, request=request, source_url=message.jump_url)
        if row['status'] != 'pending':
            await message.reply(f"That request already has reminder #{row['id']} ({row['status']}).", **reply_options)
        else:
            await message.reply(f"⏰ Reminder **#{row['id']}** set for <t:{int(row['due_at'])}:F> (<t:{int(row['due_at'])}:R>). I’ll ping you here.\n{discord.utils.escape_mentions(row['text'])}", **reply_options)
    except ValueError as error:
        await message.reply(str(error), **reply_options)
    except (OSError, sqlite3.Error) as error:
        LOGGER.warning('Reminder command failed: %s', type(error).__name__)
        await message.reply('I couldn’t confirm the reminder. Check `Jarvis /reminders` before trying again.', **reply_options)
    return True
