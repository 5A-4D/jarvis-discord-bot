import json
import tempfile
import unittest
from pathlib import Path

from jarvis_bot.watchdog import TransitionTracker, load_state, save_state


class TransitionTrackerTests(unittest.TestCase):
    def test_initial_health_establishes_quiet_online_baseline(self) -> None:
        tracker = TransitionTracker(failure_threshold=3, recovery_threshold=2)
        self.assertIsNone(tracker.observe(True))
        self.assertIsNone(tracker.observe(True))
        self.assertEqual(tracker.state, "online")

    def test_offline_and_online_require_thresholds(self) -> None:
        tracker = TransitionTracker(
            state="online", failure_threshold=3, recovery_threshold=2
        )
        self.assertIsNone(tracker.observe(False))
        self.assertIsNone(tracker.observe(False))
        self.assertEqual(tracker.observe(False), "offline")
        tracker.commit("offline")

        self.assertIsNone(tracker.observe(True))
        self.assertEqual(tracker.observe(True), "online")
        tracker.commit("online")
        self.assertEqual(tracker.state, "online")

    def test_state_round_trip_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(path, "offline")
            self.assertEqual(load_state(path), "offline")
            self.assertEqual(json.loads(path.read_text())["state"], "offline")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
