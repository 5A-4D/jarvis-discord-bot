"""Synthetic regression coverage for the multi-day availability comparison."""
import importlib.util
import re
import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from jarvis_bot.calendar_timeline import TimelineDay, busy_cells, comparison_pages


ZONE = ZoneInfo("America/Chicago")
MONDAY = date(2026, 11, 2)
FETCHED = datetime(2026, 11, 2, 7, 12, tzinfo=ZONE)
HAS_CALENDAR_DEPS = all(importlib.util.find_spec(name) is not None
                        for name in ("aiohttp", "icalendar", "recurring_ical_events"))
if HAS_CALENDAR_DEPS:
    from jarvis_bot.work_calendar import Availability, Meeting, ScheduleQuery, render_schedule


def at(day, clock):
    return datetime.combine(day, time.fromisoformat(clock), ZONE)


def timeline_day(day, intervals=(), first="08:30", last="16:00"):
    return TimelineDay(at(day, first), at(day, last),
                       tuple((at(day, a), at(day, b)) for a, b in intervals),
                       day.weekday() < 5)


def grid_rows(pages):
    return [line for page in pages for line in page.splitlines()
            if re.match(r"^\s*\d{1,2}:\d{2} [AP]M │", line)]


class ComparisonTimelineTests(unittest.TestCase):
    def test_five_day_grid_aligns_exact_quarter_hour_cells(self):
        intervals = [(("08:30", "09:00"),), (("08:35", "08:40"),),
                     (("08:45", "09:00"),), (("08:30", "08:45"),),
                     (("08:50", "08:55"),)]
        days = [timeline_day(MONDAY + timedelta(days=i), busy)
                for i, busy in enumerate(intervals)]
        pages = comparison_pages(days, timezone_name="America/Chicago", fetched_at=FETCHED)
        self.assertEqual(len(pages), 1)
        first_row = grid_rows(pages)[0]
        self.assertEqual([cell.strip() for cell in first_row.split("│")],
                         ["8:30 AM", "██", "▓░", "░█", "█░", "░▓"])
        self.assertEqual(len(grid_rows(pages)), 15)
        self.assertIn("4:00 PM └ End", pages[0])
        self.assertIn("Mon 02", pages[0])
        self.assertIn("Fri 06", pages[0])
        self.assertIn("2026-11-02–2026-11-06 · America/Chicago", pages[0])
        self.assertIn("Updated Nov 02 at 7:12 AM CST", pages[0])
        self.assertIn("15m/block", pages[0])

    def test_duplicate_partial_overlaps_do_not_falsely_fill_a_cell(self):
        first, last = at(MONDAY, "08:30"), at(MONDAY, "08:45")
        intervals = [(first, first + timedelta(minutes=10))] * 3
        self.assertEqual(busy_cells(first, last, intervals), "▓")
        # Abutting fragments together fill a cell, even with duplicates.
        intervals += [(first + timedelta(minutes=10), last)]
        self.assertEqual(busy_cells(first, last, intervals), "█")

    def test_clipping_and_boundary_only_contacts_do_not_add_busy_cells(self):
        first, last = at(MONDAY, "08:30"), at(MONDAY, "09:00")
        intervals = [(at(MONDAY, "08:00"), at(MONDAY, "08:45")),
                     (last, at(MONDAY, "10:00"))]
        self.assertEqual(busy_cells(first, last, intervals), "█░")
        self.assertEqual(busy_cells(first, last, intervals[::-1]), "█░")

    def test_short_final_row_uses_actual_coverage(self):
        days = [timeline_day(MONDAY, (("08:30", "08:37"),), last="08:37"),
                timeline_day(MONDAY + timedelta(days=1), (("08:32", "08:33"),), last="08:37")]
        pages = comparison_pages(days, timezone_name="America/Chicago", fetched_at=FETCHED)
        self.assertEqual([cell.strip() for cell in grid_rows(pages)[0].split("│")],
                         ["8:30 AM", "█", "▓"])
        self.assertIn("8:37 AM └ End", pages[0])

    def test_weekends_are_distinct_from_free_work_time_and_show_dst_per_day(self):
        friday = date(2026, 10, 30)
        days = [timeline_day(friday + timedelta(days=i), (("08:30", "09:00"),))
                for i in range(5)]
        pages = comparison_pages(days, timezone_name="America/Chicago", fetched_at=FETCHED)
        self.assertEqual([cell.strip() for cell in grid_rows(pages)[0].split("│")],
                         ["8:30 AM", "██", "——", "——", "██", "██"])
        self.assertIn("CDT", pages[0])
        self.assertIn("CST", pages[0])
        self.assertIn("— no work", pages[0])

    def test_long_hours_and_extra_days_paginate_without_losing_rows(self):
        days = [timeline_day(MONDAY + timedelta(days=i), first="00:00", last="23:59")
                for i in range(7)]
        pages = comparison_pages(days, timezone_name="America/Chicago", fetched_at=FETCHED)
        self.assertGreater(len(pages), 2)
        self.assertEqual(len(grid_rows(pages)), 48 * 2)
        self.assertEqual(sum(page.count("11:59 PM └ End") for page in pages), 2)
        for page in pages:
            self.assertLessEqual(len(page), 1500)
            self.assertEqual(page.count("```"), 2)
            self.assertIn("Time │", page)
            self.assertIn("America/Chicago", page)
            self.assertIn("Updated Nov 02 at 7:12 AM CST", page)
        labels = [row.split("│")[0].strip() for row in grid_rows(pages)]
        self.assertEqual(labels[:48], labels[48:])
        self.assertEqual(len(set(labels)), 48)

    def test_all_weekend_grid_never_implies_free_work_hours(self):
        days = [timeline_day(date(2026, 11, day)) for day in (7, 8)]
        pages = comparison_pages(days, timezone_name="America/Chicago", fetched_at=FETCHED)
        for row in grid_rows(pages):
            self.assertEqual([cell.strip() for cell in row.split("│")][1:], ["——", "——"])


