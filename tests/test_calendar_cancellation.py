"""Synthetic regressions for cancellation exceptions without a start time."""

import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis_bot.work_calendar import (
    CalendarError, Meeting, ScheduleQuery, parse_availability, render_schedule,
)


ZONE = ZoneInfo("America/Chicago")
MONDAY = date(2026, 11, 2)
FETCHED = datetime(2026, 11, 2, 7, 12, tzinfo=ZONE)
MASTER = (
    "UID:synthetic-series\nDTSTART;TZID=America/Chicago:20261102T090000\n"
    "DTEND;TZID=America/Chicago:20261102T100000\nRRULE:FREQ=DAILY;COUNT=5"
)
CANCELLATION = (
    "UID:synthetic-series\nRECURRENCE-ID;TZID=America/Chicago:20261103T090000\n"
    "STATUS:CANCELLED"
)


def calendar(*events):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Synthetic cancellation tests//EN\r\n"
        + "".join("BEGIN:VEVENT\r\n" + event.replace("\n", "\r\n")
                  + "\r\nEND:VEVENT\r\n" for event in events)
        + "END:VCALENDAR\r\n"
    ).encode()


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)


class CalendarCancellationTests(unittest.TestCase):
    def test_startless_cancellation_removes_only_the_matching_occurrence(self):
        expected = tuple(Meeting(at(day, 9), at(day, 10)) for day in (2, 4, 5, 6))
        for events in ((MASTER, CANCELLATION), (CANCELLATION, MASTER)):
            with self.subTest(cancellation_first=events[0] == CANCELLATION):
                result = parse_availability(calendar(*events), ScheduleQuery(MONDAY, 5),
                                            "America/Chicago")
                self.assertEqual(result.meetings, expected)
                self.assertEqual(result.all_day, ())

    def test_unrelated_meeting_keeps_exact_windows_totals_and_timeline(self):
        separate = (
            "UID:synthetic-other\nDTSTART;TZID=America/Chicago:20261103T091500\n"
            "DTEND;TZID=America/Chicago:20261103T093000"
        )
        data = calendar(MASTER, CANCELLATION, separate)
        query = ScheduleQuery(date(2026, 11, 3))
        result = parse_availability(data, query, "America/Chicago")
        self.assertEqual(result.meetings, (Meeting(at(3, 9, 15), at(3, 9, 30)),))
        text = "\n".join(render_schedule(result, query, fetched_at=FETCHED))
        for expected in (
            "**15m scheduled** · **7h 15m available**",
            "**8:30 AM–9:15 AM** · 45m free",
            "**9:30 AM–4:00 PM** · 6h 30m free",
            "9:00 AM │ ░█", "9:30 AM │ ░░",
            "**9:15 AM–9:30 AM** · Meeting 1 · 15m",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("Meeting 2", text)

        week = ScheduleQuery(MONDAY, 5)
        availability = parse_availability(data, week, "America/Chicago")
        self.assertIn("4h 15m / 37h 30m",
                      render_schedule(availability, week, fetched_at=FETCHED)[-1])
        pages = render_schedule(availability, week, fetched_at=FETCHED,
                                morning_huddle=True)
        # Five huddles plus four remaining meetings overlap by half an hour.
        # The separate meeting on the cancelled day is inside that day's huddle.
        self.assertIn("7h 00m / 37h 30m", pages[-1])
        row = next(line for line in pages[-1].splitlines() if "9:30 AM │" in line)
        self.assertEqual([cell.strip() for cell in row.split("│")][1:],
                         ["██", "░░", "██", "██", "██"])
        self.assertEqual("\n".join(pages).count("Morning Huddle"), 5)
        self.assertIn("America/Chicago", pages[-1])
        self.assertIn("Updated Nov 02 at 7:12 AM CST", pages[-1])
        for page in pages:
            self.assertLessEqual(len(page), 2000)
            self.assertEqual(page.count("```") % 2, 0)

    def test_utc_recurrence_id_matches_local_series_across_dst(self):
        master = (
            "UID:synthetic-dst\nDTSTART;TZID=America/Chicago:20261030T090000\n"
            "DTEND;TZID=America/Chicago:20261030T100000\nRRULE:FREQ=DAILY;COUNT=5"
        )
        cancellation = (
            "UID:synthetic-dst\nRECURRENCE-ID:20261102T150000Z\nSTATUS:CANCELLED"
        )
        result = parse_availability(calendar(master, cancellation),
                                    ScheduleQuery(date(2026, 10, 30), 5), "America/Chicago")
        self.assertEqual([meeting.start.date() for meeting in result.meetings],
                         [date(2026, 10, 30), date(2026, 10, 31),
                          date(2026, 11, 1), date(2026, 11, 3)])
        self.assertEqual(result.meetings[0].start.utcoffset(), timedelta(hours=-5))
        self.assertEqual(result.meetings[-1].start.utcoffset(), timedelta(hours=-6))
        self.assertTrue(all(meeting.start.hour == 9 for meeting in result.meetings))

    def test_all_day_cancellation_preserves_other_days_and_flags(self):
        master = (
            "UID:synthetic-all-day\nDTSTART;VALUE=DATE:20261102\n"
            "DTEND;VALUE=DATE:20261103\nRRULE:FREQ=DAILY;COUNT=3"
        )
        cancellation = (
            "UID:synthetic-all-day\nRECURRENCE-ID;VALUE=DATE:20261103\nSTATUS:CANCELLED"
        )
        query = ScheduleQuery(MONDAY, 3)
        result = parse_availability(calendar(master, cancellation), query, "America/Chicago")
        self.assertEqual(result.meetings, ())
        self.assertEqual(result.all_day,
                         ((MONDAY, date(2026, 11, 3)),
                          (date(2026, 11, 4), date(2026, 11, 5))))
        pages = render_schedule(result, query, fetched_at=FETCHED)
        self.assertIn("All-day busy entry present", pages[0])
        self.assertNotIn("All-day busy entry present", pages[1])
        self.assertIn("All-day busy entry present", pages[2])
        self.assertIn("0m / 22h 30m", pages[-1])

    def test_cancellation_metadata_never_reaches_availability_or_report(self):
        private_fields = (
            "SUMMARY:synthetic-private-title\nDESCRIPTION:synthetic-private-description\n"
            "ATTENDEE:mailto:synthetic-private@example.invalid"
        )
        data = calendar(MASTER + "\n" + private_fields,
                        CANCELLATION + "\n" + private_fields)
        query = ScheduleQuery(MONDAY, 5)
        result = parse_availability(data, query, "America/Chicago")
        text = repr(result) + "\n".join(render_schedule(result, query, fetched_at=FETCHED))
        for sentinel in ("synthetic-private", "synthetic-series", "BEGIN:VEVENT"):
            self.assertNotIn(sentinel, text)

    def test_missing_or_invalid_timing_on_active_events_still_fails_safely(self):
        for event in (
            CANCELLATION.replace("STATUS:CANCELLED", "STATUS:CONFIRMED"),
            CANCELLATION.replace("STATUS:CANCELLED", ""),
            CANCELLATION.replace("20261103T090000", "synthetic-private-invalid"),
        ):
            with self.subTest(event_index=len(event)):
                with self.assertRaises(CalendarError) as context:
                    parse_availability(calendar(MASTER, event), ScheduleQuery(MONDAY, 5),
                                       "America/Chicago")
                self.assertNotIn("synthetic", str(context.exception))


if __name__ == "__main__":
    unittest.main()
