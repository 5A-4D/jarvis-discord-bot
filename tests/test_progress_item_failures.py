"""Synthetic regressions for failed work items in live request progress."""

import json
import tempfile
import unittest
from pathlib import Path

from jarvis_bot.bot import discord_progress_text
from jarvis_bot.progress import LiveProgressStore, describe_codex_event, render_progress


def completed(item_type, **fields):
    return {"type": "item.completed", "item": {"type": item_type, **fields}}


class FailedItemDescriptionTests(unittest.TestCase):
    def test_nonzero_shell_exit_is_failure_even_with_completed_status(self):
        for exit_code in (1, 127, -15):
            with self.subTest(exit_code=exit_code):
                self.assertEqual(
                    describe_codex_event(completed(
                        "command_execution", command="synthetic-command",
                        status="completed", exit_code=exit_code,
                    )),
                    (f"Shell command failed (exit {exit_code})", "synthetic-command"),
                )

    def test_explicit_shell_failure_does_not_require_nonzero_exit(self):
        for fields in ({}, {"exit_code": None}, {"exit_code": 0}):
            with self.subTest(fields=fields):
                summary, _ = describe_codex_event(completed(
                    "command_execution", status="failed", **fields,
                ))
                self.assertEqual(
                    summary, "Shell command failed" + (" (exit 0)" if fields.get("exit_code") == 0 else ""),
                )

    def test_failed_tool_status_is_visible_without_private_error_content(self):
        for item_type in ("mcp_tool_call", "tool_call"):
            with self.subTest(item_type=item_type):
                event = completed(
                    item_type, status="failed", tool="synthetic_lookup",
                    error={"message": "synthetic private error"},
                    result={"content": [{"text": "synthetic private result"}]},
                )
                self.assertEqual(
                    describe_codex_event(event), ("Tool call failed", "synthetic_lookup"),
                )

    def test_other_explicitly_failed_items_are_not_labelled_completed(self):
        for item_type, expected in (
            ("file_change", "File Change failed"),
            ("web_search", "Web search failed"),
            ("web_search_call", "Web search failed"),
            ("agent_message", "Response drafting failed"),
            ("reasoning", "Reasoning failed"),
            ("custom_work", "Custom Work failed"),
        ):
            with self.subTest(item_type=item_type):
                self.assertEqual(
                    describe_codex_event(completed(
                        item_type, status="failed", text="synthetic private text",
                    )),
                    (expected, None),
                )

    def test_successful_and_legacy_events_keep_existing_wording(self):
        for status in (None, "completed"):
            for item_type, fields, expected in (
                ("command_execution", {"exit_code": 0}, ("Shell command completed (exit 0)", None)),
                ("command_execution", {}, ("Shell command completed", None)),
                ("mcp_tool_call", {"tool": "lookup"}, ("Tool call completed", "lookup")),
                ("tool_call", {"name": "lookup"}, ("Tool call completed", "lookup")),
                ("file_change", {}, ("File Change completed", None)),
                ("web_search", {}, ("Web search completed", None)),
                ("agent_message", {"text": "private reply"}, ("Response drafted", None)),
                ("reasoning", {"text": "Synthetic summary"}, ("Reasoning summary", "Synthetic summary")),
            ):
                with self.subTest(status=status, item_type=item_type):
                    self.assertEqual(
                        describe_codex_event(completed(item_type, status=status, **fields)), expected,
                    )

    def test_non_integer_exit_values_are_not_invented_exit_codes(self):
        for exit_code in (True, False, "1", None):
            with self.subTest(exit_code=exit_code):
                self.assertEqual(
                    describe_codex_event(completed("command_execution", exit_code=exit_code)),
                    ("Shell command completed", None),
                )

    def test_start_events_still_describe_running_work(self):
        for item_type, fields, expected in (
            ("command_execution", {"exit_code": 1}, "Running shell command"),
            ("mcp_tool_call", {}, "Calling tool"),
            ("web_search", {}, "Searching the web"),
        ):
            with self.subTest(item_type=item_type):
                event = completed(item_type, status="failed", **fields)
                event["type"] = "item.started"
                self.assertEqual(describe_codex_event(event), (expected, None))


class FailedItemLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.json"
        self.store = LiveProgressStore(self.path)
        await self.store.start(
            "synthetic-request", mode="chat", conversation_id="synthetic-scope", session_id=None,
        )

    async def record_failures(self):
        await self.store.record("synthetic-request", completed(
            "command_execution", status="completed", exit_code=1,
            command="API_TOKEN=synthetic-secret synthetic-command",
            aggregated_output="synthetic private command output",
        ))
        await self.store.record("synthetic-request", completed(
            "mcp_tool_call", status="failed", tool="synthetic_lookup",
            error={"message": "synthetic private tool error"},
            result={"content": [{"text": "synthetic private tool result"}]},
        ))

    def assert_visible_failures(self, payload):
        for rendered in (render_progress(payload), render_progress(payload, verbose=True), discord_progress_text(payload)):
            self.assertIn("Shell command failed (exit 1)", rendered)
            self.assertIn("Tool call failed", rendered)
            self.assertNotIn("synthetic-secret", rendered)
            self.assertNotIn("synthetic private", rendered)
        self.assertIn("API_TOKEN=<redacted>", render_progress(payload, verbose=True))
        self.assertNotIn("synthetic-command", render_progress(payload))

    async def test_failures_are_published_without_prematurely_finishing_request(self):
        publications = []
        self.store.add_listener(lambda: publications.append(json.loads(self.path.read_text())))
        await self.record_failures()
        payload = await self.store.snapshot()
        self.assertEqual(len(publications), 2)
        self.assertEqual(publications[-1]["active"], payload["active"])
        self.assertEqual(len(payload["active"]), 1)
        self.assertNotIn("outcome", payload["active"][0])
        self.assertIsNone(payload["last_completed"])
        self.assert_visible_failures(payload)
        self.assertNotIn("synthetic-secret", self.path.read_text())
        self.assertNotIn("synthetic private", self.path.read_text())

        # A failed command/tool may be recovered by the agent. Only finish()
        # determines the outcome of the enclosing request.
        await self.store.finish("synthetic-request", "success")
        finished = json.loads(self.path.read_text())
        self.assertEqual(finished["active"], [])
        self.assertEqual(finished["last_completed"]["outcome"], "success")
        self.assert_visible_failures(finished)

    async def test_failure_history_survives_interruption_recovery(self):
        await self.record_failures()
        restarted = LiveProgressStore(self.path)
        await restarted.reset()
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["active"], [])
        self.assertEqual(payload["last_completed"]["outcome"], "interrupted")
        self.assertEqual(payload["interrupted"], [payload["last_completed"]])
        self.assert_visible_failures(payload)


if __name__ == "__main__":
    unittest.main()
