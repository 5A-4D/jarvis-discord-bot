import json
import tempfile
import unittest
from pathlib import Path

from jarvis_bot.progress import (
    LiveProgressStore,
    describe_codex_event,
    render_progress,
)


class CodexProgressDescriptionTests(unittest.TestCase):
    def test_command_is_useful_but_obvious_secrets_are_redacted(self) -> None:
        described = describe_codex_event(
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "API_TOKEN=hunter2 curl --password swordfish example.test",
                },
            }
        )

        self.assertEqual(
            described,
            (
                "Running shell command",
                "API_TOKEN=<redacted> curl --password <redacted> example.test",
            ),
        )

    def test_agent_message_content_is_never_returned(self) -> None:
        described = describe_codex_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "private response text",
                },
            }
        )

        self.assertEqual(described, ("Response drafted", None))
        self.assertNotIn("private", repr(described))

    def test_reasoning_summary_is_available_as_verbose_detail(self) -> None:
        described = describe_codex_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "reasoning",
                    "text": "**Checking the restore archive**",
                },
            }
        )

        self.assertEqual(
            described,
            ("Reasoning summary", "**Checking the restore archive**"),
        )


class LiveProgressStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_is_atomic_and_renderable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live-progress.json"
            store = LiveProgressStore(path)
            await store.reset()
            await store.start(
                "message-123",
                mode="chat",
                conversation_id="guild:1:channel:2:user:3",
                session_id=None,
            )
            await store.record(
                "message-123",
                {"type": "thread.started", "thread_id": "session-456"},
            )
            await store.record(
                "message-123",
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": "printf safe",
                    },
                },
            )

            active_payload = json.loads(path.read_text(encoding="utf-8"))
            rendered = render_progress(active_payload, verbose=True)
            self.assertIn("1 active request(s)", rendered)
            self.assertIn("message-123", rendered)
            self.assertIn("session-456", rendered)
            self.assertIn("printf safe", rendered)

            await store.finish("message-123", "success")
            completed_payload = json.loads(path.read_text(encoding="utf-8"))
            rendered = render_progress(completed_payload)
            self.assertEqual(completed_payload["active"], [])
            self.assertEqual(
                completed_payload["last_completed"]["outcome"], "success"
            )
            self.assertIn("No active request", rendered)

    async def test_default_render_hides_command_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live-progress.json"
            store = LiveProgressStore(path)
            await store.start(
                "request-1",
                mode="chat",
                conversation_id="scope",
                session_id=None,
            )
            await store.record(
                "request-1",
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": "printf visible-only-with-verbose",
                    },
                },
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertNotIn("visible-only-with-verbose", render_progress(payload))
            self.assertIn(
                "visible-only-with-verbose",
                render_progress(payload, verbose=True),
            )

    async def test_reasoning_summary_is_only_rendered_in_verbose_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live-progress.json"
            store = LiveProgressStore(path)
            await store.start(
                "request-1",
                mode="chat",
                conversation_id="scope",
                session_id=None,
            )
            await store.record(
                "request-1",
                {
                    "type": "item.completed",
                    "item": {
                        "type": "reasoning",
                        "text": "Inspecting the event stream",
                    },
                },
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertNotIn("Inspecting the event stream", render_progress(payload))
            self.assertIn(
                "Inspecting the event stream",
                render_progress(payload, verbose=True),
            )

    async def test_listener_and_snapshot_follow_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LiveProgressStore(Path(directory) / "live-progress.json")
            notifications = 0

            def notified() -> None:
                nonlocal notifications
                notifications += 1

            store.add_listener(notified)
            await store.start(
                "request-1", mode="chat", conversation_id="scope", session_id=None
            )
            snapshot = await store.snapshot()

            self.assertEqual(notifications, 1)
            self.assertEqual(snapshot["active"][0]["request_id"], "request-1")

            snapshot["active"].clear()
            unchanged = await store.snapshot()
            self.assertEqual(len(unchanged["active"]), 1)
