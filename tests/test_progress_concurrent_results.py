"""Synthetic regressions for terminal results while other requests keep running."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_bot.bot import DISCORD_MESSAGE_LIMIT, discord_progress_text
from jarvis_bot.progress import LiveProgressStore, render_progress


class ConcurrentProgressResultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.json"
        self.store = LiveProgressStore(self.path)

    async def start(self, request_id):
        with patch("jarvis_bot.progress._now", return_value="2026-11-17T10:00:00+00:00"):
            await self.store.start(
                request_id, mode="chat", conversation_id="synthetic-scope", session_id=None,
            )

    async def finish(self, request_id, outcome):
        with patch("jarvis_bot.progress._now", return_value="2026-11-17T10:00:30+00:00"):
            await self.store.finish(request_id, outcome)

    async def test_each_terminal_outcome_remains_visible_beside_running_work(self):
        await self.start("still-running")
        other = (await self.store.snapshot())["active"][0]
        for outcome in ("success", "error", "cancelled"):
            with self.subTest(outcome=outcome):
                request_id = f"finished-{outcome}"
                await self.start(request_id)
                await self.store.record(request_id, {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "Synthetic finished-only detail"},
                })
                await self.finish(request_id, outcome)
                payload = json.loads(self.path.read_text())
                self.assertEqual(payload["active"], [other])
                self.assertEqual(payload["last_completed"]["outcome"], outcome)
                expected = f"Last: {request_id} ({outcome}, 30s)."
                for rendered in (
                    render_progress(payload), render_progress(payload, verbose=True),
                    discord_progress_text(payload),
                ):
                    self.assertIn("1 active request(s)", rendered)
                    self.assertIn(expected, rendered)
                    self.assertNotIn("No active request", rendered)
                    self.assertNotIn("Synthetic finished-only detail", rendered)
                self.assertIn(expected, render_progress(payload).splitlines()[0])

    async def test_discord_truncation_keeps_latest_result(self):
        await self.start("finished-request")
        await self.start("still-running")
        await self.finish("finished-request", "error")
        for _ in range(5):
            await self.store.record("still-running", {
                "type": "item.completed",
                "item": {"type": "reasoning", "text": "Synthetic activity. " * 70},
            })
        payload = await self.store.snapshot()
        self.assertGreater(len(render_progress(payload, verbose=True)), DISCORD_MESSAGE_LIMIT)
        rendered = discord_progress_text(payload)
        self.assertLessEqual(len(rendered), DISCORD_MESSAGE_LIMIT)
        self.assertIn("earlier activity omitted", rendered)
        self.assertIn("Last: finished-request (error, 30s).", rendered)
        self.assertIn("1 active request(s)", rendered)

    async def test_recovered_interruption_stays_visible_when_new_work_starts(self):
        await self.start("interrupted-request")
        with patch("jarvis_bot.progress._now", return_value="2026-11-17T10:00:30+00:00"):
            await self.store.record("interrupted-request", {"type": "turn.started"})
        self.store = LiveProgressStore(self.path)
        with patch("jarvis_bot.progress._now", return_value="2026-11-17T12:00:00+00:00"):
            await self.store.reset()
        await self.start("new-request")
        payload = await self.store.snapshot()
        self.assertEqual([request["request_id"] for request in payload["active"]], ["new-request"])
        expected = "Last: interrupted-request (interrupted, at least 30s)."
        self.assertIn(expected, render_progress(payload).splitlines()[0])
        self.assertIn("Execution was not resumed", render_progress(payload))
        self.assertNotIn("completed_at", payload["last_completed"])

    async def test_item_failure_does_not_invent_a_terminal_request_result(self):
        await self.start("still-running")
        await self.store.record("still-running", {
            "type": "item.completed", "item": {"type": "mcp_tool_call", "status": "failed"},
        })
        payload = await self.store.snapshot()
        self.assertIsNone(payload["last_completed"])
        self.assertEqual(len(payload["active"]), 1)
        for rendered in (render_progress(payload), discord_progress_text(payload)):
            self.assertIn("Tool call failed", rendered)
            self.assertNotIn("Last:", rendered)

    async def test_latest_completion_replaces_previous_result_without_mutating_snapshot(self):
        await self.start("still-running")
        await self.start("first-result")
        await self.finish("first-result", "error")
        previous = await self.store.snapshot()
        await self.start("new-result")
        await self.finish("new-result", "success")
        current = await self.store.snapshot()
        self.assertIn("Last: new-result (success, 30s).", render_progress(current))
        self.assertNotIn("first-result", render_progress(current))
        self.assertIn("Last: first-result (error, 30s).", render_progress(previous))
        await self.finish("still-running", "cancelled")
        idle = render_progress(await self.store.snapshot())
        self.assertIn("No active request. Last: still-running (cancelled, 30s).", idle)
        self.assertEqual(idle.count("Last:"), 1)


if __name__ == "__main__":
    unittest.main()
