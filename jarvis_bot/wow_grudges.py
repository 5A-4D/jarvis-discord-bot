"""A guild-scoped, append-only WoW grudge list; no model or host access needed."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord


class GrudgeError(ValueError):
    pass


@dataclass(frozen=True)
class GrudgeRequest:
    name: str | None = None
    reason: str = ''


HELP = 'Use `Jarvis grudge add Character Name — reason`, or `Jarvis grudge list`.'
LIST = re.compile(r'^(?:(?:show|view|list)\s+(?:(?:the|my|our)\s+)?)?(?:wow\s+)?grudge(?:\s+list|s)?[?.!]*$', re.I)
ADD = re.compile(r'^(?:please\s+)?(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:the\s+)?(?:wow\s+)?grudge\s+list\b(.*)$', re.I)
SEPARATOR = re.compile(r'\s+[—–-]\s+|\s*;\s*|\.\s+|\s+(?:because|for)\s+', re.I)


def validate_entry(name: str, reason: str) -> tuple[str, str]:
    name = ' '.join(unicodedata.normalize('NFKC', name).split()).strip('"“”')
    reason = ' '.join(reason.split())
    if (not 1 <= len(name) <= 80 or not any(c.isalpha() for c in name)
            or any(not (c.isalpha() or c in " '-’") for c in name)
            or name.casefold() in {'him', 'her', 'them', 'this guy', 'this guy too', 'that guy', 'these guys'}):
        raise GrudgeError('Give the character’s actual name. ' + HELP)
    if len(reason) > 500:
        raise GrudgeError('Keep the reason to 500 characters or fewer.')
    return name, reason


def parse_request(prompt: str, *, implicit_add: bool = False) -> GrudgeRequest | None:
    text = prompt.strip()
    if text.startswith('/grudge'):
        text = text[1:]
    if LIST.fullmatch(text):
        return GrudgeRequest()
    explicit = ADD.fullmatch(text)
    if explicit:
        name, reason = explicit.groups()
        reason = re.sub(r'^\s*(?:[—–:;-]|because\b|for\b)\s*', '', reason, flags=re.I)
    else:
        match = re.match(r'^grudge\s+add\s+(.+)$', text, re.I)
        if not match and implicit_add:
            match = re.match(r'^(?:please\s+)?add\s+(.+)$', text, re.I)
        if not match:
            if re.fullmatch(r'grudge\s+add', text, re.I):
                raise GrudgeError(HELP)
            return None
        pieces = SEPARATOR.split(match[1], maxsplit=1)
        name, reason = pieces[0], pieces[1] if len(pieces) == 2 else ''
    name, reason = validate_entry(name, reason)
    return GrudgeRequest(name, reason)


def key(name: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', name).casefold().split())


class GrudgeStore:
    def __init__(self, path: Path, guild_id: int):
        self.path, self.guild_id = path, guild_id

    def _load(self):
        if not self.path.exists():
            return []
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError('oversized store')
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            entries = payload['entries']
            if payload['version'] != 1 or str(payload['guild_id']) != str(self.guild_id) or not isinstance(entries, list) or len(entries) > 500:
                raise ValueError('invalid store')
            for entry in entries:
                validate_entry(entry['name'], entry['reason'])
            return entries
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise GrudgeError('The grudge list could not be read; existing data was preserved.') from error

    def _access(self, request: GrudgeRequest | None, user_id: int, message_id: int):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix('.lock')
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if request else fcntl.LOCK_SH)
            entries = self._load()
            if request is None:
                return entries
            name, reason = validate_entry(request.name or '', request.reason)
            if any(key(item['name']) == key(name) for item in entries):
                return False
            if len(entries) >= 500:
                raise GrudgeError('The grudge list is full. Ask the owner to review it.')
            entries.append({'name': name, 'reason': reason, 'added_by': str(user_id),
                            'message_id': str(message_id),
                            'created_at': datetime.now(timezone.utc).isoformat()})
            payload = {'version': 1, 'guild_id': str(self.guild_id), 'entries': entries}
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, prefix='.wow-grudges-', encoding='utf-8', delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return True

    async def entries(self):
        return await asyncio.to_thread(self._access, None, 0, 0)

    async def add(self, request: GrudgeRequest, user_id: int, message_id: int):
        return await asyncio.to_thread(self._access, request, user_id, message_id)


def render(entries):
    lines = ['**WoW Grudge List**']
    for index, entry in enumerate(entries, 1):
        name = discord.utils.escape_markdown(entry['name'])
        reason = discord.utils.escape_markdown(entry['reason'] or 'No reason supplied.')
        lines.append(f'{index}. **{name}** — {reason}')
    if not entries:
        lines.append('No grudges recorded yet.')
    pages, page = [], lines[0]
    for line in lines[1:]:
        if len(page) + len(line) + 2 > 1900:
            pages.append(page)
            page = '**WoW Grudge List · continued**'
        page += '\n\n' + line
    return pages + [page]


async def handle_request(client, message, prompt: str, *, is_owner: bool) -> bool:
    if getattr(message.author, 'bot', False) or getattr(message, 'webhook_id', None) is not None:
        return False
    settings = client.settings
    guild_id = getattr(settings, 'wow_grudge_guild_id', None)
    if guild_id is None:
        return False
    in_guild = getattr(getattr(message, 'guild', None), 'id', None) == guild_id
    implicit = in_guild and message.channel.id == getattr(settings, 'wow_grudge_channel_id', None)
    async def reply(text):
        await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    try:
        request = parse_request(prompt, implicit_add=implicit)
        if request is None:
            return False
        if not in_guild:
            await reply('Use the WoW grudge list in its Discord server.')
            return True
        if not is_owner and (not getattr(settings, 'wow_grudge_public_enabled', False)
                             or not client._public_channel_allowed(message.channel.id)):
            await reply('Adding to the WoW grudge list is not enabled for public requests here.')
            return True
        store = client._wow_grudges
        if request.name is None:
            for page in render(await store.entries()):
                await reply(page)
        else:
            added = await store.add(request, message.author.id, message.id)
            name = discord.utils.escape_markdown(request.name)
            await reply(f'Added **{name}** to the WoW grudge list.' if added
                        else f'**{name}** is already on the WoW grudge list.')
    except GrudgeError as error:
        await reply(str(error))
    except OSError:
        await reply('I could not save or read the grudge list. No addition was confirmed.')
    return True
