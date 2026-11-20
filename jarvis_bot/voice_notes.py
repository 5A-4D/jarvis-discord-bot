from __future__ import annotations

import asyncio
import audioop
import io
import logging
import re
import wave
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from typing import Awaitable, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .config import Settings
from .memory import MemoryStore, VoiceSegment
from .transcription import LocalVoiceTranscriber, VoiceTranscriptionError

try:
    from discord.ext import voice_recv
except ImportError:  # pragma: no cover - exercised operationally via status
    voice_recv = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)
PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH
VOICE_NOTES_SETTING = "live_voice_notes_enabled"
EMPTY_CHANNEL_TIMEOUT_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True)
class VoiceUtterance:
    segment_id: int
    guild_id: int
    voice_channel_id: int
    user_id: int
    user_name: str
    started_at: datetime
    ended_at: datetime
    transcript: str


@dataclass(frozen=True, slots=True)
class VoiceNotesQuery:
    since: datetime | None
    until: datetime
    label: str
    topic: str | None = None


@dataclass(slots=True)
class _SpeakerBuffer:
    started_at: datetime
    user_name: str
    pcm: bytearray = field(default_factory=bytearray)
    voiced_bytes: int = 0


def extract_voice_prompt(transcript: str, wake_phrase: str) -> str | None:
    """Return text surrounding an exact spoken wake phrase."""
    # Transcribers are not consistent about punctuation inside a spoken phrase:
    # the same audio may become either "Hey Jarvis" or "Hey, Jarvis". Treat
    # whitespace and light punctuation between configured words equivalently.
    wake_parts = wake_phrase.split()
    if not wake_parts:
        return None
    flexible_wake_phrase = r"[\s,;:!?.\-\u2013\u2014]+".join(
        re.escape(part) for part in wake_parts
    )
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){flexible_wake_phrase}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    match = pattern.search(transcript)
    if match is None:
        return None
    before = transcript[: match.start()].strip(" \t\r\n,:;!?.-–—")
    after = transcript[match.end() :].strip(" \t\r\n,:;!?.-–—")
    if before.casefold() in {"hey", "hi", "okay", "ok"}:
        before = ""
    return " ".join(part for part in (before, after) if part)


def voice_summary_minutes(text: str, default: int = 30) -> int | None:
    """Parse a slash command or natural request for recent VC notes."""
    normalized = " ".join(text.casefold().strip().split())
    if normalized.startswith("/vc-summary"):
        remainder = normalized[len("/vc-summary") :].strip(" :=")
        if not remainder:
            return default
        match = re.fullmatch(
            r"(?:(?:minutes?|mins?)\s*[:=]?\s*)?(\d+)\s*"
            r"(?:minutes?|mins?|m)?",
            remainder,
        )
        if match is None:
            raise ValueError("Use `/vc-summary 30` or `/vc-summary minutes:30`.")
        return int(match.group(1))

    match = re.search(
        r"^(?:(?:can|could|would|will)\s+you\s+|please\s+)?"
        r"(?:summari[sz]e|summary)\b.*?\b(?:last|past)\s+(\d+)\s*"
        r"(minutes?|mins?|hours?|hrs?)\b",
        normalized,
    )
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    return amount * 60 if unit.startswith(("hour", "hr")) else amount


