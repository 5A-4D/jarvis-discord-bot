"""Synthetic regressions for terminal telemetry interrupted during persistence."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_bot.telemetry import (
    JSONLTelemetryStore,
    RequestTelemetry,
    aggregate_records,
    read_records,
)


def sample_event(request_id="synthetic-request", outcome="success"):
    return RequestTelemetry(
        request_id=request_id, parent_request_id=None, chain_id=None,
        conversation_id="synthetic-scope", session_id=None, mode="chat",
        provider="openai", model="synthetic-model", model_source="configured",
        started_at="2026-11-19T01:00:00+00:00",
        completed_at="2026-11-19T01:00:01+00:00", latency_ms=1000,
        outcome=outcome, finish_reason=None, error_type=None,
        usage_source="provider_reported", input_tokens=10,
        cached_input_tokens=0, reasoning_tokens=0, output_tokens=2,
        total_tokens=12,
    )


class PausedAppend:
    """Pause the first worker while observing whether another append overlaps."""

    def __init__(self, append, *, fail=False):
        self.append = append
        self.fail = fail
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.calls = 0
        self.running = 0
        self.max_running = 0

    def __call__(self, line):
        with self.guard:
            self.calls += 1
            first = self.calls == 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if first:
                self.loop.call_soon_threadsafe(self.started.set)
                if not self.release.wait(timeout=5):
                    raise RuntimeError("Test did not release synthetic append")
                if self.fail:
                    raise OSError("Synthetic telemetry disk failure")
            self.append(line)
        finally:
            with self.guard:
                self.running -= 1
            if first:
                self.loop.call_soon_threadsafe(self.finished.set)


class TelemetryCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "telemetry.jsonl"
        self.store = JSONLTelemetryStore(self.path)

    async def test_cancelled_append_keeps_lock_and_record_order(self):
        paused = PausedAppend(self.store._append)
        with patch.object(self.store, "_append", side_effect=paused):
            first = asyncio.create_task(self.store.record(sample_event("first", "cancelled")))
            second = None
            try:
                await asyncio.wait_for(paused.started.wait(), timeout=2)
                first.cancel("first synthetic interruption")
                await asyncio.sleep(0)
                waiting = not first.done()
                locked = self.store._lock.locked()
                second = asyncio.create_task(self.store.record(sample_event("second", "error")))
                await asyncio.sleep(0)
                paused.release.set()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await first
                await second
            finally:
                paused.release.set()
                await asyncio.gather(
                    first, *([second] if second is not None else []), return_exceptions=True,
                )
                await asyncio.wait_for(paused.finished.wait(), timeout=2)

        self.assertTrue(waiting, "Cancellation escaped before the append finished")
        self.assertTrue(locked, "Cancellation released an in-flight append's lock")
        self.assertEqual(str(caught.exception), "first synthetic interruption")
        self.assertEqual(paused.max_running, 1, "Telemetry append workers overlapped")
        self.assertEqual(paused.calls, 2, "Interrupted appends must not be retried")
        records = list(read_records(self.path))
        self.assertEqual([record["request_id"] for record in records], ["first", "second"])
        self.assertEqual([record["outcome"] for record in records], ["cancelled", "error"])
        summary = aggregate_records(records)[0]
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["errors"], 2)
        self.assertEqual(summary["total_tokens"], 24)

    async def test_repeated_cancellation_preserves_first_reason_and_one_record(self):
        paused = PausedAppend(self.store._append)
        with patch.object(self.store, "_append", side_effect=paused):
            task = asyncio.create_task(self.store.record(sample_event()))
            try:
                await asyncio.wait_for(paused.started.wait(), timeout=2)
                pending = []
                for reason in ("first interruption", "second interruption", "third interruption"):
                    task.cancel(reason)
                    await asyncio.sleep(0)
                    pending.append(not task.done())
                paused.release.set()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
            finally:
                paused.release.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.wait_for(paused.finished.wait(), timeout=2)

        self.assertTrue(all(pending), "Repeated cancellation abandoned a running append")
        self.assertEqual(str(caught.exception), "first interruption")
        self.assertEqual(paused.calls, 1)
        self.assertEqual(len(list(read_records(self.path))), 1)

    async def test_cancelled_queued_records_keep_each_actual_outcome(self):
        for outcome in ("success", "error", "cancelled"):
            with self.subTest(outcome=outcome):
                entered = asyncio.Event()

                async def record():
                    entered.set()
                    await self.store.record(sample_event(outcome, outcome))

                async with self.store._lock:
                    task = asyncio.create_task(record())
                    await entered.wait()
                    task.cancel("queued interruption")
                    await asyncio.sleep(0)
                    pending = not task.done()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
                self.assertTrue(pending, "Queued cancellation discarded terminal telemetry")
                self.assertEqual(str(caught.exception), "queued interruption")

        records = list(read_records(self.path))
        self.assertEqual([record["outcome"] for record in records], ["success", "error", "cancelled"])

    async def test_cancelled_failed_append_logs_error_and_releases_lock(self):
        paused = PausedAppend(self.store._append, fail=True)
        with patch.object(self.store, "_append", side_effect=paused), self.assertLogs(
            "jarvis_bot.telemetry", level="ERROR",
        ) as logs:
            task = asyncio.create_task(self.store.record(sample_event("failed")))
            try:
                await asyncio.wait_for(paused.started.wait(), timeout=2)
                task.cancel("original interruption")
                await asyncio.sleep(0)
                paused.release.set()
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await task
            finally:
                paused.release.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.wait_for(paused.finished.wait(), timeout=2)

        self.assertEqual(str(caught.exception), "original interruption")
        self.assertIn("Synthetic telemetry disk failure", "\n".join(logs.output))
        self.assertFalse(self.store._lock.locked())
        self.assertEqual(paused.calls, 1)
        await self.store.record(sample_event("recovered"))
        self.assertEqual([record["request_id"] for record in read_records(self.path)], ["recovered"])

    async def test_normal_disk_failure_reaches_caller_without_retry(self):
        with patch.object(self.store, "_append", side_effect=OSError("Synthetic failure")) as append:
            with self.assertRaisesRegex(OSError, "Synthetic failure"):
                await self.store.record(sample_event())
        append.assert_called_once()
        self.assertFalse(self.store._lock.locked())
        await self.store.record(sample_event("next"))
        self.assertEqual([record["request_id"] for record in read_records(self.path)], ["next"])


if __name__ == "__main__":
    unittest.main()
