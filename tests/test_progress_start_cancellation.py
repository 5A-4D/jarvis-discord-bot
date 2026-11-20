"""Cancellation during registration must not strand an active request."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jarvis_bot.assistant import JarvisAssistant
from jarvis_bot.bot import discord_progress_text
from jarvis_bot.memory import MemoryStore
from jarvis_bot.progress import LiveProgressStore, render_progress


class ProgressStartCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.json"
        self.store = LiveProgressStore(self.path)

    async def start(self, request_id="cancelled-request"):
        await self.store.start(
            request_id, mode="chat", conversation_id="synthetic-scope", session_id=None,
        )

    async def cancel_during_write(self, operation, *, fail_calls=(), cancel_cleanup=False):
        """Gate actual disk writes so cancellation never depends on disk speed."""
        loop = asyncio.get_running_loop()
        started = [asyncio.Event(), asyncio.Event()]
        release = [threading.Event(), threading.Event()]
        original_write = self.store._write
        calls = 0

        def controlled_write():
            nonlocal calls
            calls += 1
            call = calls
            if call == 1 or (call == 2 and cancel_cleanup):
                loop.call_soon_threadsafe(started[call - 1].set)
                if not release[call - 1].wait(timeout=5):
                    raise RuntimeError("Synthetic progress write was not released")
            if call in fail_calls:
                raise OSError(f"Synthetic write failure {call}")
            original_write()

        with patch.object(self.store, "_write", controlled_write):
            task = asyncio.create_task(operation)
            try:
                await asyncio.wait_for(started[0].wait(), timeout=2)
                task.cancel("original cancellation")
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "The initial writer must finish first")
                release[0].set()
                if cancel_cleanup:
                    await asyncio.wait_for(started[1].wait(), timeout=2)
                    for _ in range(3):
                        task.cancel("later cancellation")
                        await asyncio.sleep(0)
                    self.assertFalse(task.done(), "Cancellation must wait for cleanup")
                    release[1].set()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                self.assertEqual(str(caught.exception), "original cancellation")
            finally:
                for gate in release:
                    gate.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def assert_cancelled(self):
        snapshot = await self.store.snapshot()
        self.assertEqual(snapshot["active"], [])
        request = snapshot["last_completed"]
        self.assertEqual(request["request_id"], "cancelled-request")
        self.assertEqual(request["outcome"], "cancelled")
        self.assertEqual(request["updated_at"], request["completed_at"])
        return snapshot

    async def test_assistant_cancelled_during_registration_leaves_terminal_result(self):
        memory = AsyncMock(spec=MemoryStore)
        memory.get_session_id.return_value = None
        assistant = JarvisAssistant(
            codex_binary=Path("/synthetic/codex"), workdir=self.path.parent,
            sandbox="read-only", timeout_seconds=10, persona="Synthetic",
            memory=memory, progress=self.store,
        )
        assistant._run = AsyncMock()
        publications = []
        self.store.add_listener(lambda: publications.append(json.loads(self.path.read_text())))

        await self.cancel_during_write(assistant.respond(
            "synthetic-scope", 123, "Synthetic prompt", request_id="cancelled-request",
        ))

        assistant._run.assert_not_awaited()
        memory.append_turn.assert_not_awaited()
        snapshot = await self.assert_cancelled()
        published = json.loads(self.path.read_text())
        self.assertEqual(published["last_completed"], snapshot["last_completed"])
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]["active"], [])
        for rendered in (render_progress(published), discord_progress_text(published)):
            self.assertIn("cancelled", rendered)
        self.assertNotIn("Synthetic prompt", self.path.read_text())

        restarted = LiveProgressStore(self.path)
        await restarted.reset()
        recovered = await restarted.snapshot()
        self.assertEqual(recovered["active"], [])
        self.assertEqual(recovered["interrupted"], [])
        self.assertEqual(recovered["last_completed"], snapshot["last_completed"])

    async def test_unrelated_active_requests_keep_their_sessions_and_events(self):
        await self.start("running-request")
        await self.store.record("running-request", {
            "type": "thread.started", "thread_id": "synthetic-running-session",
        })
        running = (await self.store.snapshot())["active"]

        await self.cancel_during_write(self.start())

        published = json.loads(self.path.read_text())
        self.assertEqual(published["active"], running)
        self.assertEqual(published["last_completed"]["outcome"], "cancelled")
        await self.store.record("cancelled-request", {"type": "turn.started"})
        self.assertEqual(json.loads(self.path.read_text()), published)
        await self.store.finish("running-request", "success")
        self.assertEqual(json.loads(self.path.read_text())["active"], [])

    async def test_cancelled_replacement_restores_preexisting_request(self):
        await self.start("completed-request")
        await self.store.finish("completed-request", "success")
        await self.start()
        await self.store.record("cancelled-request", {
            "type": "thread.started", "thread_id": "synthetic-original-session",
        })
        previous = await self.store.snapshot()

        await self.cancel_during_write(self.start())

        snapshot = await self.store.snapshot()
        self.assertEqual(snapshot["active"], previous["active"])
        self.assertEqual(snapshot["last_completed"], previous["last_completed"])
        published = json.loads(self.path.read_text())
        self.assertEqual(published["active"], previous["active"])
        self.assertEqual(published["last_completed"], previous["last_completed"])

    async def test_failed_initial_write_keeps_cancellation_and_publishes_terminal_state(self):
        with self.assertLogs("jarvis_bot.progress", level="ERROR") as logs:
            await self.cancel_during_write(self.start(), fail_calls={1})
        self.assertIn("Synthetic write failure 1", "\n".join(logs.output))
        snapshot = await self.assert_cancelled()
        self.assertEqual(json.loads(self.path.read_text())["last_completed"], snapshot["last_completed"])

    async def test_failed_cleanup_keeps_cancellation_and_next_write_removes_stale_active_state(self):
        publications = []
        self.store.add_listener(lambda: publications.append(json.loads(self.path.read_text())))
        with self.assertLogs("jarvis_bot.progress", level="ERROR") as logs:
            await self.cancel_during_write(self.start(), fail_calls={2})
        self.assertIn("Synthetic write failure 2", "\n".join(logs.output))
        await self.assert_cancelled()
        self.assertEqual(publications, [], "Viewers must not be notified of abandoned work")
        # Disk cannot reflect cleanup until storage accepts a subsequent write.
        self.assertEqual(len(json.loads(self.path.read_text())["active"]), 1)
        await self.start("next-request")
        published = json.loads(self.path.read_text())
        self.assertEqual([r["request_id"] for r in published["active"]], ["next-request"])
        self.assertEqual(published["last_completed"]["outcome"], "cancelled")
        self.assertEqual(publications, [published])

    async def test_repeated_cancellation_during_cleanup_keeps_original_reason(self):
        await self.cancel_during_write(self.start(), cancel_cleanup=True)
        await self.assert_cancelled()
        self.assertEqual(json.loads(self.path.read_text())["active"], [])

    async def test_cancellation_while_waiting_for_lock_leaves_state_untouched(self):
        await self.start("running-request")
        previous = self.path.read_bytes()
        async with self.store._lock:
            task = asyncio.create_task(self.start())
            await asyncio.sleep(0)
            task.cancel("cancelled before registration")
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(len((await self.store.snapshot())["active"]), 1)


if __name__ == "__main__":
    unittest.main()
