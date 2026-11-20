import hashlib
import unittest
from datetime import date

from jarvis_bot.work_calendar import ScheduleQuery, parse_availability, render_schedule


def calendar(*events):
    text = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Lunch tests//EN\r\n'
    for event in events:
        text += 'BEGIN:VEVENT\r\n' + event.replace('\n', '\r\n') + '\r\nEND:VEVENT\r\n'
    return (text + 'END:VCALENDAR\r\n').encode()


LUNCH = ('UID:private-lunch-fixture\nDTSTART;TZID=America/Chicago:20261103T123000\n'
         'DTEND;TZID=America/Chicago:20261103T133000\nRRULE:FREQ=DAILY;COUNT=5\n'
         'SUMMARY:private-title\nDESCRIPTION:private-description')
HASHES = frozenset({hashlib.sha256(b'private-lunch-fixture').hexdigest()})
ZONE = 'America/Chicago'


class LunchAvailabilityTests(unittest.TestCase):
    def test_override_is_opt_in_and_repeated_lunch_becomes_free(self):
        query = ScheduleQuery(date(2026, 11, 3), 3)
        self.assertEqual(len(parse_availability(calendar(LUNCH), query, ZONE).meetings), 3)
        available = parse_availability(calendar(LUNCH), query, ZONE, free_series_hashes=HASHES)
        self.assertEqual(available.meetings, ())
        output = '\n'.join(render_schedule(available, query))
        self.assertIn('0m scheduled', output)
        self.assertIn('7h 30m free', output)
        for secret in ('private-lunch-fixture', 'private-title', 'private-description', *HASHES):
            self.assertNotIn(secret, repr(available) + output)

    def test_new_meeting_at_identical_time_still_blocks_the_whole_hour(self):
        meeting = ('UID:real-meeting\nDTSTART;TZID=America/Chicago:20261104T123000\n'
                   'DTEND;TZID=America/Chicago:20261104T133000')
        query = ScheduleQuery(date(2026, 11, 4))
        a = parse_availability(calendar(LUNCH, meeting), query, ZONE, free_series_hashes=HASHES)
        self.assertEqual(len(a.meetings), 1)
        output = '\n'.join(render_schedule(a, query))
        self.assertIn('**1h 00m scheduled**', output)
        self.assertIn('12:30 PM–1:30 PM', output)

    def test_partial_overlap_only_blocks_the_new_meeting(self):
        meeting = ('UID:real-meeting\nDTSTART;TZID=America/Chicago:20261104T124500\n'
                   'DTEND;TZID=America/Chicago:20261104T131500')
        query = ScheduleQuery(date(2026, 11, 4))
        a = parse_availability(calendar(LUNCH, meeting), query, ZONE, free_series_hashes=HASHES)
        output = '\n'.join(render_schedule(a, query))
        self.assertIn('**30m scheduled**', output)
        self.assertIn('**8:30 AM–12:45 PM**', output)
        self.assertIn('**1:15 PM–4:00 PM**', output)
        self.assertIn('12:30 PM │ ░█', output)
        self.assertIn(' 1:00 PM │ █░', output)

    def test_moved_series_exception_is_not_silently_freed(self):
        exception = ('UID:private-lunch-fixture\nRECURRENCE-ID;TZID=America/Chicago:20261104T123000\n'
                     'DTSTART;TZID=America/Chicago:20261104T130000\n'
                     'DTEND;TZID=America/Chicago:20261104T140000')
        a = parse_availability(calendar(LUNCH, exception), ScheduleQuery(date(2026, 11, 4)),
                               ZONE, free_series_hashes=HASHES)
        self.assertEqual(len(a.meetings), 1)
        self.assertEqual(a.meetings[0].start.hour, 13)

    def test_unknown_fingerprint_does_not_free_lunch_and_weekend_is_not_ignored(self):
        query = ScheduleQuery(date(2026, 11, 3))
        a = parse_availability(calendar(LUNCH), query, ZONE, free_series_hashes=frozenset({'0'*64}))
        self.assertEqual(len(a.meetings), 1)
        weekend = parse_availability(calendar(LUNCH), ScheduleQuery(date(2026, 11, 7)),
                                     ZONE, free_series_hashes=HASHES)
        self.assertEqual(len(weekend.meetings), 1)


if __name__ == '__main__':
    unittest.main()
