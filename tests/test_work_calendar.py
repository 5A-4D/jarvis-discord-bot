import os
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from jarvis_bot.work_calendar import (
    Availability, CalendarError, Meeting, ScheduleQuery, parse_availability,
    read_availability, render_schedule, schedule_query,
)

ZONE = ZoneInfo("America/Chicago")


def feed(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
            + "".join("BEGIN:VEVENT\r\n" + e.replace("\n", "\r\n")
                      + "\r\nEND:VEVENT\r\n" for e in events)
            + "END:VCALENDAR\r\n").encode()


class CalendarTests(unittest.TestCase):
    def test_natural_requests_and_central_date(self):
        now = datetime(2026, 11, 3, 2, tzinfo=ZoneInfo("UTC"))
        for prompt in ("show my schedule today", "what's my work schedule?",
                       "can you show me my schedule please", "schduale"):
            self.assertEqual(schedule_query(prompt, now=now), ScheduleQuery(date(2026, 11, 2)))
        self.assertEqual(schedule_query("schedule tomorrow", now=now).start, date(2026, 11, 3))
        self.assertEqual(schedule_query("schedule next week", now=now), ScheduleQuery(date(2026, 11, 9), 5))
        self.assertIsNone(schedule_query("help me build a schedule parser"))
        with self.assertRaises(CalendarError):
            schedule_query("schedule 2026-99-99")

    def test_recurring_exception_and_cancelled_occurrence(self):
        data = feed(
            "UID:a\nDTSTART;TZID=America/Chicago:20261026T090000\n"
            "DTEND;TZID=America/Chicago:20261026T093000\nRRULE:FREQ=WEEKLY;COUNT=4\n"
            "EXDATE;TZID=America/Chicago:20261109T090000\nSUMMARY:secret-title\n"
            "DESCRIPTION:secret-description\nATTENDEE:mailto:secret@example.com",
            "UID:a\nRECURRENCE-ID;TZID=America/Chicago:20261102T090000\n"
            "DTSTART;TZID=America/Chicago:20261102T100000\nDTEND;TZID=America/Chicago:20261102T103000",
            "UID:a\nRECURRENCE-ID;TZID=America/Chicago:20261116T090000\n"
            "DTSTART;TZID=America/Chicago:20261116T090000\nDTEND;TZID=America/Chicago:20261116T093000\nSTATUS:CANCELLED",
        )
        query = ScheduleQuery(date(2026, 11, 2))
        parsed = parse_availability(data, query, "America/Chicago")
        self.assertEqual(len(parsed.meetings), 1)
        self.assertEqual(parsed.meetings[0].start.hour, 10)
        self.assertEqual(parsed.meetings[0].start.utcoffset(), timedelta(hours=-6))
        rendered = "\n".join(render_schedule(parsed, query))
        for private in ("secret-title", "secret-description", "secret@example.com"):
            self.assertNotIn(private, repr(parsed) + rendered)
        for day in (9, 16):
            self.assertFalse(parse_availability(data, ScheduleQuery(date(2026, 11, day)), "America/Chicago").meetings)

    def test_free_cancelled_all_day_and_duration(self):
        data = feed(
            "UID:a\nDTSTART:20261102T150000Z\nDURATION:PT30M",
            "UID:b\nDTSTART:20261102T160000Z\nDTEND:20261102T170000Z\nTRANSP:TRANSPARENT",
            "UID:c\nDTSTART:20261102T170000Z\nDTEND:20261102T180000Z\nSTATUS:CANCELLED",
            "UID:d\nDTSTART:20261102T180000Z\nDTEND:20261102T190000Z\nX-MICROSOFT-CDO-BUSYSTATUS:FREE",
            "UID:e\nDTSTART;VALUE=DATE:20261102\nDTEND;VALUE=DATE:20261103",
        )
        query = ScheduleQuery(date(2026, 11, 2))
        result = parse_availability(data, query, "America/Chicago")
        self.assertEqual(len(result.meetings), 1)
        self.assertEqual(result.meetings[0].start.hour, 9)
        text = "\n".join(render_schedule(result, query))
        self.assertIn("**30m scheduled**", text)
        self.assertIn("All-day busy entry present", text)

    def test_overlap_union_work_clipping_and_exact_open_windows(self):
        day = date(2026, 11, 2)
        def meeting(a, b):
            return Meeting(datetime.fromisoformat(f"{day}T{a}").replace(tzinfo=ZONE),
                           datetime.fromisoformat(f"{day}T{b}").replace(tzinfo=ZONE))
        availability = Availability((meeting("08:00", "09:15"), meeting("09:00", "10:00"),
                                     meeting("15:45", "17:00")))
        text = "\n".join(render_schedule(availability, ScheduleQuery(day)))
        self.assertIn("**1h 45m scheduled**", text)
        self.assertIn("**5h 45m available**", text)
        self.assertIn("Meeting 3", text)
        self.assertIn("**10:00 AM–3:45 PM** · 5h 45m free", text)
        self.assertEqual(text.split("### Open windows", 1)[1].count(" free"), 1)

    def test_week_total_and_empty_or_weekend(self):
        empty = Availability(())
        messages = render_schedule(empty, ScheduleQuery(date(2026, 11, 2), 5))
        self.assertEqual(len(messages), 6)
        self.assertIn("0m / 37h 30m · 0.0%", messages[-1])
        self.assertIn("**8:30 AM–4:00 PM** · 7h 30m free", messages[0])
        weekend = render_schedule(empty, ScheduleQuery(date(2026, 11, 7)))[0]
        self.assertIn("No scheduled work hours", weekend)
        self.assertNotIn("nan", weekend)

    def test_morning_huddle_is_always_busy_on_workdays_and_counts_once(self):
        query = ScheduleQuery(date(2026, 11, 2))
        first = datetime(2026, 11, 2, 8, 30, tzinfo=ZONE)
        availability = Availability((Meeting(first, first+timedelta(hours=1)),
                                     Meeting(first+timedelta(minutes=30), first+timedelta(hours=2))))
        text = "\n".join(render_schedule(availability, query, morning_huddle=True))
        self.assertEqual(text.count("Morning Huddle"), 1)
        self.assertIn("8:30 AM–9:30 AM", text)
        self.assertIn("**2h 00m scheduled**", text)
        self.assertIn("Meeting 1", text)
        self.assertNotIn("Meeting 2", text)
        week = render_schedule(Availability(()), ScheduleQuery(query.start, 5), morning_huddle=True)
        self.assertIn("5h 00m / 37h 30m", week[-1])
        weekend = render_schedule(Availability(()), ScheduleQuery(date(2026, 11, 7)), morning_huddle=True)
        self.assertNotIn("Morning Huddle", weekend[0])

    def test_long_schedule_keeps_complete_rows_and_discord_limits(self):
        first = datetime(2026, 11, 2, 9, tzinfo=ZONE)
        availability = Availability(tuple(Meeting(first, first+timedelta(minutes=15)) for _ in range(120)))
        messages = render_schedule(availability, ScheduleQuery(first.date()))
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), 2000)
            self.assertEqual(message.count("```") % 2, 0)
            self.assertEqual(message.count("**") % 2, 0)
        self.assertIn("**15m scheduled**", messages[0])
        self.assertEqual("\n".join(messages).count("· Meeting "), 120)
        self.assertIn("### Open windows", "\n".join(messages))

    def test_fully_booked_day_has_no_free_windows(self):
        first = datetime(2026, 11, 2, 8, 30, tzinfo=ZONE)
        last = first.replace(hour=16, minute=0)
        text = "\n".join(render_schedule(Availability((Meeting(first, last),)), ScheduleQuery(first.date())))
        self.assertIn("**0m available**", text)
        self.assertIn("No open windows during work hours.", text)
        self.assertNotIn(" free", text.split("### Open windows", 1)[1])

    def test_vertical_timeline_places_quarter_hour_blocks_and_marks_partial_coverage(self):
        first = datetime(2026, 11, 2, 10, 45, tzinfo=ZONE)
        availability = Availability((Meeting(first, first+timedelta(hours=1)),
                                     Meeting(first.replace(hour=14, minute=5), first.replace(hour=14, minute=10))))
        text = "\n".join(render_schedule(availability, ScheduleQuery(first.date())))
        self.assertIn("10:30 AM │ ░█", text)
        self.assertIn("11:00 AM │ ██", text)
        self.assertIn("11:30 AM │ █░", text)
        self.assertIn(" 2:00 PM │ ▓░", text)
        self.assertIn(" 4:00 PM └ End", text)
        self.assertIn("15m/block", text)

    def test_vertical_timeline_handles_last_short_row_with_complete_fence(self):
        query = ScheduleQuery(date(2026, 11, 2))
        text = render_schedule(Availability(()), query, work_end="08:37")[0]
        self.assertIn(" 8:30 AM │ ░", text)
        self.assertIn(" 8:37 AM └ End", text)
        self.assertEqual(text.count("```"), 2)

    def test_malformed_feed_does_not_echo_contents(self):
        for data in (b"private error or login HTML", feed("UID:a\nDTSTART:secret-content")):
            with self.assertRaises(CalendarError) as context:
                parse_availability(data, ScheduleQuery(date(2026, 11, 2)), "America/Chicago")
            self.assertNotIn("secret-content", str(context.exception))


class CalendarNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_exception_does_not_expose_feed_url(self):
        with patch("jarvis_bot.work_calendar.aiohttp.ClientSession", side_effect=RuntimeError("SECRET URL")):
            with self.assertRaises(CalendarError) as context:
                await read_availability("https://outlook.office365.com/private.ics", ScheduleQuery(date(2026, 11, 2)), "America/Chicago")
        self.assertNotIn("SECRET", str(context.exception))

    async def test_non_outlook_url_is_rejected(self):
        with self.assertRaises(CalendarError):
            await read_availability("http://localhost/private", ScheduleQuery(date(2026, 11, 2)), "America/Chicago")


class CalendarAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def message(self, user_id, *, guild=False):
        return SimpleNamespace(author=SimpleNamespace(id=user_id, bot=False), webhook_id=None,
                               guild=SimpleNamespace(id=500) if guild else None,
                               channel=SimpleNamespace(id=1, typing=lambda: Typing()),
                               content="Jarvis show my schedule", attachments=[], reply=AsyncMock())

    def client(self, *, public_enabled=False, public_channel=True):
        return SimpleNamespace(_channel_allowed=lambda _: True, _store_discord_message=AsyncMock(),
                               _public_channel_allowed=lambda _: public_channel,
                               _is_reply_to_self=AsyncMock(return_value=False), user=SimpleNamespace(id=100),
                               settings=SimpleNamespace(trigger="Jarvis", authorized_user_ids={300, 400},
                                   work_calendar_owner_id=300, work_calendar_ics_url="SECRET",
                                   work_calendar_public_enabled=public_enabled,
                                   work_calendar_timezone="America/Chicago", work_day_start="08:30", work_day_end="16:00"))

    async def test_public_and_other_owner_cannot_fetch(self):
        from jarvis_bot.bot import JarvisDiscordClient
        for user_id in (200, 400):
            message = self.message(user_id)
            with patch("jarvis_bot.bot.read_availability", new_callable=AsyncMock) as reader:
                await JarvisDiscordClient.on_message(self.client(), message)
            reader.assert_not_awaited()
            self.assertIn("private", message.reply.await_args.args[0])

    async def test_owner_gets_direct_report_without_ai(self):
        from jarvis_bot.bot import JarvisDiscordClient
        message = self.message(300)
        # This assertion exercises a workday report, independently of the day
        # the baseline suite runs (including weekends).
        message.content = "Jarvis show my schedule 2026-11-02"
        with patch("jarvis_bot.bot.read_availability", new_callable=AsyncMock, return_value=Availability(())) as reader:
            await JarvisDiscordClient.on_message(self.client(), message)
        reader.assert_awaited_once()
        self.assertIn("scheduled**", message.reply.await_args.args[0])
        self.assertNotIn("SECRET", message.reply.await_args.args[0])

    async def test_public_opt_in_serves_dm_and_server_without_ai(self):
        from jarvis_bot.bot import JarvisDiscordClient
        for user_id in (200, 400):
            for guild in (False, True):
                with self.subTest(user=user_id, guild=guild):
                    message = self.message(user_id, guild=guild)
                    with patch("jarvis_bot.bot.schedule_query", return_value=ScheduleQuery(date(2026, 11, 2))), \
                         patch("jarvis_bot.bot.read_availability", new_callable=AsyncMock,
                               return_value=Availability(())) as reader:
                        await JarvisDiscordClient.on_message(self.client(public_enabled=True), message)
                    reader.assert_awaited_once()
                    rendered = "\n".join(call.args[0] for call in message.reply.await_args_list)
                    self.assertIn("Shared work schedule — <@300>", rendered)
                    self.assertIn("### Day timeline", rendered)
                    self.assertIn("█ busy", rendered)
                    self.assertNotIn("SECRET", rendered)
                    for call in message.reply.await_args_list:
                        self.assertLessEqual(len(call.args[0]), 2000)
                        self.assertFalse(call.kwargs["mention_author"])

    async def test_public_calendar_still_respects_server_channel_gate(self):
        from jarvis_bot.bot import JarvisDiscordClient
        message = self.message(200, guild=True)
        with patch("jarvis_bot.bot.read_availability", new_callable=AsyncMock) as reader:
            await JarvisDiscordClient.on_message(
                self.client(public_enabled=True, public_channel=False), message)
        reader.assert_not_awaited()
        self.assertIn("not enabled in this channel", message.reply.await_args.args[0])

    async def test_public_flag_does_not_enable_bots_or_webhooks(self):
        from jarvis_bot.bot import JarvisDiscordClient
        for kind in ("bot", "webhook"):
            with self.subTest(kind=kind):
                message = self.message(200, guild=True)
                message.author.bot = kind == "bot"
                message.webhook_id = 123 if kind == "webhook" else None
                with patch("jarvis_bot.bot.read_availability", new_callable=AsyncMock) as reader:
                    await JarvisDiscordClient.on_message(self.client(public_enabled=True), message)
                reader.assert_not_awaited()
                message.reply.assert_not_awaited()

    async def test_public_calendar_requires_valid_owner_and_returns_safe_errors(self):
        from jarvis_bot.bot import JarvisDiscordClient
        client = self.client(public_enabled=True)
        client.settings.work_calendar_owner_id = 999
        message = self.message(200)
        with patch("jarvis_bot.bot.read_availability", new_callable=AsyncMock) as reader:
            await JarvisDiscordClient.on_message(client, message)
        reader.assert_not_awaited()

        message = self.message(200)
        with patch("jarvis_bot.bot.read_availability", new_callable=AsyncMock,
                   side_effect=CalendarError("The calendar is temporarily unavailable.")):
            await JarvisDiscordClient.on_message(self.client(public_enabled=True), message)
        self.assertEqual(message.reply.await_args.args[0], "The calendar is temporarily unavailable.")
        self.assertNotIn("SECRET", message.reply.await_args.args[0])


