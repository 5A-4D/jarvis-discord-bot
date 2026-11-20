from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from jarvis_bot.send_countdown import (
    build_message,
    countdown_values,
    main,
    publish_countdown,
)


class CountdownTests(unittest.TestCase):
    def test_countdown_uses_decimal_days_and_ascii_bar(self) -> None:
        now = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)

        self.assertEqual(
            build_message(now),
            "`⚔ 〔█░░░░░░░░░░░░░░░░░░░〕 1.00 / 33.27`",
        )

    def test_release_instant_is_zero_days(self) -> None:
        now = datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc)

        self.assertEqual(
            build_message(now),
            "`⚔ 〔░░░░░░░░░░░░░░░░░░░░〕 0.00 / 33.27`",
        )

    def test_countdown_rejects_naive_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            countdown_values(datetime(2026, 10, 2, 16, 30, 29))

    def test_countdown_stops_after_release(self) -> None:
        now = datetime(2026, 11, 4, 23, 0, 1, tzinfo=timezone.utc)

        self.assertIsNone(build_message(now))

    def test_publish_posts_then_edits_until_three_days_have_passed(self) -> None:
        first = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)
        second = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
        fourth_day = datetime(2026, 10, 5, 17, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with (
                patch(
                    "jarvis_bot.send_countdown.send_message",
                    side_effect=["first", "second"],
                ) as send,
                patch("jarvis_bot.send_countdown.edit_message") as edit,
            ):
                self.assertEqual(
                    publish_countdown(123, first, state_file), "posted:first"
                )
                self.assertEqual(
                    publish_countdown(123, second, state_file), "edited:first"
                )
                self.assertEqual(
                    publish_countdown(123, fourth_day, state_file), "posted:second"
                )

            self.assertEqual(send.call_count, 2)
            edit.assert_called_once()

    def test_main_publishes_generated_countdown(self) -> None:
        now = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)
        with (
            patch("jarvis_bot.send_countdown.datetime") as mocked_datetime,
            patch(
                "jarvis_bot.send_countdown.publish_countdown",
                return_value="posted:789",
            ) as publish,
            patch(
                "sys.argv",
                ["send_countdown", "--channel-id", "123", "--state-file", "/tmp/x"],
            ),
        ):
            mocked_datetime.now.return_value = now
            main()

        publish.assert_called_once_with(
            123,
            now,
            Path("/tmp/x"),
        )


if __name__ == "__main__":
    unittest.main()
