"""Read Outlook availability without retaining event details or the feed on disk."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from icalendar import Calendar
import recurring_ical_events

from .calendar_timeline import TimelineDay, busy_cells, comparison_pages, union_seconds as _union_seconds

MAX_FEED_BYTES = 2 * 1024 * 1024
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class CalendarError(Exception):
    """Safe, content-free error suitable for Discord."""


@dataclass(frozen=True)
class ScheduleQuery:
    start: date
    days: int = 1


@dataclass(frozen=True)
class Meeting:
    start: datetime
    end: datetime
    is_huddle: bool = False


@dataclass(frozen=True)
class Availability:
    meetings: tuple[Meeting, ...]
    all_day: tuple[tuple[date, date], ...] = ()


def schedule_query(prompt: str, *, timezone_name: str = "America/Chicago",
                   now: datetime | None = None) -> ScheduleQuery | None:
    """Resolve schedule dates locally; qualified weekdays use Monday-based weeks."""
    text = prompt.casefold().strip().strip("?!., ")
    text = re.sub(r"^(?:hey\s+)?(?:(?:can|could|would) you\s+)?(?:please\s+)?", "", text)
    text = re.sub(r"\s+please$", "", text)
    match = re.fullmatch(
        r"(?:(?:show|give|tell)(?: me)?\s+|what(?:'s| is)\s+)?"
        r"(?:my\s+)?(?:work\s+)?(?:schedule|schduale|schdule|sch|calendar|meetings)"
        r"(?:\s+(?:(?:for|on)\s+)?(today|tomorrow|yesterday|this week|next week|week|"
        r"\d{4}-\d{2}-\d{2}|(?:(?:this|next)\s+)?(?:"
        + "|".join(WEEKDAYS) + r")))?",
        text,
    )
    if match is None:
        return None
    today = (now or datetime.now(ZoneInfo(timezone_name))).astimezone(ZoneInfo(timezone_name)).date()
    period = match[1] or "today"
    if period in {"week", "this week", "next week"}:
        monday = today - timedelta(days=today.weekday())
        return ScheduleQuery(monday + timedelta(days=7 if period == "next week" else 0), 5)
    if period in {"today", "tomorrow", "yesterday"}:
        return ScheduleQuery(today + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[period]))
    parts = period.split()
    weekday = parts[-1]
    if weekday in WEEKDAYS:
        target = WEEKDAYS.index(weekday)
        if len(parts) == 2:
            # Match the Monday-based boundaries used by this/next week.
            monday = today - timedelta(days=today.weekday())
            offset = target + (7 if parts[0] == "next" else 0)
            return ScheduleQuery(monday + timedelta(days=offset))
        return ScheduleQuery(today + timedelta(days=(target - today.weekday()) % 7))
    try:
        return ScheduleQuery(date.fromisoformat(period))
    except ValueError:
        raise CalendarError("Use a valid date, for example: schedule 2026-11-03.") from None


def parse_availability(data: bytes, query: ScheduleQuery, timezone_name: str, *,
                       free_series_hashes: frozenset[str] = frozenset()) -> Availability:
    """Only timing survives parsing; no titles, attendees, URLs or descriptions."""
    if len(data) > MAX_FEED_BYTES or b"BEGIN:VCALENDAR" not in data[:100]:
        raise CalendarError("The calendar feed did not return a supported calendar.")
    zone = ZoneInfo(timezone_name)
    try:
        calendar = Calendar.from_ical(data)
        if calendar.name != "VCALENDAR" or calendar.errors:
            raise ValueError("Invalid calendar")
        # Strip unrelated properties before expanding any recurrence rules.
        allowed = {"UID", "DTSTART", "DTEND", "DURATION", "RRULE", "RDATE", "EXDATE",
                   "RECURRENCE-ID", "SEQUENCE", "DTSTAMP", "STATUS", "TRANSP",
                   "X-MICROSOFT-CDO-BUSYSTATUS"}
        for component in calendar.subcomponents:
            if component.name == "VEVENT":
                if component.errors:
                    raise ValueError("Invalid event")
                if "DTSTART" not in component:
                    if (str(component.get("STATUS", "")).upper() != "CANCELLED"
                            or "RECURRENCE-ID" not in component):
                        raise ValueError("Invalid event")
                    # A cancellation can identify its occurrence without a new
                    # start. Expansion needs DTSTART to match the exception;
                    # dropping it would incorrectly restore the busy occurrence.
                    cancelled_start = component.decoded("RECURRENCE-ID")
                    if not isinstance(cancelled_start, date):
                        raise ValueError("Invalid cancellation")
                    component.add("DTSTART", cancelled_start)
                for key in list(component):
                    if key not in allowed:
                        del component[key]
                component.subcomponents.clear()
        start = datetime.combine(query.start, time.min, zone)
        end = datetime.combine(query.start + timedelta(days=query.days), time.min, zone)
        events = recurring_ical_events.of(calendar).between(start, end)
        meetings: list[Meeting] = []
        all_day: list[tuple[date, date]] = []
        for event in events:
            if (str(event.get("STATUS", "")).upper() == "CANCELLED"
                    or str(event.get("TRANSP", "")).upper() == "TRANSPARENT"
                    or str(event.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper() == "FREE"):
                continue
            first = event.decoded("DTSTART")
            last = event.decoded("DTEND", None)
            if last is None:
                duration = event.decoded("DURATION", None)
                if duration is not None:
                    last = first + duration
                elif not isinstance(first, datetime):
                    last = first + timedelta(days=1)
                else:
                    raise ValueError("Timed event without duration")
            if not isinstance(first, datetime):
                all_day.append((first, last.date() if isinstance(last, datetime) else last))
                continue
            if not isinstance(last, datetime):
                raise ValueError("Mixed event date types")
            first = (first.replace(tzinfo=zone) if first.tzinfo is None else first).astimezone(zone)
            last = (last.replace(tzinfo=zone) if last.tzinfo is None else last).astimezone(zone)
            if last < first:
                raise ValueError("Negative event duration")
            # The owner explicitly treats one recurring lunch series as free.
            # Match its private fingerprint AND exact local lunch interval, so
            # an unrelated meeting at the same time remains unavailable. A
            # moved/extended exception to the lunch series remains busy too.
            uid = str(event.get("UID", ""))
            if (uid and hashlib.sha256(uid.encode()).hexdigest() in free_series_hashes
                    and first.weekday() < 5 and first.date() == last.date()
                    and first.time() == time(12, 30) and last.time() == time(13, 30)):
                continue
            if last > first and first < end and last > start:
                meetings.append(Meeting(first, last))
        # Separate simultaneous meetings remain separate numbered entries.
        return Availability(tuple(sorted(meetings, key=lambda m: (m.start, m.end))), tuple(all_day))
    except CalendarError:
        raise
    except Exception:
        # Parser exceptions can contain event text. Never log or forward them.
        raise CalendarError("I could not read the calendar safely; please retry or check the feed.") from None


async def read_availability(url: str, query: ScheduleQuery, timezone_name: str, *,
                            free_series_hashes: frozenset[str] | None = None) -> Availability:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "outlook.office365.com"
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise CalendarError("Configure a valid HTTPS Outlook calendar feed privately on the server.")
    try:
        if free_series_hashes is None:
            free_series_hashes = frozenset(x.strip().lower() for x in
                os.getenv("WORK_CALENDAR_FREE_SERIES_SHA256", "").split(",") if x.strip())
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in free_series_hashes):
            raise CalendarError("The calendar availability override needs a valid private fingerprint.")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            # Do not follow redirects to unrelated hosts or print credential-bearing URLs.
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise CalendarError("Outlook could not provide the calendar. Check that the feed is still published.")
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_FEED_BYTES:
                        raise CalendarError("The calendar feed is too large to read safely.")
        return await asyncio.to_thread(parse_availability, bytes(data), query, timezone_name,
                                       free_series_hashes=free_series_hashes)
    except CalendarError:
        raise
    except Exception:
        raise CalendarError("The calendar is temporarily unavailable. Please try again shortly.") from None


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}h {minutes:02}m" if hours else f"{minutes}m")


def _clock(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _open_windows(first: datetime, last: datetime,
                  intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor = first
    for start, end in sorted(intervals):
        if start > cursor:
            windows.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < last:
        windows.append((cursor, last))
    return windows


def _vertical_timeline(first: datetime, last: datetime,
                       intervals: list[tuple[datetime, datetime]]) -> str:
    lines: list[str] = []
    cursor = first
    while cursor < last:
        row_end = min(cursor + timedelta(minutes=30), last)
        lines.append(f"{_clock(cursor):>8} │ {busy_cells(cursor, row_end, intervals)}")
        cursor = row_end
    lines.append(f"{_clock(last):>8} └ End")
    # One indivisible block keeps its fence intact when the agenda paginates.
    return ("### Day timeline\n```text\n" + "\n".join(lines) + "\n```\n"
            "-# █ busy · ▓ partly busy · ░ free · 15m/block; rows up to 30m")


def render_schedule(availability: Availability, query: ScheduleQuery, *,
                    timezone_name: str = "America/Chicago", work_start: str = "08:30",
                    work_end: str = "16:00", fetched_at: datetime | None = None,
                    morning_huddle: bool = False) -> list[str]:
    zone = ZoneInfo(timezone_name)
    refreshed = (fetched_at or datetime.now(zone)).astimezone(zone)
    messages: list[str] = []
    timeline_days: list[TimelineDay] = []
    grand_meetings = grand_work = 0.0
    for offset in range(query.days):
        day = query.start + timedelta(days=offset)
        midnight = datetime.combine(day, time.min, zone)
        next_midnight = datetime.combine(day + timedelta(days=1), time.min, zone)
        first = datetime.combine(day, time.fromisoformat(work_start), zone)
        last = datetime.combine(day, time.fromisoformat(work_end), zone)
        working = day.weekday() < 5
        meetings = [m for m in availability.meetings if m.start < next_midnight and m.end > midnight]
        if working and morning_huddle:
            huddle_start = datetime.combine(day, time(8, 30), zone)
            huddle_end = datetime.combine(day, time(9, 30), zone)
            # The fixed work block also represents a matching anonymous feed event.
            meetings = [m for m in meetings if (m.start, m.end) != (huddle_start, huddle_end)]
            meetings.append(Meeting(huddle_start, huddle_end, is_huddle=True))
            meetings.sort(key=lambda m: (m.start, m.end))
        intervals = [(max(m.start, first), min(m.end, last)) for m in meetings
                     if working and m.start < last and m.end > first]
        timeline_days.append(TimelineDay(first, last, tuple(intervals), working))
        total = _union_seconds(intervals)
        work = (last - first).total_seconds() if working else 0
        grand_meetings += total
        grand_work += work
        header = [f"## 📅 {day:%A}, {day.day} {day:%B}",
                  f"-# {day:%Y} · {first:%Z} · Work {_clock(first)}–{_clock(last)}"
                  if working else f"-# {day:%Y} · {first:%Z} · No scheduled work hours (weekend)"]
        if working:
            header += ["", f"**{_duration(total)} scheduled** · **{_duration(work-total)} available**",
                       f"-# {total/work:.0%} of your {_duration(work)} workday booked"]
        rows = ([_vertical_timeline(first, last, intervals), "", "### Agenda"]
                if working and query.days == 1 else ["### Agenda"])
        number = 0
        for meeting in meetings:
            if meeting.is_huddle:
                label = "Morning Huddle"
            else:
                number += 1
                label = f"Meeting {number}"
            a, b = max(meeting.start, midnight), min(meeting.end, next_midnight)
            until = "12:00 AM (+1d)" if b == next_midnight else _clock(b)
            rows.append(f"**{_clock(a)}–{until}** · {label} · {_duration((b-a).total_seconds())}")
        if not meetings:
            rows.append("No timed meetings in the published feed.")
        if working:
            rows += ["", "### Open windows"]
            windows = _open_windows(first, last, intervals)
            rows += [f"**{_clock(a)}–{_clock(b)}** · {_duration((b-a).total_seconds())} free"
                     for a, b in windows] or ["No open windows during work hours."]
        footer = [""]
        if any(a <= day < b for a, b in availability.all_day):
            footer.append("-# All-day busy entry present; excluded from totals and open-window calculations.")
        footer += ["-# Overlaps counted once; lunch not deducted. Busy entries use anonymous meeting labels.",
                   f"-# Updated {refreshed:%b %d} at {_clock(refreshed)} {refreshed:%Z} · Outlook publishing may lag."]
        # Keep every Markdown row intact and leave room for the final notes.
        page = header + [""]
        for row in rows:
            if len("\n".join(page + [row] + footer)) > 1800:
                messages.append("\n".join(page))
                page = [f"## 📅 {day:%a}, {day.day} {day:%b} · continued", ""]
            page.append(row)
        messages.append("\n".join(page + footer))
    if query.days > 1:
        timelines = comparison_pages(timeline_days, timezone_name=timezone_name, fetched_at=refreshed)
        messages.append("## Week at a glance\n"
                        + f"**{_duration(grand_meetings)} scheduled** · "
                        + f"**{_duration(grand_work-grand_meetings)} available**\n"
                        + f"-# {_duration(grand_meetings)} / {_duration(grand_work)}"
                        + (f" · {grand_meetings/grand_work:.1%} booked" if grand_work else "")
                        + "\n\n" + timelines[0])
        messages.extend(timelines[1:])
    return messages
