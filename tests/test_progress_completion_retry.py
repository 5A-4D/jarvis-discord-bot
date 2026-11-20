"""Synthetic regressions for a transient failure publishing the final outcome."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from jarvis_bot.assistant import CodexCLIError, JarvisAssistant, ParsedCodexEvents
from jarvis_bot.memory import MemoryStore
from jarvis_bot.progress import LiveProgressStore, render_progress


class ProgressCompletionRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "progress.json"
        self.store = LiveProgressStore(self.path)

    async def start(self, request_id="synthetic-request"):
        await self.store.start(
            request_id, mode="chat", conversation_id="synthetic-scope", session_id=None,
        )

    def failing_commit(self, *, always=False):
        original = os.replace
        previous = self.path.read_bytes()
        attempts = []

        def replace(source, destination):
            attempts.append(json.loads(Path(source).read_text()))
            if always or len(attempts) == 1:
                # Fail after writing the real temporary snapshot. The last
                # published state must stay intact until a commit succeeds.
                self.assertEqual(self.path.read_bytes(), previous)
                raise OSError("Synthetic completion commit failure")
            original(source, destination)

        return replace, attempts

    async def test_each_terminal_outcome_is_persisted_after_one_failed_commit(self):
        publications = []
        self.store.add_listener(lambda: publications.append(json.loads(self.path.read_text())))
        for outcome in ("success", "error", "cancelled"):
            with self.subTest(outcome=outcome):
                await self.start()
                await self.store.record("synthetic-request", {
                    "type": "thread.started", "thread_id": "synthetic-session",
                })
                before = (await self.store.snapshot())["active"][0]
                publications.clear()
                replace, attempts = self.failing_commit()
                with patch("jarvis_bot.progress.os.replace", side_effect=replace):
                    await self.store.finish("synthetic-request", outcome)

                self.assertEqual(len(attempts), 2)
                self.assertEqual(len(publications), 1)
                payload = json.loads(self.path.read_text())
                self.assertEqual(publications[0], payload)
                self.assertEqual(payload["active"], [])
                completed = payload["last_completed"]
                self.assertEqual(completed["outcome"], outcome)
                self.assertEqual(completed["session_id"], "synthetic-session")
                self.assertEqual(completed["events"], before["events"])
                self.assertEqual(attempts[0]["last_completed"], completed)
                self.assertEqual(attempts[1]["last_completed"], completed)
                self.assertIn(outcome, render_progress(payload))

                restarted = LiveProgressStore(self.path)
                await restarted.reset()
                recovered = await restarted.snapshot()
                self.assertEqual(recovered["last_completed"], completed)
                self.assertEqual(recovered["interrupted"], [])

    async def test_retry_preserves_other_active_requests(self):
        await self.start()
        await self.start("other-request")
        other = (await self.store.snapshot())["active"][1]
        replace, attempts = self.failing_commit()
        with patch("jarvis_bot.progress.os.replace", side_effect=replace):
            await self.store.finish("synthetic-request", "error")
        payload = json.loads(self.path.read_text())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(payload["active"], [other])
        self.assertEqual(payload["last_completed"]["outcome"], "error")

    async def test_persistent_failure_is_bounded_and_propagates(self):
        await self.start()
        previous = self.path.read_bytes()
        notified = Mock()
        self.store.add_listener(notified)
        replace, attempts = self.failing_commit(always=True)
        with patch("jarvis_bot.progress.os.replace", side_effect=replace):
            with self.assertRaisesRegex(OSError, "Synthetic completion commit failure"):
                await self.store.finish("synthetic-request", "success")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.path.read_bytes(), previous)
        notified.assert_not_called()
        memory = await self.store.snapshot()
        self.assertEqual(memory["active"], [])
        self.assertEqual(memory["last_completed"]["outcome"], "success")

        await self.start("next-request")
        recovered = json.loads(self.path.read_text())
        self.assertEqual(recovered["active"][0]["request_id"], "next-request")
        self.assertEqual(recovered["last_completed"], memory["last_completed"])

    async def test_successful_finish_does_not_write_twice(self):
        await self.start()
        with patch.object(self.store, "_write", wraps=self.store._write) as writer:
            await self.store.finish("synthetic-request", "success")
        writer.assert_called_once_with()

    async def test_non_io_error_is_not_retried(self):
        await self.start()
        with patch.object(self.store, "_write", side_effect=ValueError("Synthetic bug")) as writer:
            with self.assertRaisesRegex(ValueError, "Synthetic bug"):
                await self.store.finish("synthetic-request", "error")
        writer.assert_called_once_with()

    async def test_cancelled_retry_keeps_lock_until_writer_finishes(self):
        await self.start()
        loop = asyncio.get_running_loop()
        retry_started = asyncio.Event()
        release = threading.Event()
        write = self.store._write
        calls = 0
        publications = []
        self.store.add_listener(lambda: publications.append(json.loads(self.path.read_text())))

        def paused_retry():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("Synthetic first write failure")
            if calls == 2:
                loop.call_soon_threadsafe(retry_started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release completion retry")
            write()

        with patch.object(self.store, "_write", side_effect=paused_retry):
            finish = asyncio.create_task(self.store.finish("synthetic-request", "cancelled"))
            following = None
            try:
                await asyncio.wait_for(retry_started.wait(), timeout=2)
                finish.cancel("synthetic interruption")
                await asyncio.sleep(0)
                finish.cancel("second interruption")
                await asyncio.sleep(0)
                following = asyncio.create_task(self.start("next-request"))
                await asyncio.sleep(0)
                self.assertFalse(finish.done())
                self.assertFalse(following.done())
                self.assertEqual(calls, 2)
                self.assertEqual(publications, [])
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await finish
                if following is not None:
                    await following
        self.assertEqual(str(caught.exception), "synthetic interruption")
        self.assertEqual(calls, 3)
        self.assertEqual(len(publications), 2)
        self.assertEqual(publications[0]["active"], [])
        self.assertEqual(publications[0]["last_completed"]["outcome"], "cancelled")
        self.assertEqual(publications[1]["active"][0]["request_id"], "next-request")

    async def test_assistant_keeps_its_result_when_final_commit_needs_retry(self):
        result = ParsedCodexEvents(
            answer="Synthetic answer", session_id=None, error=None, model=None,
            finish_reason="stop", input_tokens=None, cached_input_tokens=None,
            reasoning_tokens=None, output_tokens=None,
        )
        for error, outcome in (
            (None, "success"),
            (CodexCLIError("Synthetic request failure"), "error"),
            (asyncio.CancelledError("Synthetic cancellation"), "cancelled"),
        ):
            with self.subTest(outcome=outcome):
                memory = AsyncMock(spec=MemoryStore)
                memory.get_session_id.return_value = None
                assistant = JarvisAssistant(
                    codex_binary=Path("/synthetic/codex"), workdir=self.root,
                    sandbox="read-only", timeout_seconds=10, persona="Synthetic",
                    memory=memory, progress=self.store,
                )
                assistant._run = AsyncMock(return_value=result, side_effect=error)
                replace = os.replace
                completion_attempts = 0

                def fail_first_completion(source, destination):
                    nonlocal completion_attempts
                    payload = json.loads(Path(source).read_text())
                    if not payload["active"]:
                        completion_attempts += 1
                        if completion_attempts == 1:
                            raise OSError("Synthetic final commit failure")
                    replace(source, destination)

                with patch("jarvis_bot.progress.os.replace", side_effect=fail_first_completion):
                    if error is None:
                        answer = await assistant.respond(
                            "synthetic-scope", 123, "Synthetic prompt", request_id="synthetic-request",
                        )
                        self.assertEqual(answer, "Synthetic answer")
                    else:
                        with self.assertRaises(type(error)) as caught:
                            await assistant.respond(
                                "synthetic-scope", 123, "Synthetic prompt", request_id="synthetic-request",
                            )
                        self.assertIs(caught.exception, error)
                self.assertEqual(completion_attempts, 2)
                payload = json.loads(self.path.read_text())
                self.assertEqual(payload["active"], [])
                self.assertEqual(payload["last_completed"]["outcome"], outcome)


if __name__ == "__main__":
    unittest.main()