_CLOCK = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?"
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_DAY_WORDS = "|".join(("today", "yesterday", *_WEEKDAYS))
_VOICE_REFERENCE = re.compile(
    r"\b(?:voice[\s-]*chat|voice\s+channel|voice|vc|void|call|"
    r"transcripts?|transcriptions?|voice\s+notes?)\b"
)
_VOICE_RETRIEVAL_REQUEST = re.compile(
    r"(?:^|\b(?:can|could|would|will)\s+you\s+|\bplease\s+)"
    r"(?:recap|summari[sz]e|catch\s+me\s+up)\b|"
    r"\b(?:give|show|send|read|pull|get|find|need)\s+(?:me\s+)?(?:the\s+|a\s+)?"
    r"(?:(?:voice|vc|void)\s+)?(?:notes?|summary|transcripts?|recap|rundown)\b|"
    r"^(?:(?:voice|vc|void)\s+)?(?:notes?|summary|transcripts?)\s+"
    r"(?:from|for|of|about|regarding|today|yesterday|last|past)\b|"
    r"\bwhat(?:\s+all)?\s+(?:was|has\s+been)\s+"
    r"(?:said|discussed|talked\s+about)\b|"
    r"\bwhat\s+(?:happened|were\s+we\s+talking\s+about)\b|"
    r"\bwho\s+(?:said|spoke|talked)\b|"
    r"\banything\s+(?:said|discussed|mentioned)\b"
)
_SPEECH_REQUEST = re.compile(
    r"\b(?:did|has|have|what\s+did|what\s+has)\s+"
    r"(?P<speaker>[a-z0-9_@.'-]+(?:\s+[a-z0-9_@.'-]+){0,3})\s+"
    r"(?:say|said|mention(?:ed)?|talk(?:ed)?|speak|spoke)\b"
)
_TEMPORAL_REFERENCE = re.compile(
    rf"\b(?:last|past|today|yesterday|{('|'.join(_WEEKDAYS))}|"
    r"from|between|around)\b|\b\d{4}-\d{2}-\d{2}\b"
)
_MONTHS = {
    name.casefold(): number
    for number, name in enumerate(
        (
            "",
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
    )
    if name
}


def _clock_value(raw: str, inherited_meridiem: str | None = None) -> time:
    compact = raw.casefold().replace(".", "").replace(" ", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?([ap]m)?", compact)
    if match is None:
        raise ValueError(f"I could not read the time `{raw}`.")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3) or inherited_meridiem
    if minute > 59:
        raise ValueError(f"I could not read the time `{raw}`.")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"I could not read the time `{raw}`.")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        raise ValueError(f"I could not read the time `{raw}`.")
    return time(hour, minute)


def _meridiem(raw: str) -> str | None:
    match = re.search(r"([ap])\.?m\.?\s*$", raw.casefold())
    return f"{match.group(1)}m" if match else None


def _selected_day(text: str, local_now: datetime) -> date | None:
    if re.search(r"\byesterday\b", text):
        return local_now.date() - timedelta(days=1)
    if re.search(r"\btoday\b", text):
        return local_now.date()

    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError as error:
            raise ValueError("That calendar date is not valid.") from error

    month_names = "|".join(_MONTHS)
    named = re.search(
        rf"\b({month_names})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?\b", text
    )
    if named:
        year = int(named.group(3) or local_now.year)
        try:
            selected = date(year, _MONTHS[named.group(1)], int(named.group(2)))
        except ValueError as error:
            raise ValueError("That calendar date is not valid.") from error
        if named.group(3) is None and selected > local_now.date():
            selected = selected.replace(year=year - 1)
        return selected

    for name, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", text):
            days_ago = (local_now.weekday() - weekday) % 7
            return local_now.date() - timedelta(days=days_ago)
    return None


def _query_label(since: datetime, until: datetime, zone: ZoneInfo) -> str:
    local_since = since.astimezone(zone)
    local_until = until.astimezone(zone)
    zone_name = local_since.tzname() or str(zone)
    if local_since.date() == local_until.date():
        return (
            f"{local_since:%Y-%m-%d} {local_since:%H:%M}–"
            f"{local_until:%H:%M} {zone_name}"
        )
    return (
        f"{local_since:%Y-%m-%d %H:%M} to "
        f"{local_until:%Y-%m-%d %H:%M} {zone_name}"
    )