@unittest.skipUnless(HAS_CALENDAR_DEPS, "Calendar integration dependencies unavailable")
class CalendarComparisonIntegrationTests(unittest.TestCase):
    def render(self, availability, days=5, **kwargs):
        return render_schedule(availability, ScheduleQuery(MONDAY, days),
                               fetched_at=FETCHED, **kwargs)

    def test_default_week_keeps_daily_details_and_one_comparison_page(self):
        messages = self.render(Availability(()))
        self.assertEqual(len(messages), 6)
        self.assertEqual(sum(message.count("Side-by-side timeline") for message in messages), 1)
        self.assertNotIn("Day timeline", "\n".join(messages))
        for message in messages[:5]:
            self.assertIn("### Agenda", message)
            self.assertIn("**8:30 AM–4:00 PM** · 7h 30m free", message)
            self.assertIn("**0m scheduled** · **7h 30m available**", message)
        self.assertIn("0m / 37h 30m · 0.0% booked", messages[-1])
        self.assertEqual(len(grid_rows([messages[-1]])), 15)
        self.assertTrue(all(len(message) <= 1800 for message in messages))

    def test_huddles_overlap_totals_open_windows_and_grid_use_same_intervals(self):
        first = at(MONDAY, "08:30")
        availability = Availability((Meeting(first, at(MONDAY, "09:30")),
                                     Meeting(at(MONDAY, "09:00"), at(MONDAY, "10:00"))))
        messages = self.render(availability, morning_huddle=True)
        self.assertEqual(messages[0].count("Morning Huddle"), 1)
        self.assertIn("· Meeting 1", messages[0])
        self.assertNotIn("· Meeting 2", messages[0])
        self.assertIn("**1h 30m scheduled** · **6h 00m available**", messages[0])
        self.assertIn("**10:00 AM–4:00 PM** · 6h 00m free", messages[0])
        self.assertIn("5h 30m / 37h 30m", messages[-1])
        rows = grid_rows([messages[-1]])
        self.assertEqual([cell.strip() for cell in rows[0].split("│")][1:], ["██"] * 5)
        self.assertEqual([cell.strip() for cell in rows[2].split("│")][1:],
                         ["██", "░░", "░░", "░░", "░░"])

    def test_dense_anonymous_agenda_preserves_every_entry_and_balanced_pages(self):
        first = at(MONDAY, "09:00")
        availability = Availability(tuple(Meeting(first, first + timedelta(minutes=15))
                                          for _ in range(120)), ((MONDAY, MONDAY + timedelta(days=1)),))
        messages = self.render(availability)
        text = "\n".join(messages)
        self.assertEqual(text.count("· Meeting "), 120)
        self.assertIn("All-day busy entry present", text)
        self.assertIn("15m / 37h 30m", messages[-1])
        for message in messages:
            self.assertLessEqual(len(message), 2000)
            self.assertEqual(message.count("```") % 2, 0)
            self.assertEqual(message.count("**") % 2, 0)

    def test_single_day_retains_existing_vertical_timeline(self):
        messages = self.render(Availability(()), days=1)
        self.assertIn("Day timeline", messages[0])
        self.assertNotIn("Side-by-side timeline", messages[0])
        self.assertIn("8:30 AM │ ░░", messages[0])

    def test_long_multi_day_comparison_keeps_discord_limits(self):
        messages = self.render(Availability(()), days=7, work_start="00:00", work_end="23:59")
        self.assertTrue(all(len(message) <= 2000 for message in messages))
        self.assertTrue(all(message.count("```") % 2 == 0 for message in messages))
        self.assertEqual(len(grid_rows(messages)), 96)


if __name__ == "__main__":
    unittest.main()
