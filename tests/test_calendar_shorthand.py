"""Synthetic regressions for deterministic schedule shorthand requests."""

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis_bot.work_calendar import (
    Availability, CalendarError, Meeting, ScheduleQuery, render_schedule,
    schedule_query,
)


ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 3, 2, tzinfo=ZoneInfo("UTC"))
MONDAY = date(2026, 11, 2)


class CalendarShorthandTests(unittest.TestCase):
    def test_my_sch_and_natural_wrappers_resolve_to_local_today(self):
        for prompt in (
            "my sch", "sch", "MY SCH?!", "show my sch today",
            "what's my work sch?", "what is my sch for today",
            "can you show me my sch please",
            "hey could you please give me my work sch for today please",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(schedule_query(prompt, now=NOW), ScheduleQuery(MONDAY))

    def test_shorthand_preserves_all_existing_periods(self):
        cases = (
            ("today", MONDAY, 1),
            ("tomorrow", date(2026, 11, 3), 1),
            ("yesterday", date(2026, 11, 1), 1),
            ("week", MONDAY, 5),
            ("this week", MONDAY, 5),
            ("next week", date(2026, 11, 9), 5),
            ("2028-02-29", date(2028, 2, 29), 1),
        )
        for period, first, days in cases:
            with self.subTest(period=period):
                self.assertEqual(schedule_query(f"my sch for {period}", now=NOW),
                                 ScheduleQuery(first, days))

    def test_shorthand_respects_configured_timezone_at_midnight(self):
        for zone, expected in (("America/Chicago", MONDAY),
                               ("UTC", date(2026, 11, 3))):
            with self.subTest(zone=zone):
                self.assertEqual(schedule_query("my sch", now=NOW, timezone_name=zone),
                                 ScheduleQuery(expected))

    def test_week_selection_stays_on_monday_including_weekends(self):
        for offset in range(7):
            now = datetime(2026, 11, 2, 12, tzinfo=ZONE) + timedelta(days=offset)
            with self.subTest(day=now.date()):
                self.assertEqual(schedule_query("my sch this week", now=now),
                                 ScheduleQuery(MONDAY, 5))
                self.assertEqual(schedule_query("my sch next week", now=now),
                                 ScheduleQuery(date(2026, 11, 9), 5))

    def test_impossible_dates_keep_the_existing_safe_error(self):
        for value in ("2026-02-29", "2026-13-01", "2026-11-31"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(CalendarError, "Use a valid date"):
                    schedule_query(f"my sch {value}", now=NOW)

    def test_unrelated_words_and_extra_instructions_are_not_calendar_requests(self):
        for prompt in (
            "my school", "my scholarship", "show my schema", "my scheme",
            "help me build a sch parser", "my sch next month",
            "my sch tomorrow and send it to someone", "change my sch",
            "my sch delete meetings", "my sch 2026-11-02 extra",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(schedule_query(prompt, now=NOW))


class ShorthandReportTests(unittest.TestCase):
    def reports(self, period):
        def at(hour, minute=0):
            return datetime(2026, 11, 2, hour, minute, tzinfo=ZONE)

        availability = Availability((
            Meeting(at(8, 30), at(9, 30)),
            Meeting(at(9), at(10)),
            Meeting(at(14, 5), at(14, 10)),
        ))
        options = dict(fetched_at=NOW, morning_huddle=True)
        shorthand = schedule_query(f"my sch {period}", now=NOW)
        full = schedule_query(f"my schedule {period}", now=NOW)
        self.assertIsNotNone(shorthand)
        messages = render_schedule(availability, shorthand, **options)
        self.assertEqual(messages,
                         render_schedule(availability, full, **options))
        return messages

    def test_single_day_shorthand_keeps_exact_windows_overlap_and_partial_cells(self):
        text = "\n".join(self.reports("today"))
        for expected in (
            "### Day timeline", "2:00 PM │ ▓░", "### Agenda",
            "**1h 35m scheduled** · **5h 55m available**",
            "**10:00 AM–2:05 PM** · 4h 05m free",
            "**2:10 PM–4:00 PM** · 1h 50m free",
            "Updated Nov 02 at 8:00 PM CST",
        ):
            self.assertIn(expected, text)
        self.assertEqual(text.count("Morning Huddle"), 1)
        self.assertEqual(text.count("· Meeting "), 2)

    def test_week_shorthand_keeps_side_by_side_grid_totals_and_discord_limits(self):
        messages = self.reports("this week")
        text = "\n".join(messages)
        self.assertIn("Side-by-side timeline", text)
        self.assertIn("America/Chicago", text)
        self.assertIn("5h 35m / 37h 30m", text)
        self.assertEqual(text.count("### Agenda"), 5)
        self.assertEqual(text.count("### Open windows"), 5)
        self.assertEqual(text.count("Morning Huddle"), 5)
        for message in messages:
            self.assertLessEqual(len(message), 2000)
            self.assertEqual(message.count("```") % 2, 0)


if __name__ == "__main__":
    unittest.main()
