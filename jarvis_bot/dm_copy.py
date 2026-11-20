"""Copy new human DMs to one configured owner without invoking the assistant."""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, unquote, urlsplit

import discord

LOGGER = logging.getLogger(__name__)


def _private_calendar_text(text: str, feed_url: str) -> str:
    # Never send raw calendar data, even to the owner. Do not fetch attachments.
    if re.search(r"BEGIN:(?:VCALENDAR|VEVENT|VTIMEZONE)", text, re.IGNORECASE):
        return "[Calendar content withheld]"
    if feed_url:
        parts = urlsplit(feed_url)
        secrets = {feed_url, unquote(feed_url)}
        secrets.update(part for part in parts.path.split("/") if len(part) >= 16)
        secrets.update(value for _, value in parse_qsl(parts.query) if value)
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "[Calendar secret withheld]")
            text = text.replace(unquote(secret), "[Calendar secret withheld]")
    return re.sub(
        r"https?://outlook\.office365\.com/[^\s<>]+",
        "[Calendar URL withheld]", text, flags=re.IGNORECASE,
    )


def dm_copy_text(message: discord.Message, feed_url: str = "") -> str:
    author = message.author
    lines = [
        "📨 **Incoming DM to Jarvis**",
        f"From: {author} (user ID: {author.id})",
        f"Sent: {message.created_at.isoformat()}",
        f"Message ID: {message.id}",
    ]
    reply_id = getattr(message.reference, "message_id", None)
    if reply_id:
        lines.append(f"Reply to message: {reply_id}")
    lines.extend(["", message.content or "[No text]"])
    for attachment in message.attachments:
        if (attachment.filename.lower().endswith((".ics", ".ical", ".ifb"))
                or (attachment.content_type or "").lower().startswith("text/calendar")):
            lines.append("[Calendar attachment withheld]")
        else:
            lines.append(f"Attachment: {attachment.filename}\n{attachment.url}")
    for sticker in message.stickers:
        lines.append(f"Sticker: {sticker.name}\n{sticker.url}")
    return _private_calendar_text("\n".join(lines), feed_url)


async def copy_incoming_dm(client: discord.Client, message: discord.Message) -> None:
    settings = client.settings
    owner_id = settings.dm_copy_owner_id
    if (owner_id not in settings.authorized_user_ids
            or message.guild is not None or message.author.bot
            or message.webhook_id is not None or message.author.id == owner_id):
        return
    # No prompts, replies or attachment contents are written to logs or AI context.
    text = dm_copy_text(message, getattr(settings, "work_calendar_ics_url", ""))
    try:
        owner = client.get_user(owner_id) or await client.fetch_user(owner_id)
        # Fixed slices preserve the complete text, including whitespace, and stay
        # below Discord's message limit (including astral Unicode characters).
        chunks: list[str] = []
        chunk = ""
        units = 0
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if units + width > 1900:
                chunks.append(chunk)
                chunk, units = "", 0
            chunk += char
            units += width
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            await owner.send(chunk, allowed_mentions=discord.AllowedMentions.none(),
                             suppress_embeds=True)
    except discord.HTTPException as error:
        LOGGER.warning("DM copy failed source=%s owner=%s error=%s",
                       message.id, owner_id, type(error).__name__)
