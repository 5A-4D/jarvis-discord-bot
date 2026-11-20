"""Synthetic regressions for cancellation before completion acquires its lock."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_bot.progress import LiveProgressStore, render_progress


class QueuedCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.json"
        self.store = LiveProgressStore(self.path)
        await self.start("finished-request")
        await self.store.record("finished-request", {
            "type": "thread.started", "thread_id": "synthetic-session",
        })
        await self.start("other-request")

    async def start(self, request_id):
        await self.store.start(
            request_id, mode="chat", conversation_id="synthetic-scope", session_id=None,
        )

    async def queued_finish(self, outcome):
        """Return a completion task cancelled after it starts waiting for the lock."""
        entered = asyncio.Event()

        async def finish():
            entered.set()
            await self.store.finish("finished-request", outcome)

        task = asyncio.create_task(finish())
        await entered.wait()
        task.cancel("first synthetic interruption")
        await asyncio.sleep(0)
        return task

    async def check_queued_outcome(self, outcome):
        before = await self.store.snapshot()
        request = before["active"][0]
        other = before["active"][1]
        publications = []
        listener = lambda: publications.append(json.loads(self.path.read_text()))
        self.store.add_listener(listener)
        async with self.store._lock:
            task = await self.queued_finish(outcome)
            waiting = not task.done()
            self.assertEqual(publications, [])
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task

        self.assertTrue(waiting, "Queued cancellation abandoned completion cleanup")
        self.assertEqual(str(caught.exception), "first synthetic interruption")
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["active"], [other])
        completed = payload["last_completed"]
        self.assertEqual(completed["outcome"], outcome)
        self.assertEqual(completed["events"], request["events"])
        self.assertEqual(completed["session_id"], "synthetic-session")
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0], payload)
        self.assertEqual((await self.store.snapshot())["last_completed"], completed)

        restarted = LiveProgressStore(self.path)
        await restarted.reset()
        recovered = await restarted.snapshot()
        # The other request was truly interrupted; the completed one
        # must never be recovered as unfinished work.
        self.assertEqual(
            [item["request_id"] for item in recovered["interrupted"]],
            ["other-request"],
        )
        await self.store.finish("other-request", "success")
        self.assertIn("0 active request(s)", render_progress(await self.store.snapshot()))

    async def test_queued_success_is_persisted_before_cancellation_propagates(self):
        await self.check_queued_outcome("success")

    async def test_queued_error_is_persisted_before_cancellation_propagates(self):
        await self.check_queued_outcome("error")

    async def test_queued_cancelled_outcome_is_persisted_before_cancellation_propagates(self):
        await self.check_queued_outcome("cancelled")

    async def test_repeated_cancellation_waits_for_real_record_and_completion_writes(self):
        loop = asyncio.get_running_loop()
        started = [asyncio.Event(), asyncio.Event()]
        release = [threading.Event(), threading.Event()]
        write = self.store._write
        calls = 0
        publications = []
        self.store.add_listener(lambda: publications.append(json.loads(self.path.read_text())))

        def paused_write():
            nonlocal calls
            index = calls
            calls += 1
            loop.call_soon_threadsafe(started[index].set)
            if not release[index].wait(timeout=5):
                raise RuntimeError("Test did not release synthetic writer")
            write()

        with patch.object(self.store, "_write", side_effect=paused_write):
            record = asyncio.create_task(self.store.record("finished-request", {
                "type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3},
            }))
            finish = None
            try:
                await asyncio.wait_for(started[0].wait(), timeout=2)
                finish = await self.queued_finish("success")
                finish.cancel("second synthetic interruption")
                await asyncio.sleep(0)
                self.assertFalse(finish.done())
                self.assertEqual(calls, 1, "Completion wrote while another writer held the lock")
                release[0].set()
                await record
                await asyncio.wait_for(started[1].wait(), timeout=2)
                finish.cancel("third synthetic interruption")
                await asyncio.sleep(0)
                self.assertFalse(finish.done())
                self.assertEqual(len(publications), 1)
            finally:
                for gate in release:
                    gate.set()
                await record
                if finish is not None:
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await finish

        self.assertEqual(str(caught.exception), "first synthetic interruption")
        self.assertEqual(calls, 2)
        self.assertEqual(len(publications), 2)
        completed = publications[-1]["last_completed"]
        self.assertEqual(completed["outcome"], "success")
        self.assertEqual(completed["events"][-1]["summary"], "Codex turn completed")
        self.assertEqual(completed["events"][-1]["detail"], "10 tokens")
        self.assertEqual(publications[-1]["active"][0]["request_id"], "other-request")

    async def test_queued_cancellation_keeps_bounded_retry_and_observes_disk_failure(self):
        previous = self.path.read_bytes()
        with patch.object(self.store, "_write", side_effect=OSError("Synthetic disk failure")) as writer:
            async with self.store._lock:
                finish = await self.queued_finish("error")
            with self.assertLogs("jarvis_bot.progress", level="ERROR") as logs:
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await finish
        self.assertEqual(str(caught.exception), "first synthetic interruption")
        self.assertEqual(writer.call_count, 2)
        self.assertIn("Synthetic disk failure", "\n".join(logs.output))
        self.assertEqual(self.path.read_bytes(), previous)
        memory = await self.store.snapshot()
        self.assertEqual(memory["last_completed"]["outcome"], "error")
        self.assertEqual([item["request_id"] for item in memory["active"]], ["other-request"])

        await self.start("next-request")
        self.assertEqual(json.loads(self.path.read_text())["last_completed"], memory["last_completed"])


if __name__ == "__main__":
    unittest.main()
