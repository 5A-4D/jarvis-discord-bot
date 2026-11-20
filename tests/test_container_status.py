import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from jarvis_bot.bot import JarvisDiscordClient, split_message
from jarvis_bot.container_status import (
    INSPECT_FORMAT, collect_report, docker_command, is_container_command,
    read_container_report, render_report, write_report,
)


def container(**overrides):
    result = {
        "name": "jellyfin", "id": "a" * 64, "stack": "media",
        "image": "jellyfin/jellyfin:latest", "service": "jellyfin",
        "state": "running", "health": "healthy", "restart_count": 2,
        "started_at": "2026-10-17T00:00:00Z", "finished_at": "0001-01-01T00:00:00Z",
        "exit_code": 0, "oom_killed": False,
    }
    result.update(overrides)
    return result


def report(**overrides):
    result = {
        "schema_version": 1, "checked_at": 1792195200.0,
        "docker_ok": True, "containers": [container()], "missing": [],
    }
    result.update(overrides)
    return result


class CollectorTests(unittest.TestCase):
    @patch("jarvis_bot.container_status.docker_command")
    def test_all_containers_discovery_missing_and_selected_stats(self, command):
        command.side_effect = [
            "a\nb\n",
            "\n".join(json.dumps(item) for item in [
                container(name="/jellyfin"),
                container(name="/new-service", state="exited", stack=None),
            ]),
            json.dumps({"Name": "jellyfin", "CPUPerc": "1.5%", "MemUsage": "10MiB / 1GiB", "Secret": "never publish"}),
        ]
        payload = collect_report(Path("/docker"), ["jellyfin", "deleted-service"])
        self.assertTrue(payload["docker_ok"])
        self.assertEqual(payload["missing"], ["deleted-service"])
        self.assertEqual({item["name"] for item in payload["containers"]}, {"jellyfin", "new-service"})
        jellyfin = next(item for item in payload["containers"] if item["name"] == "jellyfin")
        self.assertEqual(jellyfin["resources"]["CPUPerc"], "1.5%")
        self.assertNotIn("Secret", json.dumps(payload))
        self.assertEqual(command.call_args_list[0].args[1:], ("ps", "--all", "--quiet", "--no-trunc"))
        self.assertIn(INSPECT_FORMAT, command.call_args_list[1].args)
        self.assertNotIn(".Config.Env", INSPECT_FORMAT)
        self.assertNotIn(".State.Health.Log", INSPECT_FORMAT)

    @patch("jarvis_bot.container_status.docker_command")
    def test_daemon_failure_does_not_claim_expected_containers_missing(self, command):
        command.side_effect = RuntimeError("Docker query failed")
        payload = collect_report(Path("/docker"), ["jellyfin"])
        self.assertFalse(payload["docker_ok"])
        self.assertEqual(payload["missing"], [])
        self.assertIn("unavailable", render_report(payload))

    @patch("jarvis_bot.container_status.docker_command")
    def test_stats_failure_preserves_successful_inventory(self, command):
        command.side_effect = ["a", json.dumps(container()), RuntimeError("timeout")]
        payload = collect_report(Path("/docker"), [])
        self.assertTrue(payload["docker_ok"])
        self.assertEqual(payload["containers"][0]["name"], "jellyfin")
        self.assertIn("stats_error", payload)

    @patch("jarvis_bot.container_status.docker_command", return_value="")
    def test_empty_engine_is_distinct_from_unreachable_engine(self, command):
        payload = collect_report(Path("/docker"), [])
        self.assertTrue(payload["docker_ok"])
        self.assertEqual(command.call_count, 1)
        self.assertIn("has no containers", render_report(payload))

    @patch("jarvis_bot.container_status.subprocess.run")
    def test_subprocess_is_bounded_and_does_not_expose_stderr(self, run):
        run.side_effect = subprocess.CalledProcessError(1, "docker", stderr="TOKEN=secret")
        with self.assertRaisesRegex(RuntimeError, "daemon or access unavailable") as error:
            docker_command(Path("/docker"), "ps")
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertNotIn("shell", run.call_args.kwargs)


