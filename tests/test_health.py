import tempfile
import unittest
from pathlib import Path

from jarvis_bot.health import heartbeat_is_healthy, read_heartbeat, write_heartbeat


class HeartbeatTests(unittest.TestCase):
    def test_ready_fresh_heartbeat_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            write_heartbeat(path, ready=True, timestamp=100.0, process_id=42)

            self.assertTrue(
                heartbeat_is_healthy(path, maximum_age_seconds=45, now=130.0)
            )
            self.assertEqual(read_heartbeat(path)["pid"], 42)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stale_or_disconnected_heartbeat_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            write_heartbeat(path, ready=True, timestamp=100.0)
            self.assertFalse(
                heartbeat_is_healthy(path, maximum_age_seconds=45, now=146.0)
            )

            write_heartbeat(path, ready=False, timestamp=200.0)
            self.assertFalse(
                heartbeat_is_healthy(path, maximum_age_seconds=45, now=200.0)
            )


if __name__ == "__main__":
    unittest.main()
