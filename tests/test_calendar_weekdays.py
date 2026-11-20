"""Synthetic regressions for weekday selection through the existing calendar path."""

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis_bot.work_calendar import (
    Availability, Meeting, ScheduleQuery, render_schedule, schedule_query,
)


ZONE = ZoneInfo("America/Chicago")
MONDAY = date(2026, 11, 2)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class WeekdayQueryTests(unittest.TestCase):
    def test_bare_weekday_selects_next_occurrence_including_today(self):
        for today_offset in range(7):
            now = datetime(2026, 11, 2, 12, tzinfo=ZONE) + timedelta(days=today_offset)
            for target_offset, weekday in enumerate(WEEKDAYS):
                with self.subTest(today=now.date(), weekday=weekday):
                    expected = now.date() + timedelta(days=(target_offset - today_offset) % 7)
                    self.assertEqual(schedule_query(f"my sch {weekday}", now=now),
                                     ScheduleQuery(expected))

    def test_this_and_next_weekday_use_the_same_week_boundaries_as_week_queries(self):
        for today_offset in range(7):
            now = datetime(2026, 11, 2, 12, tzinfo=ZONE) + timedelta(days=today_offset)
            for qualifier in ("this", "next"):
                week = schedule_query(f"my sch {qualifier} week", now=now)
                for target_offset, weekday in enumerate(WEEKDAYS):
                    with self.subTest(today=now.date(), qualifier=qualifier, weekday=weekday):
                        self.assertEqual(
                            schedule_query(f"my sch {qualifier} {weekday}", now=now),
                            ScheduleQuery(week.start + timedelta(days=target_offset)),
                        )

    def test_weekdays_accept_existing_wrappers_and_on_or_for(self):
        now = datetime(2026, 11, 4, 12, tzinfo=ZONE)
        for prompt in (
            "my sch Friday", "MY SCH ON FRIDAY?!", "schedule for Friday",
            "what's my work calendar on Friday?", "show me my meetings this Friday",
            "hey could you please give me my sch on this Friday please",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(schedule_query(prompt, now=now), ScheduleQuery(date(2026, 11, 6)))

    def test_weekday_resolution_uses_configured_local_date_at_midnight(self):
        now = datetime(2026, 11, 3, 2, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(schedule_query("my sch Monday", now=now), ScheduleQuery(MONDAY))
        self.assertEqual(schedule_query("my sch Monday", now=now, timezone_name="UTC"),
                         ScheduleQuery(date(2026, 11, 9)))

    def test_qualifier_whitespace_does_not_change_week_selection(self):
        now = datetime(2026, 11, 4, 12, tzinfo=ZONE)
        for separator in (" ", "  ", "\t", "\n"):
            for qualifier, expected in (("this", MONDAY), ("next", date(2026, 11, 9))):
                with self.subTest(separator=repr(separator), qualifier=qualifier):
                    self.assertEqual(schedule_query(f"my sch {qualifier}{separator}Monday", now=now),
                                     ScheduleQuery(expected))

    def test_weekday_selection_crosses_year_and_dst_boundaries_by_local_date(self):
        for now, prompt, expected in (
            (datetime(2026, 12, 31, 12, tzinfo=ZONE), "my sch Friday", date(2027, 1, 1)),
            (datetime(2026, 12, 31, 12, tzinfo=ZONE), "my sch next Monday", date(2027, 1, 4)),
            (datetime(2026, 10, 31, 12, tzinfo=ZONE), "my sch Monday", MONDAY),
            (datetime(2026, 3, 7, 12, tzinfo=ZONE), "my sch Monday", date(2026, 3, 9)),
        ):
            with self.subTest(prompt=prompt, today=now.date()):
                self.assertEqual(schedule_query(prompt, now=now), ScheduleQuery(expected))

    def test_unrelated_or_action_requests_still_do_not_match(self):
        for prompt in (
            "my sch Friday and send it to someone", "change my sch Friday",
            "my sch Fridays", "my sch Fridayish", "my sch next next Monday",
            "my sch last Monday", "my sch on", "my sch on Friday extra",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(schedule_query(prompt))


class WeekdayReportTests(unittest.TestCase):
    def test_selected_weekday_keeps_exact_anonymous_report_and_huddle_accounting(self):
        now = datetime(2026, 11, 3, 12, tzinfo=ZONE)
        query = schedule_query("my sch on Friday", now=now)
        self.assertEqual(query, ScheduleQuery(date(2026, 11, 6)))
        def at(hour, minute=0):
            return datetime(2026, 11, 6, hour, minute, tzinfo=ZONE)

        availability = Availability((Meeting(at(9), at(10)), Meeting(at(14, 5), at(14, 10))))
        options = dict(fetched_at=now, morning_huddle=True)
        messages = render_schedule(availability, query, **options)
        self.assertEqual(messages, render_schedule(
            availability, schedule_query("my sch 2026-11-06", now=now), **options))
        text = "\n".join(messages)
        for expected in (
            "Friday, 6 November", "CST", "### Day timeline", "2:00 PM │ ▓░",
            "**1h 35m scheduled** · **5h 55m available**",
            "**10:00 AM–2:05 PM** · 4h 05m free",
            "**2:10 PM–4:00 PM** · 1h 50m free",
            "Morning Huddle", "· Meeting 1", "· Meeting 2",
            "Updated Nov 03 at 12:00 PM CST",
        ):
            self.assertIn(expected, text)
        for message in messages:
            self.assertLessEqual(len(message), 2000)
            self.assertEqual(message.count("```") % 2, 0)

    def test_weekend_selection_preserves_no_work_hours_and_omits_huddle(self):
        query = schedule_query("my sch Saturday", now=datetime(2026, 11, 4, 12, tzinfo=ZONE))
        self.assertEqual(query, ScheduleQuery(date(2026, 11, 7)))
        text = "\n".join(render_schedule(Availability(()), query, morning_huddle=True))
        self.assertIn("Saturday, 7 November", text)
        self.assertIn("No scheduled work hours (weekend)", text)
        self.assertNotIn("Morning Huddle", text)
        self.assertNotIn("### Open windows", text)


if __name__ == "__main__":
    unittest.main()
