"""Owner-armed, one-use GIF following ExampleMember' next answered question."""
from __future__ import annotations

import logging
import re

import discord

from .memory import MemoryStore

TARGET_USER_ID = 900000000000000005
SETTING_KEY = "examplemember_question_gif_20261110"
GIF_URL = "https://klipy.com/gifs/and-shove-it-up-your-butt"
LOGGER = logging.getLogger(__name__)


def is_question(prompt: str) -> bool:
    text = prompt.strip()
    if not text or text.startswith("/"):
        return False
    return "?" in text or re.match(
        r"^(?:what|who|when|where|why|how|which|whose|can|could|would|will|"
        r"should|do|does|did|is|are|was|were|have|has|explain|compare|define|"
        r"describe|tell|help|show|summari[sz]e)\b", text, re.IGNORECASE,
    ) is not None


async def append_once(memory: MemoryStore, message: discord.Message, prompt: str) -> None:
    if (message.author.id != TARGET_USER_ID or message.author.bot
            or not is_question(prompt)):
        return
    if not await memory.consume_runtime_setting(SETTING_KEY, "armed"):
        return
    # Consume before sending: ambiguous Discord failures must never replay the joke.
    try:
        await message.channel.send(
            GIF_URL, suppress_embeds=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        LOGGER.warning("Discord rejected the one-use reply GIF; it will not be retried.")
