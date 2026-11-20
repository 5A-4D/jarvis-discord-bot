import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_bot.watchdog import notification_content, notify


class StatusPresentationTests(unittest.TestCase):
    def test_delayed_offline_notice_uses_recorded_event_time(self):
        with patch('jarvis_bot.watchdog.time.time', return_value=2000000000):
            text = notification_content('offline', observed_at=1793946000)
        self.assertIn('<t:1793946000:f>', text)
        self.assertIn('<t:1793946000:R>', text)
        self.assertNotIn('2000000000', text)
        self.assertIn('Recorded outage', text)
        self.assertIn('delivery may be delayed', text)

    def test_online_notice_only_claims_the_checks_that_recovered(self):
        text = notification_content('online', observed_at=1793946000)
        self.assertIn('Heartbeat verified', text)
        self.assertIn('Discord reachable', text)
        self.assertIn('Recovered <t:1793946000:f>', text)

    def test_zero_timestamp_is_not_replaced_with_delivery_time(self):
        self.assertIn('<t:0:f>', notification_content('offline', observed_at=0))

    def test_invalid_status_cannot_post_a_false_recovery(self):
        with patch('jarvis_bot.watchdog.send_message_with_attachment') as send:
            with self.assertRaises(ValueError):
                notify(123, 'unknown', Path('test.gif'), 'private', observed_at=1)
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
