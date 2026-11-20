import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from jarvis_bot.bot import JarvisDiscordClient


class ProgressRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def client(self, channel):
        changed = asyncio.Event()
        changed.set()
        return SimpleNamespace(
            _progress_changed=changed,
            progress=SimpleNamespace(snapshot=AsyncMock(return_value={"active": [{}]})),
            _progress_channel=AsyncMock(return_value=channel),
        )

    async def run_until(self, client, action, completed):
        with patch("jarvis_bot.bot.PROGRESS_RETRY_SECONDS", 0), patch(
            "jarvis_bot.bot.PROGRESS_REFRESH_SECONDS", 0
        ), patch("jarvis_bot.bot.discord_progress_text", return_value="progress"):
            task = asyncio.create_task(action(client))
            try:
                await asyncio.wait_for(completed.wait(), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_send_network_failure_retries_without_new_events(self):
        completed = asyncio.Event()
        message = SimpleNamespace(edit=AsyncMock())
        calls = 0

        async def send(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise aiohttp.ClientConnectionError("DNS unavailable")
            completed.set()
            return message

        channel = SimpleNamespace(send=AsyncMock(side_effect=send))
        await self.run_until(self.client(channel), JarvisDiscordClient._mirror_live_progress, completed)
        self.assertEqual(calls, 2)

    async def test_failed_completion_edit_is_retried_without_new_events(self):
        completed = asyncio.Event()
        client = self.client(None)
        client.progress.snapshot.side_effect = [{"active": [{}]}] + [{"active": []}] * 5

        async def edit(**kwargs):
            raise aiohttp.ClientConnectionError("connection reset")

        message = SimpleNamespace(edit=AsyncMock(side_effect=edit))

        async def send(*args, **kwargs):
            if channel.send.await_count == 1:
                client._progress_changed.set()
            else:
                completed.set()
            return message

        channel = SimpleNamespace(send=AsyncMock(side_effect=send))
        client._progress_channel.return_value = channel
        with patch("jarvis_bot.bot.discord_progress_text", side_effect=lambda p: str(p)):
            # run_until's constant renderer would suppress the completion edit.
            with patch("jarvis_bot.bot.PROGRESS_RETRY_SECONDS", 0), patch(
                "jarvis_bot.bot.PROGRESS_REFRESH_SECONDS", 0
            ):
                task = asyncio.create_task(JarvisDiscordClient._mirror_live_progress(client))
                try:
                    await asyncio.wait_for(completed.wait(), 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(channel.send.await_count, 2)
        message.edit.assert_awaited_once()

    async def test_missing_channel_retries(self):
        completed = asyncio.Event()

        async def send(*args, **kwargs):
            completed.set()
            return SimpleNamespace(edit=AsyncMock())

        channel = SimpleNamespace(send=AsyncMock(side_effect=send))
        client = self.client(channel)
        client._progress_channel.side_effect = [None, channel]
        await self.run_until(client, JarvisDiscordClient._mirror_live_progress, completed)
        self.assertEqual(client._progress_channel.await_count, 2)

    async def test_supervisor_recovers_unexpected_failure(self):
        completed = asyncio.Event()
        calls = 0

        async def mirror():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("unexpected worker failure")
            completed.set()
            await asyncio.Event().wait()

        client = SimpleNamespace(
            _mirror_live_progress=mirror, _progress_changed=asyncio.Event()
        )
        await self.run_until(client, JarvisDiscordClient._progress_worker, completed)
        self.assertEqual(calls, 2)

    async def test_reconnect_replaces_finished_worker_without_duplicates(self):
        old = asyncio.create_task(asyncio.sleep(0))
        await old
        client = SimpleNamespace(
            settings=SimpleNamespace(progress_channel_id=123), progress=object(),
            _progress_task=old, _progress_changed=asyncio.Event(),
            _progress_worker=AsyncMock(side_effect=lambda: None),
        )
        JarvisDiscordClient._ensure_progress_task(client)
        new = client._progress_task
        self.assertIsNot(new, old)
        JarvisDiscordClient._ensure_progress_task(client)
        self.assertIs(client._progress_task, new)
        await new

    async def test_cancelled_worker_is_not_retried(self):
        client = SimpleNamespace(
            _mirror_live_progress=AsyncMock(side_effect=asyncio.CancelledError),
            _progress_changed=asyncio.Event(),
        )
        with self.assertRaises(asyncio.CancelledError):
            await JarvisDiscordClient._progress_worker(client)
        client._mirror_live_progress.assert_awaited_once()

    async def test_resume_wakes_progress_worker(self):
        from unittest.mock import Mock
        client = SimpleNamespace(_record_heartbeat=Mock(), _ensure_progress_task=Mock())
        await JarvisDiscordClient.on_resumed(client)
        client._ensure_progress_task.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
