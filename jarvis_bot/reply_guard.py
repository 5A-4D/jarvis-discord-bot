"""Keep a queued restart outside the lifetime of a Discord request and its reply."""
from __future__ import annotations

import asyncio
import fcntl
import os
from contextlib import asynccontextmanager
from pathlib import Path


class ReplyGuard:
    def __init__(self, path: Path):
        self.path = path

    @asynccontextmanager
    async def hold(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.1)
            yield
        finally:
            os.close(descriptor)
