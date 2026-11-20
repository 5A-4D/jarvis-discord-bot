from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def write_heartbeat(
    path: Path,
    *,
    ready: bool,
    timestamp: float | None = None,
    process_id: int | None = None,
) -> None:
    """Atomically publish the bot's process and Discord readiness state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid() if process_id is None else process_id,
        "ready": ready,
        "timestamp": time.time() if timestamp is None else timestamp,
    }
    temporary = path.with_name(f".{path.name}.{payload['pid']}.tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def heartbeat_is_healthy(
    path: Path,
    *,
    maximum_age_seconds: float,
    now: float | None = None,
) -> bool:
    payload = read_heartbeat(path)
    if payload is None or payload.get("ready") is not True:
        return False
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        return False
    age = (time.time() if now is None else now) - float(timestamp)
    return 0 <= age <= maximum_age_seconds