def voice_notes_query(
    text: str,
    *,
    now: datetime | None = None,
    timezone_name: str = "UTC",
    retention_days: int = 0,
    default_minutes: int = 30,
) -> VoiceNotesQuery | None:
    """Parse stored-note requests into an exact UTC range and optional topic."""
    normalized = " ".join(text.casefold().strip().split())
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"The configured VC-notes timezone is invalid: {timezone_name}.") from error
    utc_now = now or datetime.now(timezone.utc)
    if utc_now.tzinfo is None:
        utc_now = utc_now.replace(tzinfo=timezone.utc)
    utc_now = utc_now.astimezone(timezone.utc)
    local_now = utc_now.astimezone(zone)

    legacy_minutes = voice_summary_minutes(normalized, default_minutes)
    if legacy_minutes is not None:
        since = utc_now - timedelta(minutes=legacy_minutes)
        return VoiceNotesQuery(
            since=since,
            until=utc_now,
            label=_query_label(since, utc_now, zone),
        )
    explicit_notes = normalized == "notes" or normalized.startswith("notes ")
    speech_match = _SPEECH_REQUEST.search(normalized)
    voice_reference = _VOICE_REFERENCE.search(normalized)
    retrieval_request = _VOICE_RETRIEVAL_REQUEST.search(normalized)
    natural_request = not explicit_notes and bool(
        (voice_reference and retrieval_request)
        or (
            retrieval_request
            and re.search(r"\b(?:notes?|transcripts?)\b", normalized)
        )
        or (speech_match and _TEMPORAL_REFERENCE.search(normalized))
        or (speech_match and voice_reference)
        or (speech_match and re.search(r"\bto\s+you\b", normalized))
    )
    if not explicit_notes and not natural_request:
        return None

    remainder = (
        normalized[5:].strip(" ,:;-") if explicit_notes else normalized
    )
    topic: str | None = None
    topic_match = re.search(
        rf"\b(?:about|regarding|mentioning)\s+(.+?)"
        rf"(?=\s+\b(?:{_DAY_WORDS}|from|between|at|around|last|past)\b|$)",
        remainder,
    )
    if topic_match:
        topic = topic_match.group(1).strip(" .?!") or None
        temporal = " ".join(
            part
            for part in (
                remainder[: topic_match.start()].strip(" ,:;-"),
                remainder[topic_match.end() :].strip(" ,:;-"),
            )
            if part
        )
    else:
        temporal = remainder
    if topic is None and speech_match is not None:
        candidate = speech_match.group("speaker").strip(" .?!")
        if candidate not in {
            "anyone",
            "anybody",
            "someone",
            "somebody",
            "i",
            "you",
            "we",
            "they",
            "he",
            "she",
        }:
            topic = candidate
    all_history = temporal in {"all", "all time", "everything", "all history"}

    duration = re.search(
        r"\b(?:last|past)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b",
        temporal,
    )
    selected_day = _selected_day(temporal, local_now)
    if duration:
        amount = int(duration.group(1))
        unit = duration.group(2)
        if unit.startswith(("hour", "hr")):
            delta = timedelta(hours=amount)
        elif unit.startswith("day"):
            delta = timedelta(days=amount)
        else:
            delta = timedelta(minutes=amount)
        since = utc_now - delta
        until = utc_now
    elif selected_day is not None:
        local_since = datetime.combine(selected_day, time.min, zone)
        local_until = local_since + timedelta(days=1)
        if selected_day == local_now.date():
            local_until = local_now
        since = local_since.astimezone(timezone.utc)
        until = local_until.astimezone(timezone.utc)
    elif natural_request and not all_history:
        since = utc_now - timedelta(minutes=default_minutes)
        until = utc_now
    elif topic or all_history:
        since = (
            utc_now - timedelta(days=retention_days)
            if retention_days > 0
            else None
        )
        until = utc_now
    else:
        since = utc_now - timedelta(minutes=default_minutes)
        until = utc_now

    range_match = re.search(
        rf"\b(?:from|between)\s+({_CLOCK})\s+(?:to|until|and|-)\s+({_CLOCK})\b",
        temporal,
    )
    at_match = re.search(rf"\b(?:at|around)\s+({_CLOCK})\b", temporal)
    if range_match or at_match:
        day = selected_day or local_now.date()
        if range_match:
            start_raw, end_raw = range_match.group(1), range_match.group(2)
            start_clock = _clock_value(start_raw, _meridiem(end_raw))
            end_clock = _clock_value(end_raw, _meridiem(start_raw))
            local_since = datetime.combine(day, start_clock, zone)
            local_until = datetime.combine(day, end_clock, zone)
            if local_until <= local_since:
                local_until += timedelta(days=1)
        else:
            local_since = datetime.combine(
                day, _clock_value(at_match.group(1)), zone
            )
            local_until = local_since + timedelta(hours=1)
        since = local_since.astimezone(timezone.utc)
        until = min(local_until.astimezone(timezone.utc), utc_now)

    if topic is None and temporal and not natural_request and not (
        duration
        or selected_day is not None
        or range_match
        or at_match
        or all_history
        or temporal in {"summary", "summarise", "summarize"}
    ):
        topic = temporal.strip(" .?!") or None
        since = (
            utc_now - timedelta(days=retention_days)
            if retention_days > 0
            else None
        )
        until = utc_now

    return VoiceNotesQuery(
        since=since,
        until=until,
        label=(
            _query_label(since, until, zone)
            if since is not None
            else "all retained history"
        ),
        topic=topic,
    )


