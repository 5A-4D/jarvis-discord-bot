"""Synthetic regressions for recovery of partially damaged progress snapshots."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis_bot.bot import discord_progress_text
from jarvis_bot.progress import LiveProgressStore, _read_payload, render_progress


def request(request_id, *, outcome=None):
    value = {
        "request_id": request_id,
        "mode": "chat",
        "conversation_id": "synthetic-scope",
        "session_id": "synthetic-session",
        "started_at": "2026-11-20T10:00:00+00:00",
        "updated_at": "2026-11-20T10:00:30+00:00",
        "events": [{
            "at": "2026-11-20T10:00:30+00:00",
            "summary": "Codex turn started",
        }],
        "checklist": {"completed": 1, "total": 2},
    }
    if outcome:
        value["outcome"] = outcome
        if outcome == "interrupted":
            value["last_activity_at"] = value["updated_at"]
            value["interrupted_at"] = "2026-11-20T11:00:00+00:00"
        else:
            value["completed_at"] = value["updated_at"]
    return value


class PartialProgressRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "progress.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    async def recover(self):
        store = LiveProgressStore(self.path)
        listener = Mock()
        store.add_listener(listener)
        with self.assertLogs("jarvis_bot.progress", level="WARNING") as logs:
            with patch("jarvis_bot.progress._now", return_value="2026-11-20T12:00:00+00:00"):
                await store.reset()
        self.assertIn("Could not recover previous live progress", logs.output[0])
        listener.assert_called_once_with()
        snapshot = await store.snapshot()
        self.assertEqual(json.loads(self.path.read_text())["interrupted"], snapshot["interrupted"])
        self.assertNotIn("read_error", snapshot)
        return store, snapshot

    async def test_valid_concurrent_requests_survive_malformed_siblings(self):
        first, second = request("request-a"), request("request-b")
        self.write({
            "active": [None, first, {"events": None}, second, "damaged"],
            "last_completed": request("previous-result", outcome="success"),
        })
        _, recovered = await self.recover()
        self.assertEqual(recovered["active"], [])
        self.assertEqual([item["request_id"] for item in recovered["interrupted"]],
                         ["request-a", "request-b"])
        for original, item in zip((first, second), recovered["interrupted"]):
            self.assertEqual(item["outcome"], "interrupted")
            self.assertEqual(item["session_id"], original["session_id"])
            self.assertEqual(item["events"][:-1], original["events"])
            self.assertEqual(item["checklist"], original["checklist"])
            self.assertEqual(item["last_activity_at"], original["updated_at"])
            self.assertNotIn("completed_at", item)
        self.assertEqual(recovered["last_completed"], recovered["interrupted"][-1])
        for text in (render_progress(recovered), discord_progress_text(recovered)):
            self.assertIn("request-a, request-b", text)
            self.assertIn("Execution was not resumed", text)
            self.assertIn("interrupted, at least 30s", text)

    async def test_malformed_fields_do_not_discard_other_request_groups(self):
        for field in ("active", "interrupted", "last_completed"):
            with self.subTest(field=field):
                payload = {
                    "active": [request("unfinished")],
                    "interrupted": [request("earlier-interruption", outcome="interrupted")],
                    "last_completed": request("known-result", outcome="error"),
                }
                payload[field] = "synthetic damaged field"
                self.write(payload)
                _, recovered = await self.recover()
                if field == "active":
                    self.assertEqual(recovered["last_completed"], payload["last_completed"])
                    self.assertEqual(recovered["interrupted"], payload["interrupted"])
                else:
                    self.assertEqual(recovered["interrupted"][0]["request_id"], "unfinished")
                    self.assertEqual(recovered["last_completed"]["outcome"], "interrupted")

    async def test_known_completion_survives_when_all_active_entries_are_damaged(self):
        for outcome in ("success", "error", "cancelled"):
            with self.subTest(outcome=outcome):
                known = request("known-result", outcome=outcome)
                self.write({"active": [None, {"events": {}}], "last_completed": known})
                _, recovered = await self.recover()
                self.assertEqual(recovered["active"], [])
                self.assertEqual(recovered["last_completed"], known)
                self.assertIn(f"known-result ({outcome}, 30s)", render_progress(recovered))

    async def test_valid_earlier_interruptions_survive_damaged_siblings(self):
        earlier = request("earlier-interruption", outcome="interrupted")
        self.write({"active": [], "interrupted": [False, earlier, {"events": None}]})
        _, recovered = await self.recover()
        self.assertEqual(recovered["interrupted"], [earlier])
        self.assertIn("earlier-interruption", render_progress(recovered))

    async def test_watcher_reports_unknown_count_and_read_does_not_modify_snapshot(self):
        self.write({"active": [request("unfinished"), None], "last_completed": []})
        original = self.path.read_bytes()
        payload = _read_payload(self.path)
        self.assertEqual([item["request_id"] for item in payload["active"]], ["unfinished"])
        self.assertIsNone(payload["last_completed"])
        self.assertIn("read_error", payload)
        text = render_progress(payload)
        self.assertIn("active request count unknown", text)
        self.assertIn("Progress state unavailable", text)
        self.assertNotIn("0 active request(s)", text)
        self.assertNotIn("1 active request(s)", text)
        self.assertEqual(self.path.read_bytes(), original)

    async def test_recovered_records_survive_next_restart_and_new_work(self):
        self.write({"active": [request("unfinished"), None]})
        store, recovered = await self.recover()
        before = copy.deepcopy(recovered["interrupted"])
        self.assertEqual([item["request_id"] for item in before], ["unfinished"])
        await store.start("new-request", mode="chat", conversation_id="synthetic-scope", session_id=None)
        await store.finish("new-request", "success")
        restarted = LiveProgressStore(self.path)
        await restarted.reset()
        latest = await restarted.snapshot()
        self.assertEqual(latest["interrupted"], before)
        self.assertEqual(latest["last_completed"]["request_id"], "new-request")
        self.assertEqual(latest["last_completed"]["outcome"], "success")
        self.assertEqual(latest["active"], [])

    async def test_failed_recovery_write_preserves_original_for_next_restart(self):
        self.write({"active": [None, request("unfinished")]})
        original = self.path.read_bytes()
        store = LiveProgressStore(self.path)
        listener = Mock()
        store.add_listener(listener)
        with patch("jarvis_bot.progress.os.replace", side_effect=OSError("Synthetic write failure")):
            with self.assertLogs("jarvis_bot.progress", level="WARNING"):
                with self.assertRaisesRegex(OSError, "Synthetic write failure"):
                    await store.reset()
        self.assertEqual(self.path.read_bytes(), original)
        listener.assert_not_called()
        _, recovered = await self.recover()
        self.assertEqual([item["request_id"] for item in recovered["interrupted"]],
                         ["unfinished"])
        self.assertEqual(len(recovered["interrupted"][0]["events"]), 2)

    async def test_warning_does_not_include_rejected_request_content(self):
        self.write({
            "active": [request("unfinished"), {
                "request_id": "synthetic private identifier",
                "events": "synthetic private content",
            }],
            "interrupted": "synthetic private content",
            "last_completed": "synthetic private content",
        })
        self.assertNotIn("synthetic private", _read_payload(self.path)["read_error"])
        with self.assertLogs("jarvis_bot.progress", level="WARNING") as logs:
            await LiveProgressStore(self.path).reset()
        self.assertNotIn("synthetic private", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
