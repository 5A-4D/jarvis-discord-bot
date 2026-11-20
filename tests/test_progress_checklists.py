"""Synthetic checklist regressions: item closure does not finish its tasks."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from jarvis_bot.bot import discord_progress_text
from jarvis_bot.progress import (
    MAX_EVENTS_PER_REQUEST,
    LiveProgressStore,
    describe_codex_event,
    render_progress,
)


def checklist(event_type="item.updated", completed=(True, False), **fields):
    return {
        "type": event_type,
        "item": {
            "id": "synthetic-checklist",
            "type": "todo_list",
            "items": [
                {"text": "synthetic private task description", "completed": done}
                for done in completed
            ],
            **fields,
        },
    }


class ChecklistDescriptionTests(unittest.TestCase):
    def test_all_lifecycle_events_report_actual_counts(self):
        for event_type in ("item.started", "item.updated", "item.completed"):
            for flags, count in (((False, False), 0), ((True, False), 1), ((True, True), 2)):
                with self.subTest(event_type=event_type, flags=flags):
                    self.assertEqual(
                        describe_codex_event(checklist(event_type, flags)),
                        (f"Task checklist: {count}/2 complete", None),
                    )

    def test_completed_item_with_unchecked_tasks_does_not_claim_completion(self):
        summary, detail = describe_codex_event(checklist("item.completed"))
        self.assertEqual(summary, "Task checklist: 1/2 complete")
        self.assertNotIn("completed", summary)
        self.assertIsNone(detail)

    def test_empty_checklist_does_not_claim_all_tasks_complete(self):
        self.assertEqual(
            describe_codex_event(checklist(completed=())),
            ("Task checklist is empty", None),
        )

    def test_malformed_checklists_do_not_invent_counts(self):
        for items in (None, {}, "invalid", [None], [{}], [{"completed": "false"}],
                      [{"completed": 1}], [{"completed": False}, {}]):
            with self.subTest(items=items):
                self.assertEqual(
                    describe_codex_event(checklist(items=items)),
                    ("Task checklist: progress unavailable", None),
                )

    def test_explicit_update_failure_keeps_failure_label_and_hides_error(self):
        self.assertEqual(
            describe_codex_event(checklist(
                "item.completed", status="failed", error="synthetic private error",
            )),
            ("Task checklist update failed", None),
        )

    def test_unrelated_item_updates_remain_ignored(self):
        for item in (None, {}, {"type": "command_execution", "command": "synthetic"},
                     {"type": "agent_message", "text": "synthetic private reply"}):
            with self.subTest(item=item):
                self.assertIsNone(describe_codex_event({"type": "item.updated", "item": item}))


class ChecklistLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "progress.json"
        self.store = LiveProgressStore(self.path)
        await self.start("request-a")

    async def start(self, request_id):
        await self.store.start(
            request_id, mode="chat", conversation_id="synthetic-scope", session_id=None,
        )

    def assert_counts_visible(self, payload, expected="1/2"):
        for text in (render_progress(payload), render_progress(payload, verbose=True),
                     discord_progress_text(payload)):
            self.assertIn(f"Task checklist: {expected} complete", text)
            self.assertNotIn("synthetic private", text)

    async def test_updates_publish_counts_without_task_text_or_finishing_request(self):
        notified = Mock()
        self.store.add_listener(notified)
        await self.store.record("request-a", checklist("item.started", (False, False)))
        await self.store.record("request-a", checklist())
        payload = await self.store.snapshot()
        active = payload["active"][0]
        self.assertEqual(active["checklist"], {"completed": 1, "total": 2})
        self.assertNotIn("outcome", active)
        self.assertIsNone(payload["last_completed"])
        self.assertEqual(notified.call_count, 2)
        self.assertEqual(json.loads(self.path.read_text())["active"], payload["active"])
        self.assertNotIn("synthetic private", self.path.read_text())
        self.assert_counts_visible(payload)

    async def test_latest_counts_survive_event_trimming_and_restart(self):
        await self.store.record("request-a", checklist())
        for _ in range(MAX_EVENTS_PER_REQUEST + 1):
            await self.store.record("request-a", {"type": "turn.started"})
        payload = await self.store.snapshot()
        self.assertFalse(any("checklist" in event["summary"]
                             for event in payload["active"][0]["events"]))
        self.assert_counts_visible(payload)

        restarted = LiveProgressStore(self.path)
        await restarted.reset()
        recovered = await restarted.snapshot()
        self.assertEqual(recovered["active"], [])
        self.assertEqual(recovered["last_completed"]["outcome"], "interrupted")
        self.assertEqual(recovered["interrupted"][0]["checklist"], {"completed": 1, "total": 2})
        self.assert_counts_visible(recovered)

    async def test_current_counts_survive_discord_truncation_of_long_details(self):
        await self.store.record("request-a", checklist())
        for _ in range(MAX_EVENTS_PER_REQUEST + 1):
            await self.store.record("request-a", {
                "type": "item.started",
                "item": {"type": "command_execution", "command": "echo " + "x" * 490},
            })
        text = discord_progress_text(await self.store.snapshot())
        self.assertIn("earlier activity omitted", text)
        self.assertIn("Task checklist: 1/2 complete", text)
        self.assertLessEqual(len(text), 2000)

    async def test_full_checklist_does_not_finish_request_and_final_outcome_is_preserved(self):
        for outcome in ("success", "error", "cancelled"):
            with self.subTest(outcome=outcome):
                await self.start("request-a")
                await self.store.record("request-a", checklist("item.completed", (True, True)))
                running = await self.store.snapshot()
                self.assertEqual(len(running["active"]), 1)
                self.assertNotIn("outcome", running["active"][0])
                await self.store.finish("request-a", outcome)
                finished = await self.store.snapshot()
                self.assertEqual(finished["active"], [])
                self.assertEqual(finished["last_completed"]["outcome"], outcome)
                self.assert_counts_visible(finished, "2/2")

    async def test_updates_are_request_local_and_cannot_resurrect_finished_work(self):
        await self.start("request-b")
        await self.store.record("request-a", checklist())
        await self.store.record("request-b", checklist(completed=(False, False, False)))
        payload = await self.store.snapshot()
        self.assertEqual(payload["active"][0]["checklist"], {"completed": 1, "total": 2})
        self.assertEqual(payload["active"][1]["checklist"], {"completed": 0, "total": 3})
        await self.store.finish("request-a", "error")
        before = await self.store.snapshot()
        await self.store.record("request-a", checklist(completed=(True, True)))
        after = await self.store.snapshot()
        self.assertEqual(before["active"], after["active"])
        self.assertEqual(before["last_completed"], after["last_completed"])

    async def test_malformed_update_invalidates_stale_counts_and_valid_update_recovers(self):
        await self.store.record("request-a", checklist())
        await self.store.record("request-a", checklist(items=[{"completed": "true"}]))
        payload = await self.store.snapshot()
        self.assertIsNone(payload["active"][0]["checklist"])
        self.assertEqual(render_progress(payload).splitlines()[-1],
                         "Task checklist: progress unavailable")
        await self.store.record("request-a", checklist(completed=(True, True)))
        self.assertEqual((await self.store.snapshot())["active"][0]["checklist"],
                         {"completed": 2, "total": 2})
        await self.store.record("request-a", checklist(completed=()))
        self.assertEqual((await self.store.snapshot())["active"][0]["checklist"],
                         {"completed": 0, "total": 0})

    async def test_failed_update_preserves_last_observed_counts(self):
        await self.store.record("request-a", checklist())
        await self.store.record("request-a", checklist(
            "item.completed", (True, True), status="failed", error="synthetic private error",
        ))
        payload = await self.store.snapshot()
        self.assertEqual(payload["active"][0]["checklist"], {"completed": 1, "total": 2})
        self.assertIn("Task checklist update failed", render_progress(payload))
        self.assertNotIn("synthetic private", self.path.read_text())

    async def test_invalid_stored_counts_render_unavailable_without_crashing(self):
        for counts in (None, [], {"completed": True, "total": 2},
                       {"completed": 3, "total": 2}, {"completed": -1, "total": 2},
                       {"completed": 0, "total": "2"}):
            with self.subTest(counts=counts):
                payload = await self.store.snapshot()
                payload["active"][0]["checklist"] = counts
                self.assertIn("Task checklist: progress unavailable", render_progress(payload))


if __name__ == "__main__":
    unittest.main()