def voice_notes_command(text: str) -> tuple[str, int | None] | None:
    normalized = " ".join(text.casefold().strip().split())
    if normalized == "ntoggle":
        return ("toggle", None)

    voice_target = bool(
        re.search(r"\b(?:voice(?:[\s-]*chat)?|vc|void)\b", normalized)
        and re.search(
            r"\b(?:notes?|transcripts?|transcription|transcribe|transcribing|"
            r"logging|log|recording|record|listening|listen)\b",
            normalized,
        )
    )
    if voice_target:
        if re.search(
            r"\b(?:turn\s+off|stop|pause|disable|do\s+not|don't|quit)\b",
            normalized,
        ):
            return ("pause", None)
        if re.search(
            r"\b(?:turn\s+on|start|resume|enable|go\s+back\s+to)\b",
            normalized,
        ):
            return ("resume", None)
        if re.search(r"\btoggle\b", normalized):
            return ("toggle", None)
        if re.search(
            r"\b(?:status|working|running|enabled|active|connected|listening|"
            r"logging|recording|transcribing)\b",
            normalized,
        ) and re.search(r"\b(?:what|is|are|check|show|give)\b", normalized):
            return ("status", None)

        if re.search(r"\b(?:delete|erase|clear|remove)\b", normalized):
            if re.search(r"\b(?:all|everything|entire)\b", normalized):
                return ("delete_all", None)
            window = re.search(
                r"\b(?:last|past)\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b",
                normalized,
            )
            if window is not None:
                amount = int(window.group(1))
                if window.group(2).startswith(("hour", "hr")):
                    amount *= 60
                return ("delete", amount)

    command, _, remainder = normalized.partition(" ")
    if command != "/vc-notes":
        return None
    remainder = remainder.strip()
    if not remainder or remainder == "status":
        return ("status", None)
    if remainder in {"on", "off", "pause", "resume"}:
        return (remainder, None)
    if remainder == "delete all":
        return ("delete_all", None)
    match = re.fullmatch(
        r"delete\s+(?:(?:minutes?|mins?)\s*[:=]?\s*)?(\d+)", remainder
    )
    if match is not None:
        return ("delete", int(match.group(1)))
    return ("usage", None)


