"""A rejected initial publication must not leave an unfinishable active request."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from jarvis_bot.assistant import CodexCLIError, JarvisAssistant, ParsedCodexEvents
from jarvis_bot.memory import MemoryStore
from jarvis_bot.progress import LiveProgressStore


class ProgressStartFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "progress.json"
        self.store = LiveProgressStore(self.path)

    async def start(self, request_id):
        await self.store.start(
            request_id,
            mode="chat",
            conversation_id="synthetic-scope",
            session_id=None,
        )

    async def reject_start(self, request_id):
        # Fail at the atomic commit, after the real temporary file was written.
        with patch("jarvis_bot.progress.os.replace", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaisesRegex(OSError, "Synthetic disk failure"):
                await self.start(request_id)

    async def test_first_failed_start_is_absent_from_memory_and_later_events(self):
        notified = Mock()
        self.store.add_listener(notified)
        await self.reject_start("rejected")

        self.assertEqual((await self.store.snapshot())["active"], [])
        self.assertFalse(self.path.exists())
        await self.store.record("rejected", {"type": "turn.started"})
        self.assertFalse(self.path.exists(), "A later event resurrected a rejected request")
        notified.assert_not_called()

        await self.start("next")
        await self.store.finish("next", "success")
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["active"], [])
        self.assertEqual(payload["last_completed"]["request_id"], "next")
        self.assertEqual(payload["last_completed"]["outcome"], "success")

    async def test_failed_start_preserves_other_active_and_completed_requests(self):
        await self.start("completed")
        await self.store.finish("completed", "error")
        await self.start("running")
        before = await self.store.snapshot()
        published = self.path.read_bytes()
        notified = Mock()
        self.store.add_listener(notified)

        await self.reject_start("rejected")

        after = await self.store.snapshot()
        self.assertEqual(after["active"], before["active"])
        self.assertEqual(after["last_completed"], before["last_completed"])
        self.assertEqual(self.path.read_bytes(), published)
        notified.assert_not_called()
        await self.store.record("rejected", {"type": "turn.completed"})
        await self.store.finish("running", "success")
        self.assertEqual(json.loads(self.path.read_text())["active"], [])

    async def test_failed_replacement_restores_existing_request_and_its_events(self):
        await self.start("same-id")
        await self.store.record("same-id", {"type": "thread.started", "thread_id": "synthetic-session"})
        before = await self.store.snapshot()
        published = self.path.read_bytes()

        await self.reject_start("same-id")

        self.assertEqual((await self.store.snapshot())["active"], before["active"])
        self.assertEqual(self.path.read_bytes(), published)
        await self.store.finish("same-id", "cancelled")
        last = json.loads(self.path.read_text())["last_completed"]
        self.assertEqual(last["session_id"], "synthetic-session")
        self.assertEqual(last["events"], before["active"][0]["events"])

    async def test_assistant_continues_after_rejected_progress_without_ghost_request(self):
        result = ParsedCodexEvents(
            answer="Synthetic answer", session_id=None, error=None, model=None,
            finish_reason="stop", input_tokens=None, cached_input_tokens=None,
            reasoning_tokens=None, output_tokens=None,
        )
        for error in (None, CodexCLIError("Synthetic request failure"), asyncio.CancelledError()):
            with self.subTest(outcome=type(error).__name__):
                memory = AsyncMock(spec=MemoryStore)
                memory.get_session_id.return_value = None
                assistant = JarvisAssistant(
                    codex_binary=Path("/synthetic/codex"), workdir=self.root,
                    sandbox="read-only", timeout_seconds=10, persona="Synthetic",
                    memory=memory, progress=self.store,
                )

                async def run(*args, **kwargs):
                    await self.store.record(kwargs["progress_request_id"], {"type": "turn.started"})
                    if error is not None:
                        raise error
                    return result

                assistant._run = AsyncMock(side_effect=run)
                replace = os.replace
                attempted = False

                def fail_once(source, destination):
                    nonlocal attempted
                    if not attempted:
                        attempted = True
                        raise OSError("Synthetic initial progress failure")
                    return replace(source, destination)

                with patch("jarvis_bot.progress.os.replace", side_effect=fail_once), self.assertLogs(
                    "jarvis_bot.assistant", level="WARNING"
                ):
                    if error is None:
                        answer = await assistant.respond("synthetic-scope", 123, "Synthetic prompt", request_id="rejected")
                        self.assertEqual(answer, result.answer)
                    else:
                        with self.assertRaises(type(error)):
                            await assistant.respond("synthetic-scope", 123, "Synthetic prompt", request_id="rejected")

                assistant._run.assert_awaited_once()
                self.assertTrue(attempted)
                self.assertEqual((await self.store.snapshot())["active"], [])

                assistant._run = AsyncMock(return_value=result)
                await assistant.respond("synthetic-scope", 123, "Retry", request_id="recovered")
                payload = json.loads(self.path.read_text())
                self.assertEqual(payload["active"], [])
                self.assertEqual(payload["last_completed"]["request_id"], "recovered")
                self.assertEqual(payload["last_completed"]["outcome"], "success")
