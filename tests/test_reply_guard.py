import asyncio
import fcntl
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis_bot.bot import JarvisDiscordClient
from jarvis_bot.reply_guard import ReplyGuard


class ReplyGuardTests(unittest.IsolatedAsyncioTestCase):
    def exclusive_is_available(self, path):
        with path.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

    async def test_restart_waits_for_all_concurrent_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'replies.lock'
            guard = ReplyGuard(path)
            async with guard.hold():
                self.assertFalse(self.exclusive_is_available(path))
                async with guard.hold():
                    self.assertFalse(self.exclusive_is_available(path))
                self.assertFalse(self.exclusive_is_available(path))
            self.assertTrue(self.exclusive_is_available(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_exception_and_cancellation_release_the_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'replies.lock'
            guard = ReplyGuard(path)
            with self.assertRaises(RuntimeError):
                async with guard.hold():
                    raise RuntimeError('Discord failed')
            self.assertTrue(self.exclusive_is_available(path))
            started = asyncio.Event()

            async def request():
                async with guard.hold():
                    started.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(request())
            await started.wait()
            self.assertFalse(self.exclusive_is_available(path))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.exclusive_is_available(path))

    async def test_on_message_protects_final_delivery_after_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'replies.lock'
            client = SimpleNamespace(_reply_guard=ReplyGuard(path))
            generated, deliver = asyncio.Event(), asyncio.Event()

            async def dispatch(*args):
                generated.set()  # CLI has exited; Discord delivery is still pending.
                await deliver.wait()

            with patch.object(JarvisDiscordClient, '_dispatch_message', side_effect=dispatch):
                task = asyncio.create_task(JarvisDiscordClient.on_message(client, object()))
                await generated.wait()
                self.assertFalse(self.exclusive_is_available(path))
                deliver.set()
                await task
            self.assertTrue(self.exclusive_is_available(path))

    async def test_existing_exclusive_restart_lock_delays_a_new_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'replies.lock'
            started = asyncio.Event()
            with path.open('a') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

                async def request():
                    async with ReplyGuard(path).hold():
                        started.set()

                task = asyncio.create_task(request())
                await asyncio.sleep(0.02)
                self.assertFalse(started.is_set())
                fcntl.flock(stream, fcntl.LOCK_UN)
                await asyncio.wait_for(task, 1)
            self.assertTrue(started.is_set())


class RestartProcessCheckTests(unittest.TestCase):
    def run_check(self, table, *, inspect_error=0, top_error=0):
        source = (Path(__file__).resolve().parents[1] / 'scripts/restart-after-reply').read_text()
        function = 'codex_is_active() {' + source.split('codex_is_active() {', 1)[1].split('\n}\n', 1)[0] + '\n}'
        harness = '''
fake_docker() {
    if [[ "$1" == inspect ]]; then
        [[ "$MOCK_INSPECT_ERROR" == 0 ]] || return 1
        printf 'true\n'
    elif [[ "$1" == top ]]; then
        [[ "$*" == 'top jarvis-discord -eo pid,comm' ]] || return 2
        [[ "$MOCK_TOP_ERROR" == 0 ]] || return 1
        printf '%s\n' "$MOCK_PS"
    else
        return 3
    fi
}
docker_cli=fake_docker
container_name=jarvis-discord
'''
        result = subprocess.run(
            ['bash', '-c', harness + function + '\nif codex_is_active; then printf active; else printf idle; fi'],
            env={**os.environ, 'MOCK_PS': table, 'MOCK_INSPECT_ERROR': str(inspect_error),
                 'MOCK_TOP_ERROR': str(top_error)},
            text=True, capture_output=True, check=True,
        )
        return result.stdout

    def test_active_codex_and_code_mode_workers_block_restart(self):
        for name in ('codex', 'codex-code-mode'):
            self.assertEqual(self.run_check(f'PID COMMAND\n 100 python\n  200 {name}'), 'active')

    def test_idle_python_process_does_not_block_restart(self):
        self.assertEqual(self.run_check('PID COMMAND\n100 python'), 'idle')

    def test_inspection_errors_and_malformed_output_block_restart(self):
        for kwargs in ({'inspect_error': 1}, {'top_error': 1}, {}):
            self.assertEqual(self.run_check('', **kwargs), 'active')