def pcm_to_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(PCM_CHANNELS)
        wav.setsampwidth(PCM_SAMPLE_WIDTH)
        wav.setframerate(PCM_SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def format_voice_context(
    segments: list[VoiceSegment],
    max_chars: int,
    *,
    topic: str | None = None,
    user_names: dict[int, str] | None = None,
) -> str:
    resolved_names = user_names or {}

    def speaker_text(segment: VoiceSegment) -> str:
        return resolved_names.get(segment.user_id, segment.user_name)

    if topic:
        topic_tokens = {
            token
            for token in re.findall(r"[a-z0-9']+", topic.casefold())
            if len(token) >= 3
        }
        matching = {
            index
            for index, segment in enumerate(segments)
            if topic.casefold()
            in f"{speaker_text(segment)} {segment.transcript}".casefold()
            or topic_tokens.intersection(
                re.findall(
                    r"[a-z0-9']+",
                    f"{speaker_text(segment)} {segment.transcript}".casefold(),
                )
            )
        }
        if matching:
            contextual = {
                nearby
                for index in matching
                for nearby in range(max(0, index - 2), min(len(segments), index + 3))
            }
            segments = [segments[index] for index in sorted(contextual)]

    lines = [
        f"[{segment.started_at} to {segment.ended_at}] "
        f"{speaker_text(segment)} (user {segment.user_id}): {segment.transcript}"
        for segment in segments
    ]
    selected: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + (1 if selected else 0)
        if selected and used + cost > max_chars:
            break
        if not selected and len(line) > max_chars:
            line = line[-max_chars:]
            cost = len(line)
        selected.append(line)
        used += cost
    return "\n".join(reversed(selected))


class LiveVoiceNotes:
    """Receive one Discord VC, transcribe locally, and emit wake requests."""

    def __init__(
        self,
        *,
        settings: Settings,
        memory: MemoryStore,
        transcriber: LocalVoiceTranscriber,
        request_handler: Callable[[VoiceUtterance, str], Awaitable[None]],
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.transcriber = transcriber
        self.request_handler = request_handler
        self.client: discord.Client | None = None
        self.voice_client: object | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_id = str(uuid4())
        self._buffers: dict[int, _SpeakerBuffer] = {}
        self._flush_tasks: dict[int, asyncio.Task[None]] = {}
        self._followups: dict[int, float] = {}
        self._queue: asyncio.Queue[
            tuple[int, str, datetime, datetime, bytes]
        ] = asyncio.Queue(maxsize=100)
        self._worker_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._enabled: bool | None = None
        self._last_error: str | None = None
        self._last_transcript_at: datetime | None = None
        self._last_prune_at = 0.0
        self._empty_since: float | None = None

    async def start(self, client: discord.Client) -> None:
        self.client = client
        self._loop = asyncio.get_running_loop()
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(
                self._transcription_worker(), name="jarvis-vc-transcription"
            )
        if self._supervisor_task is None:
            self._supervisor_task = asyncio.create_task(
                self._supervise(), name="jarvis-vc-supervisor"
            )

    async def close(self) -> None:
        for task in list(self._flush_tasks.values()):
            task.cancel()
        await self._flush_all()
        for task in (self._supervisor_task, self._worker_task):
            if task is not None:
                task.cancel()
        await self._disconnect()

    async def enabled(self) -> bool:
        # Read the persistent control on each supervisor pass so a change made
        # outside this process also takes effect without restarting the bot.
        saved = await self.memory.get_runtime_setting(VOICE_NOTES_SETTING)
        self._enabled = (
            self.settings.live_voice_enabled if saved is None else saved == "true"
        )
        return self._enabled

    async def set_enabled(self, enabled: bool) -> None:
        await self.memory.set_runtime_setting(
            VOICE_NOTES_SETTING, "true" if enabled else "false"
        )
        self._enabled = enabled
        self._empty_since = None
        if enabled:
            await self.ensure_connected()
        else:
            await self._flush_all()
            await self._disconnect()

    async def ensure_connected(self) -> bool:
        if not await self.enabled():
            if self.voice_client is not None:
                await self._flush_all()
                await self._disconnect()
            return False
        if voice_recv is None:
            self._last_error = "discord-ext-voice-recv-dave is not installed"
            return False
        if self.client is None:
            self._last_error = "Discord client is not ready"
            return False
        channel_id = self.settings.live_voice_channel_id
        if channel_id is None:
            self._last_error = "LIVE_VOICE_CHANNEL_ID is not configured"
            return False

        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound) as error:
                self._last_error = f"cannot fetch voice channel: {error}"
                return False
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            self._last_error = "configured live voice channel is not a voice channel"
            return False

        current = self.voice_client
        if current is not None:
            if getattr(current, "is_connected", lambda: False)():
                if getattr(getattr(current, "channel", None), "id", None) == channel_id:
                    try:
                        if not getattr(current, "is_listening", lambda: False)():
                            self._begin_listening(current)
                    except Exception as error:
                        self._last_error = f"voice receiver restart failed: {error}"
                        LOGGER.warning(
                            "Could not restart live voice receiver; reconnecting: %s",
                            error,
                        )
                        await self._disconnect()
                    else:
                        self._last_error = None
                        if await self._leave_if_empty(channel):
                            return False
                        return True
                else:
                    await self._disconnect()
            else:
                # discord.py retains a failed VoiceClient in its per-guild cache.
                # Calling channel.connect() while that stale object remains raises
                # "Already connected to a voice channel" forever, even though its
                # voice transport is down. Force cleanup before attempting a fresh
                # connection so the supervisor can recover without a process restart.
                LOGGER.warning("Discarding stale live voice client before reconnecting")
                await self._disconnect()

        try:
            current = await channel.connect(
                cls=voice_recv.VoiceRecvClient,
                self_mute=True,
                self_deaf=False,
                reconnect=True,
            )
            self.voice_client = current
            self._begin_listening(current)
            self._last_error = None
            LOGGER.info("Listening for voice notes in #%s (%s)", channel.name, channel.id)
            if await self._leave_if_empty(channel):
                return False
            return True
        except Exception as error:
            self._last_error = f"voice connection failed: {error}"
            LOGGER.exception("Could not connect live voice notes")
            return False

    def observe_voice_state(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Reset the empty period even for visits between supervisor passes."""
        if member.bot:
            return
        before_id = getattr(before.channel, "id", None)
        after_id = getattr(after.channel, "id", None)
        if before_id != after_id and self.settings.live_voice_channel_id in {
            before_id, after_id
        }:
            self._empty_since = None

    async def _leave_if_empty(self, channel: discord.VoiceChannel) -> bool:
        if any(not member.bot for member in channel.members):
            self._empty_since = None
            return False
        now = monotonic()
        if self._empty_since is None:
            self._empty_since = now
            return False
        if now - self._empty_since < EMPTY_CHANNEL_TIMEOUT_SECONDS:
            return False
        # Persist the disabled state so the supervisor and later restarts cannot
        # immediately rejoin. An explicit join request enables listening again.
        await self.set_enabled(False)
        LOGGER.info("Left voice channel %s after five minutes without people", channel.id)
        return True

    def _begin_listening(self, voice_client: object) -> None:
        if voice_recv is None:
            return
        sink = voice_recv.BasicSink(self._sink_write, decode=True)
        voice_client.listen(sink, after=self._sink_after)

    async def _disconnect(self) -> None:
        current = self.voice_client
        self.voice_client = None
        self._empty_since = None
        if current is None:
            return
        try:
            if getattr(current, "is_listening", lambda: False)():
                current.stop_listening()
            await current.disconnect(force=True)
        except Exception as error:
            LOGGER.warning("Voice disconnect failed: %s", error)

    def _sink_write(self, user: discord.User | discord.Member | None, data: object) -> None:
        if user is None or getattr(user, "bot", False):
            return
        pcm = bytes(getattr(data, "pcm", b""))
        if not pcm or self._loop is None:
            return
        # Discord's Member.display_name is server-nickname aware. Persist the
        # account username instead so attribution remains stable when nicknames
        # are changed or deliberately shared by several members.
        user_name = getattr(user, "name", None) or str(user)
        self._loop.call_soon_threadsafe(
            self._accept_packet, int(user.id), user_name, pcm
        )

    def _sink_after(self, error: Exception | None) -> None:
        if error is not None:
            self._last_error = f"voice receiver stopped: {error}"
            LOGGER.error("Voice receiver stopped: %s", error)

    def _accept_packet(self, user_id: int, user_name: str, pcm: bytes) -> None:
        try:
            rms = audioop.rms(pcm, PCM_SAMPLE_WIDTH)
        except audioop.error:
            return
        buffer = self._buffers.get(user_id)
        voiced = rms >= self.settings.live_voice_rms_threshold
        if buffer is None:
            if not voiced:
                return
            buffer = _SpeakerBuffer(
                started_at=datetime.now(timezone.utc), user_name=user_name
            )
            self._buffers[user_id] = buffer

        buffer.pcm.extend(pcm)
        if voiced:
            buffer.voiced_bytes += len(pcm)
            previous = self._flush_tasks.pop(user_id, None)
            if previous is not None:
                previous.cancel()
            self._flush_tasks[user_id] = asyncio.create_task(
                self._flush_after_silence(user_id),
                name=f"jarvis-vc-flush-{user_id}",
            )

        duration = len(buffer.pcm) / PCM_BYTES_PER_SECOND
        if duration >= self.settings.live_voice_segment_max_seconds:
            task = self._flush_tasks.pop(user_id, None)
            if task is not None:
                task.cancel()
            self._flush_user(user_id)

    async def _flush_after_silence(self, user_id: int) -> None:
        try:
            await asyncio.sleep(self.settings.live_voice_silence_seconds)
            self._flush_tasks.pop(user_id, None)
            self._flush_user(user_id)
        except asyncio.CancelledError:
            return

    def _flush_user(self, user_id: int) -> None:
        buffer = self._buffers.pop(user_id, None)
        if buffer is None:
            return
        voiced_seconds = buffer.voiced_bytes / PCM_BYTES_PER_SECOND
        if voiced_seconds < self.settings.live_voice_min_speech_seconds:
            return
        item = (
            user_id,
            buffer.user_name,
            buffer.started_at,
            datetime.now(timezone.utc),
            pcm_to_wav(bytes(buffer.pcm)),
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._last_error = "transcription queue is full; an utterance was dropped"
            LOGGER.error(self._last_error)

    async def _flush_all(self) -> None:
        for user_id in list(self._buffers):
            self._flush_user(user_id)

    async def _transcription_worker(self) -> None:
        while True:
            user_id, user_name, started_at, ended_at, audio = await self._queue.get()
            try:
                transcript = await self.transcriber.transcribe(audio, suffix=".wav")
                await self._record_transcript(
                    user_id, user_name, started_at, ended_at, transcript
                )
            except VoiceTranscriptionError as error:
                self._last_error = f"transcription failed: {error}"
                LOGGER.warning("Live voice transcription failed: %s", error)
            except Exception as error:
                self._last_error = f"voice processing failed: {error}"
                LOGGER.exception("Live voice processing failed")
            finally:
                self._queue.task_done()

    async def _record_transcript(
        self,
        user_id: int,
        user_name: str,
        started_at: datetime,
        ended_at: datetime,
        transcript: str,
    ) -> None:
        prompt = extract_voice_prompt(
            transcript, self.settings.live_voice_wake_phrase
        )
        now = monotonic()
        followup_until = self._followups.get(user_id)
        if prompt is None and followup_until is not None:
            if now <= followup_until:
                prompt = transcript.strip()
            self._followups.pop(user_id, None)

        is_wake_request = prompt is not None
        segment_id = await self.memory.store_voice_segment(
            VoiceSegment(
                id=None,
                session_id=self._session_id,
                guild_id=self._guild_id(),
                voice_channel_id=self.settings.live_voice_channel_id or 0,
                user_id=user_id,
                user_name=user_name,
                started_at=started_at.isoformat(),
                ended_at=ended_at.isoformat(),
                transcript=transcript,
                is_wake_request=is_wake_request,
                request_text=prompt or None,
            )
        )
        self._last_transcript_at = ended_at

        if prompt is None:
            return
        if not prompt:
            self._followups[user_id] = (
                now + self.settings.live_voice_followup_seconds
            )
            await self.notify(f"🎙️ Listening for {user_name}…")
            return

        utterance = VoiceUtterance(
            segment_id=segment_id,
            guild_id=self._guild_id(),
            voice_channel_id=self.settings.live_voice_channel_id or 0,
            user_id=user_id,
            user_name=user_name,
            started_at=started_at,
            ended_at=ended_at,
            transcript=transcript,
        )
        await self.request_handler(utterance, prompt)

    def _guild_id(self) -> int:
        if self.voice_client is not None:
            guild = getattr(self.voice_client, "guild", None)
            if guild is not None:
                return int(guild.id)
        if self.client is not None and self.settings.live_voice_channel_id is not None:
            channel = self.client.get_channel(self.settings.live_voice_channel_id)
            guild = getattr(channel, "guild", None)
            if guild is not None:
                return int(guild.id)
        return 0

    async def output_channel(self) -> discord.TextChannel | discord.Thread | None:
        if self.client is None or self.settings.live_voice_text_channel_id is None:
            return None
        channel = self.client.get_channel(self.settings.live_voice_text_channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(
                    self.settings.live_voice_text_channel_id
                )
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def voice_chat_channel(
        self,
    ) -> discord.VoiceChannel | discord.StageChannel | None:
        """Return the configured VC itself so wake replies land in its text chat."""
        if self.client is None or self.settings.live_voice_channel_id is None:
            return None
        channel = self.client.get_channel(self.settings.live_voice_channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(
                    self.settings.live_voice_channel_id
                )
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        return None

    async def notify(self, text: str) -> None:
        channel = await self.output_channel()
        if channel is None:
            self._last_error = "LIVE_VOICE_TEXT_CHANNEL_ID is unavailable"
            return
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def status_text(self) -> str:
        enabled = await self.enabled()
        connected = bool(
            self.voice_client is not None
            and getattr(self.voice_client, "is_connected", lambda: False)()
        )
        listening = bool(
            connected
            and getattr(self.voice_client, "is_listening", lambda: False)()
        )
        count = await self.memory.voice_segment_count(
            self._guild_id(), self.settings.live_voice_channel_id or 0
        )
        last = (
            self._last_transcript_at.isoformat()
            if self._last_transcript_at is not None
            else "none this process"
        )
        retention = (
            "until manually deleted"
            if self.settings.live_voice_retention_days == 0
            else f"{self.settings.live_voice_retention_days} days"
        )
        detail = (
            f"Live VC notes are {'enabled' if enabled else 'disabled'}; "
            f"connected: {'yes' if connected else 'no'}; "
            f"receiving: {'yes' if listening else 'no'}. "
            f"Stored segments: {count}; queued: {self._queue.qsize()}; "
            f"last transcript: {last}; retention: {retention}."
        )
        if self._last_error:
            detail += f" Last fault: {self._last_error}"
        return detail

    async def _supervise(self) -> None:
        while True:
            try:
                await self.ensure_connected()
                now = monotonic()
                if (
                    self.settings.live_voice_retention_days > 0
                    and now - self._last_prune_at >= 3_600
                ):
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        days=self.settings.live_voice_retention_days
                    )
                    removed = await self.memory.prune_voice_segments(
                        cutoff.isoformat()
                    )
                    if removed:
                        LOGGER.info("Pruned %s expired voice transcript segments", removed)
                    self._last_prune_at = now
            except asyncio.CancelledError:
                return
            except Exception as error:
                self._last_error = f"voice supervisor failed: {error}"
                LOGGER.exception("Live voice supervisor failed")
            await asyncio.sleep(20)
