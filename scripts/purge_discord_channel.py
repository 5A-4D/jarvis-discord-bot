#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import load_dotenv


API_BASE = "https://discord.com/api/v10"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def discord_request(
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "JarvisDiscordBot (message maintenance)",
    }
    for _ in range(8):
        request = Request(
            f"{API_BASE}{path}", data=body, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read()
                return json.loads(response_body) if response_body else None
        except HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            if error.code == 429:
                try:
                    retry_after = float(json.loads(response_body)["retry_after"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    retry_after = 2.0
                time.sleep(max(retry_after, 0.25))
                continue
            raise RuntimeError(
                f"Discord API returned HTTP {error.code}: {response_body[:500]}"
            ) from error
    raise RuntimeError("Discord API rate limit did not clear after eight retries")


def stored_message_ids(
    database: Path, channel_id: int, after: str, through_message_id: int
) -> list[int]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            """
            SELECT message_id
            FROM channel_messages
            WHERE channel_id = ?
              AND created_at >= ?
              AND message_id <= ?
              AND deleted_at IS NULL
            ORDER BY message_id
            """,
            (channel_id, after, through_message_id),
        ).fetchall()
    return [int(row[0]) for row in rows]


def delete_messages(token: str, channel_id: int, message_ids: list[int]) -> int:
    deleted = 0
    pending = list(message_ids)
    while len(pending) >= 2:
        batch = pending[:100]
        if len(batch) == 1:
            break
        discord_request(
            token,
            "POST",
            f"/channels/{channel_id}/messages/bulk-delete",
            {"messages": [str(message_id) for message_id in batch]},
        )
        deleted += len(batch)
        del pending[: len(batch)]
    if pending:
        discord_request(
            token,
            "DELETE",
            f"/channels/{channel_id}/messages/{pending[0]}",
        )
        deleted += 1
    return deleted


def send_notice(token: str, channel_id: int, user_id: int, content: str) -> None:
    discord_request(
        token,
        "POST",
        f"/channels/{channel_id}/messages",
        {
            "content": f"<@{user_id}> {content}",
            "allowed_mentions": {"parse": [], "users": [str(user_id)]},
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--through", type=int, required=True)
    parser.add_argument("--notify-user", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = PROJECT_ROOT / "data" / "jarvis.sqlite3"
    message_ids = stored_message_ids(
        database, args.channel, args.after, args.through
    )
    if args.dry_run:
        print(f"Would delete {len(message_ids)} messages")
        return 0

    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("DISCORD_BOT_TOKEN is unavailable", file=sys.stderr)
        return 1

    try:
        deleted = delete_messages(token, args.channel, message_ids)
        send_notice(
            token,
            args.channel,
            args.notify_user,
            f"Deleted {deleted} messages from today through your purge request.",
        )
        return 0
    except Exception as error:
        failure = f"The requested message purge failed: {str(error)[:500]}"
        try:
            send_notice(token, args.channel, args.notify_user, failure)
        except Exception as notice_error:
            print(f"{failure}; notification also failed: {notice_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
