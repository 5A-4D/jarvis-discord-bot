"""Synthetic restart regressions for persisted live request progress."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis_bot.bot import discord_progress_text
from jarvis_bot.progress import (
    MAX_EVENTS_PER_REQUEST,
    LiveProgressStore,
    _read_payload,
    render_progress,
)


class ProgressRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.json"
        self.store = LiveProgressStore(self.path)

    async def start(self, store, request_id="synthetic-request"):
        await store.start(
            request_id, mode="chat", conversation_id="synthetic-scope", session_id=None
        )

    async def test_restart_marks_unfinished_work_interrupted_with_frozen_observed_duration(self):
        with patch("jarvis_bot.progress._now", return_value="2026-11-09T10:00:00+00:00"):
            await self.start(self.store)
        with patch("jarvis_bot.progress._now", return_value="2026-11-09T10:00:30+00:00"):
            await self.store.record(
                "synthetic-request", {"type": "thread.started", "thread_id": "synthetic-session"}
            )
            await self.store.record("synthetic-request", {
                "type": "item.started",
                "item": {"type": "command_execution", "command": "API_TOKEN=synthetic-secret echo safe"},
            })
        before = json.loads(self.path.read_text())["active"][0]

        restarted = LiveProgressStore(self.path)
        notified = Mock()
        restarted.add_listener(notified)
        with patch("jarvis_bot.progress._now", return_value="2026-11-09T12:00:00+00:00"):
            await restarted.reset()
        payload = json.loads(self.path.read_text())
        request = payload["last_completed"]

        self.assertEqual(payload["active"], [])
        self.assertEqual(payload["interrupted"], [request])
        self.assertEqual(request["outcome"], "interrupted")
        self.assertEqual(request["session_id"], "synthetic-session")
        self.assertEqual(request["events"][:-1], before["events"])
        self.assertEqual(request["last_activity_at"], before["updated_at"])
        self.assertEqual(request["interrupted_at"], "2026-11-09T12:00:00+00:00")
        self.assertNotIn("completed_at", request, "Restart time is not a known completion time")
        self.assertEqual((await restarted.snapshot())["interrupted"], [request])
        notified.assert_called_once_with()

        rendered = render_progress(payload, verbose=True)
        self.assertIn("interrupted, at least 30s", rendered)
        self.assertIn("Execution was not resumed", rendered)
        self.assertIn("synthetic-session", rendered)
        self.assertIn("API_TOKEN=<redacted>", rendered)
        self.assertNotIn("synthetic-secret", self.path.read_text())
        self.assertNotIn("API_TOKEN", render_progress(payload))
        self.assertIn("interrupted", discord_progress_text(payload))

    async def test_all_concurrent_requests_survive_repeated_restart_without_duplicate_events(self):
        await self.start(self.store, "request-a")
        await self.start(self.store, "request-b")
        restarted = LiveProgressStore(self.path)
        await restarted.reset()
        first = json.loads(self.path.read_text())
        self.assertEqual(
            [request["request_id"] for request in first["interrupted"]],
            ["request-a", "request-b"],
        )
        self.assertTrue(all(request["outcome"] == "interrupted" for request in first["interrupted"]))
        self.assertIn("request-a, request-b", render_progress(first))

        await LiveProgressStore(self.path).reset()
        second = json.loads(self.path.read_text())
        self.assertEqual(second["active"], [])
        self.assertEqual(second["interrupted"], first["interrupted"])
        self.assertEqual(second["last_completed"], first["last_completed"])

    async def test_new_requests_complete_normally_after_recovery(self):
        await self.start(self.store, "interrupted-request")
        restarted = LiveProgressStore(self.path)
        await restarted.reset()
        await self.start(restarted, "new-request")
        running = await restarted.snapshot()
        self.assertEqual([request["request_id"] for request in running["active"]], ["new-request"])
        self.assertIn("1 active request(s)", render_progress(running))
        self.assertIn("Interrupted before restart: interrupted-request", render_progress(running))

        await restarted.finish("new-request", "success")
        completed = json.loads(self.path.read_text())
        self.assertEqual(completed["active"], [])
        self.assertEqual(completed["last_completed"]["request_id"], "new-request")
        self.assertEqual(completed["last_completed"]["outcome"], "success")
        self.assertEqual(completed["interrupted"][0]["outcome"], "interrupted")

    async def test_known_completion_outcome_survives_restart(self):
        for outcome in ("success", "error", "cancelled"):
            with self.subTest(outcome=outcome):
                await self.start(self.store)
                await self.store.finish("synthetic-request", outcome)
                previous = json.loads(self.path.read_text())["last_completed"]
                await LiveProgressStore(self.path).reset()
                recovered = json.loads(self.path.read_text())
                self.assertEqual(recovered["active"], [])
                self.assertEqual(recovered["interrupted"], [])
                self.assertEqual(recovered["last_completed"], previous)

    async def test_recovery_keeps_event_history_bounded(self):
        await self.start(self.store)
        for _ in range(MAX_EVENTS_PER_REQUEST + 2):
            await self.store.record("synthetic-request", {"type": "turn.started"})
        before = json.loads(self.path.read_text())["active"][0]["events"]
        await LiveProgressStore(self.path).reset()
        events = json.loads(self.path.read_text())["last_completed"]["events"]
        self.assertEqual(len(events), MAX_EVENTS_PER_REQUEST)
        self.assertEqual(events[:-1], before[-(MAX_EVENTS_PER_REQUEST - 1):])
        self.assertIn("interrupted", events[-1]["summary"])

    async def test_missing_snapshot_starts_cleanly(self):
        await self.store.reset()
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["active"], [])
        self.assertEqual(payload["interrupted"], [])
        self.assertIsNone(payload["last_completed"])

    async def test_invalid_snapshots_report_unavailable_then_allow_new_work(self):
        fixtures = (
            b"{broken", b"\xff", b"[]", b'{"active": {}}',
            b'{"active": [null]}', b'{"active": [{"events": null}]}',
            b'{"interrupted": {}}', b'{"last_completed": "invalid"}',
        )
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.path.write_bytes(fixture)
                rendered = render_progress(_read_payload(self.path))
                self.assertIn("Progress state unavailable", rendered)
                self.assertNotIn("No active request", rendered)
                self.assertNotIn("0 active request(s)", rendered)
                restarted = LiveProgressStore(self.path)
                with self.assertLogs("jarvis_bot.progress", level="WARNING") as logs:
                    await restarted.reset()
                self.assertIn("Could not recover previous live progress", logs.output[0])
                self.assertEqual((await restarted.snapshot())["active"], [])
                await self.start(restarted)
                await restarted.finish("synthetic-request", "error")
                self.assertEqual(json.loads(self.path.read_text())["last_completed"]["outcome"], "error")

    async def test_unreadable_snapshot_warns_without_blocking_startup(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("Synthetic read failure")):
            with self.assertLogs("jarvis_bot.progress", level="WARNING") as logs:
                await self.store.reset()
        self.assertIn("Synthetic read failure", logs.output[0])
        await self.start(self.store)
        self.assertEqual(len(json.loads(self.path.read_text())["active"]), 1)

    async def test_missing_or_invalid_activity_times_render_unknown_instead_of_crashing(self):
        for timestamp in (None, "invalid", "2026-11-09T10:00:00"):
            with self.subTest(timestamp=timestamp):
                self.path.write_text(json.dumps({"active": [{
                    "request_id": "synthetic-request",
                    "started_at": "2026-11-09T10:00:00+00:00",
                    "updated_at": timestamp,
                    "events": [],
                }]}))
                await LiveProgressStore(self.path).reset()
                rendered = render_progress(json.loads(self.path.read_text()))
                self.assertIn("interrupted, unknown", rendered)
                self.assertNotIn("at least unknown", rendered)

    async def test_failed_recovery_write_keeps_original_snapshot_for_retry(self):
        await self.start(self.store)
        previous = self.path.read_bytes()
        restarted = LiveProgressStore(self.path)
        notified = Mock()
        restarted.add_listener(notified)
        with patch("jarvis_bot.progress.os.replace", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaisesRegex(OSError, "Synthetic disk failure"):
                await restarted.reset()
        self.assertEqual(self.path.read_bytes(), previous)
        notified.assert_not_called()
        await LiveProgressStore(self.path).reset()
        recovered = json.loads(self.path.read_text())
        self.assertEqual(recovered["active"], [])
        self.assertEqual(recovered["last_completed"]["outcome"], "interrupted")


if __name__ == "__main__":
    unittest.main()
