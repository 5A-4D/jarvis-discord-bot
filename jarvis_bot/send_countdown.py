from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis_bot.send_reminder import edit_message, send_message


WOW_FOREVER_RELEASE = datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc)
COUNTDOWN_START = datetime(2026, 10, 2, 16, 30, 29, tzinfo=timezone.utc)
BAR_WIDTH = 20
REPOST_AFTER = timedelta(days=3)
DEFAULT_STATE_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "wow-forever-countdown.json"
)


def countdown_values(now: datetime) -> tuple[float, float, int] | None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    now_utc = now.astimezone(timezone.utc)
    remaining_seconds = (WOW_FOREVER_RELEASE - now_utc).total_seconds()
    if remaining_seconds < 0:
        return None

    total_seconds = (WOW_FOREVER_RELEASE - COUNTDOWN_START).total_seconds()
    remaining_seconds = min(remaining_seconds, total_seconds)
    ratio = remaining_seconds / total_seconds
    filled = min(BAR_WIDTH, int((ratio * BAR_WIDTH) + 0.5))
    return (
        remaining_seconds / 86_400,
        total_seconds / 86_400,
        filled,
    )


def build_message(now: datetime) -> str | None:
    values = countdown_values(now)
    if values is None:
        return None
    remaining_days, total_days, filled = values
    bar = ("█" * filled) + ("░" * (BAR_WIDTH - filled))
    return f"`⚔ 〔{bar}〕 {remaining_days:.2f} / {total_days:.2f}`"


def _load_state(state_file: Path) -> tuple[str, datetime] | None:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        message_id = str(state["message_id"])
        posted_at = datetime.fromisoformat(state["posted_at"])
        if not message_id or posted_at.tzinfo is None:
            return None
        return message_id, posted_at.astimezone(timezone.utc)
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _save_state(state_file: Path, message_id: str, posted_at: datetime) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_suffix(state_file.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "message_id": message_id,
                "posted_at": posted_at.astimezone(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(state_file)


def publish_countdown(channel_id: int, now: datetime, state_file: Path) -> str | None:
    content = build_message(now)
    if content is None:
        return None

    now_utc = now.astimezone(timezone.utc)
    state = _load_state(state_file)
    if state is not None:
        message_id, posted_at = state
        if now_utc - posted_at < REPOST_AFTER:
            try:
                edit_message(channel_id, message_id, content)
                return f"edited:{message_id}"
            except RuntimeError as error:
                if "HTTP 404" not in str(error):
                    raise

    message_id = send_message(channel_id, content)
    _save_state(state_file, message_id, now_utc)
    return f"posted:{message_id}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send the verified WoW Forever release countdown to Discord"
    )
    parser.add_argument("--channel-id", required=True, type=int)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    args = parser.parse_args()

    result = publish_countdown(
        args.channel_id,
        datetime.now(timezone.utc),
        args.state_file,
    )
    if result is None:
        print("WoW Forever has released; no countdown sent")
        return
    action, message_id = result.split(":", 1)
    print(f"Discord countdown {action} and verified: message_id={message_id}")


if __name__ == "__main__":
    main()
