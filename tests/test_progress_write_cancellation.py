"""Regression coverage for cancellation while progress is being written."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_bot.progress import LiveProgressStore


class PausedWrite:
    """Pause the first real file write without relying on disk timing."""

    def __init__(self, writer, *, fail=False):
        self.writer = writer
        self.fail = fail
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.calls = 0
        self.running = 0
        self.max_running = 0

    def __call__(self):
        with self.guard:
            self.calls += 1
            first = self.calls == 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if first:
                self.loop.call_soon_threadsafe(self.started.set)
                if not self.release.wait(timeout=5):
                    raise RuntimeError("Test did not release paused progress write")
                if self.fail:
                    raise OSError("Synthetic progress disk failure")
            self.writer()
        finally:
            with self.guard:
                self.running -= 1
            if first:
                self.loop.call_soon_threadsafe(self.finished.set)


class ProgressWriteCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def start_request(self, store, request_id="request-1"):
        await store.start(
            request_id, mode="chat", conversation_id="synthetic-scope", session_id=None
        )

    async def check_cancelled_write(self, operation):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            store = LiveProgressStore(path)
            await self.start_request(store)
            publications = []
            store.add_listener(lambda: publications.append(json.loads(path.read_text())))
            paused = PausedWrite(store._write)

            if operation == "start":
                update = self.start_request(store, "request-2")
            elif operation == "record":
                update = store.record("request-1", {"type": "turn.started"})
            elif operation == "finish":
                update = store.finish("request-1", "cancelled")
            else:
                update = store.reset()

            async def next_request():
                # This must wait for the interrupted writer before publishing.
                await store.finish("request-1", "cancelled")
                await store.finish("request-2", "error")
                await self.start_request(store, "request-final")
                await store.finish("request-final", "error")

            with patch.object(store, "_write", paused):
                task = asyncio.create_task(update)
                following = None
                try:
                    await asyncio.wait_for(paused.started.wait(), timeout=2)
                    task.cancel("request interrupted")
                    await asyncio.sleep(0)  # Deliver cancellation to the writer's await.
                    following = asyncio.create_task(next_request())
                    await asyncio.sleep(0)  # Let the following request try the lock.
                    still_writing = not task.done()
                    next_request_waits = not following.done()
                finally:
                    paused.release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await asyncio.wait_for(paused.finished.wait(), timeout=2)
                    if following is not None:
                        await following

            self.assertTrue(still_writing, "Cancellation released an unfinished writer")
            self.assertTrue(next_request_waits)
            self.assertEqual(paused.max_running, 1, "Progress writers overlapped")
            self.assertEqual(len(publications), 5)
            payload = json.loads(path.read_text())
            self.assertEqual(payload, publications[-1])
            self.assertEqual(payload["active"], [])
            self.assertEqual(payload["last_completed"]["request_id"], "request-final")
            self.assertEqual(payload["last_completed"]["outcome"], "error")
            self.assertEqual((await store.snapshot())["last_completed"], payload["last_completed"])

    async def test_cancelled_start_cannot_race_with_completion(self):
        await self.check_cancelled_write("start")

    async def test_cancelled_event_cannot_race_with_completion(self):
        await self.check_cancelled_write("record")

    async def test_cancelled_finish_cannot_race_with_next_request(self):
        await self.check_cancelled_write("finish")

    async def test_cancelled_reset_cannot_race_with_next_request(self):
        await self.check_cancelled_write("reset")

    async def test_repeated_cancellation_waits_and_notifies_viewer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            store = LiveProgressStore(path)
            await self.start_request(store)
            publications = []
            store.add_listener(lambda: publications.append(json.loads(path.read_text())))
            paused = PausedWrite(store._write)
            with patch.object(store, "_write", paused):
                task = asyncio.create_task(store.finish("request-1", "cancelled"))
                try:
                    await asyncio.wait_for(paused.started.wait(), timeout=2)
                    for _ in range(3):
                        task.cancel("request interrupted")
                        await asyncio.sleep(0)
                    still_writing = not task.done()
                    self.assertEqual(publications, [])
                finally:
                    paused.release.set()
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    await asyncio.wait_for(paused.finished.wait(), timeout=2)
            self.assertTrue(still_writing)
            self.assertEqual(str(caught.exception), "request interrupted")
            self.assertEqual(len(publications), 1)
            self.assertEqual(publications[0]["active"], [])
            self.assertEqual(publications[0]["last_completed"]["outcome"], "cancelled")

    async def test_failed_interrupted_write_preserves_cancellation_and_allows_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            store = LiveProgressStore(path)
            await self.start_request(store)
            publications = []
            store.add_listener(lambda: publications.append(json.loads(path.read_text())))
            paused = PausedWrite(store._write, fail=True)
            with patch.object(store, "_write", paused), self.assertLogs(
                "jarvis_bot.progress", level="ERROR"
            ) as logs:
                task = asyncio.create_task(store.record("request-1", {"type": "turn.started"}))
                try:
                    await asyncio.wait_for(paused.started.wait(), timeout=2)
                    task.cancel("request interrupted")
                    await asyncio.sleep(0)
                finally:
                    paused.release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await asyncio.wait_for(paused.finished.wait(), timeout=2)
            self.assertIn("Synthetic progress disk failure", "\n".join(logs.output))
            self.assertEqual(publications, [])
            await store.finish("request-1", "cancelled")
            self.assertEqual(publications[-1]["last_completed"]["outcome"], "cancelled")

    async def test_ordinary_write_failure_still_reaches_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LiveProgressStore(Path(directory) / "progress.json")
            await self.start_request(store)
            with patch.object(store, "_write", side_effect=OSError("Synthetic failure")):
                with self.assertRaisesRegex(OSError, "Synthetic failure"):
                    await store.finish("request-1", "error")
            await self.start_request(store, "request-2")
            self.assertEqual((await store.snapshot())["active"][0]["request_id"], "request-2")