class CalendarConfigTests(unittest.TestCase):
    def test_public_access_defaults_to_private_and_can_be_enabled(self):
        from jarvis_bot.config import Settings
        base = {"DISCORD_BOT_TOKEN": "test", "AUTHORIZED_USER_IDS": "300",
                "WORK_CALENDAR_OWNER_ID": "300"}
        for flag, expected in ((None, False), ("false", False), ("true", True)):
            with self.subTest(flag=flag):
                environment = dict(base)
                if flag is not None:
                    environment["WORK_CALENDAR_PUBLIC_ENABLED"] = flag
                with patch.dict(os.environ, environment, clear=True), \
                     patch("jarvis_bot.config.load_dotenv"):
                    self.assertEqual(Settings.from_env().work_calendar_public_enabled, expected)

    def test_public_access_requires_an_authorized_calendar_owner(self):
        from jarvis_bot.config import Settings
        for calendar_owner in ("", "400"):
            with self.subTest(owner=calendar_owner):
                environment = {"DISCORD_BOT_TOKEN": "test", "AUTHORIZED_USER_IDS": "300",
                               "WORK_CALENDAR_OWNER_ID": calendar_owner,
                               "WORK_CALENDAR_PUBLIC_ENABLED": "true"}
                with patch.dict(os.environ, environment, clear=True), \
                     patch("jarvis_bot.config.load_dotenv"):
                    with self.assertRaisesRegex(ValueError, "authorized WORK_CALENDAR_OWNER_ID"):
                        Settings.from_env()


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False