class RenderingTests(unittest.TestCase):
    def test_stale_and_future_reports_never_appear_current(self):
        for now in (1792195400, 1792195100):
            output = render_report(report(), now=now)
            self.assertIn("STALE REPORT", output)
            self.assertIn("current status is unknown", output)
            self.assertNotIn("🟢", output)

    def test_unconfigured_health_is_not_reported_healthy(self):
        output = render_report(report(containers=[container(health="not configured")]), now=1792195210)
        self.assertIn("🟡", output)
        self.assertIn("health: not configured", output)

    def test_unhealthy_oom_and_missing_are_visible(self):
        output = render_report(report(
            containers=[container(health="unhealthy", oom_killed=True)], missing=["vpn"]
        ), now=1792195210)
        self.assertIn("🔴 **jellyfin**", output)
        self.assertIn("out-of-memory", output)
        self.assertIn("vpn** · expected container missing", output)

    def test_discord_fields_and_long_reports_are_safe(self):
        payload = report(containers=[container(name=f"@everyone **```x``` {i}") for i in range(30)])
        output = render_report(payload, now=1792195210)
        self.assertNotIn("@everyone", output)
        self.assertNotIn("```", output)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in split_message(output)))

    def test_atomic_report_is_private_and_malformed_report_is_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            self.assertIn("not published", read_container_report(path))
            write_report(path, report())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("jellyfin", read_container_report(path))
            for payload in ([], {"checked_at": float("nan")}, report(containers=[None])):
                path.write_text(json.dumps(payload))
                self.assertIn("could not be read or validated", read_container_report(path))


class CommandAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, *, owner=False, bot=False):
        message = SimpleNamespace(
            id=10, author=SimpleNamespace(id=300, bot=bot), webhook_id=None,
            channel=SimpleNamespace(id=1), content="Jarvis containers",
            attachments=[], mentions=[], reply=AsyncMock(),
        )
        client = SimpleNamespace(
            _channel_allowed=lambda _: True, _store_discord_message=AsyncMock(),
            _is_reply_to_self=AsyncMock(return_value=False),
            _bot_access_enabled=AsyncMock(return_value=True),
            _handle_bot_request=AsyncMock(), _handle_public_request=AsyncMock(),
            user=SimpleNamespace(id=100),
            settings=SimpleNamespace(trigger="Jarvis", authorized_user_ids=frozenset({300}) if owner else frozenset()),
        )
        with patch("jarvis_bot.bot.read_container_report", return_value="Container report") as read:
            await JarvisDiscordClient.on_message(client, message)
        return client, message, read

    async def test_owner_receives_report_without_model_call(self):
        client, message, read = await self.invoke(owner=True)
        read.assert_called_once_with()
        self.assertEqual(message.reply.call_args.args[0], "Container report")
        self.assertFalse(message.reply.call_args.kwargs["allowed_mentions"].everyone)
        client._handle_public_request.assert_not_awaited()

    async def test_public_cannot_read_report(self):
        _, message, read = await self.invoke()
        read.assert_not_called()
        self.assertEqual(message.reply.call_args.args[0], "Container status is owner-only.")

    async def test_bot_with_owner_id_still_cannot_read_report(self):
        client, message, read = await self.invoke(owner=True, bot=True)
        read.assert_not_called()
        client._handle_bot_request.assert_not_awaited()
        message.reply.assert_not_awaited()

    def test_command_aliases_are_exact_and_accept_no_shell_parameters(self):
        for prompt in ("containers", "/containers", "Containers Details", "docker status"):
            self.assertTrue(is_container_command(prompt))
        for prompt in ("containers; reboot", "containers restart jellyfin", "how do containers work?"):
            self.assertFalse(is_container_command(prompt))


if __name__ == "__main__":
    unittest.main()
