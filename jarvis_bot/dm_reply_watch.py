"""One authorised DM reply alert; never forward reply content or invoke AI."""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .send_reminder import _discord_token

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 60


def save_watch(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def discord_api(path: str, method: str = "GET", payload: dict | None = None):
    request = urllib.request.Request(
        "https://discord.com/api/v10" + path,
        headers={"Authorization": "Bot " + _discord_token(),
                 "User-Agent": "JarvisDiscordBot/1.0", "Content-Type": "application/json"},
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def check_once(state: dict, save, api=discord_api) -> bool:
    """Return true once the single notification is verified or already complete."""
    if state["status"] == "notified":
        return True
    channel_id = str(state["dm_channel_id"])
    recipient_id = str(state["recipient_id"])
    bot_id = str(state["bot_id"])
    original_id = str(state["after_message_id"])
    notify_channel = str(state["notify_channel_id"])
    owner_id = str(state["owner_id"])
    for value in (channel_id, recipient_id, bot_id, original_id, notify_channel, owner_id):
        if not value.isdecimal():
            raise ValueError("Invalid watch identifier")

    if state["status"] == "waiting":
        channel = api("/channels/" + channel_id)
        if (channel.get("type") != 1
                or [str(user["id"]) for user in channel.get("recipients", [])] != [recipient_id]):
            raise ValueError("Unexpected DM recipient")
        original = api(f"/channels/{channel_id}/messages/{original_id}")
        if (str(original.get("channel_id")) != channel_id
                or str(original.get("author", {}).get("id")) != bot_id):
            raise ValueError("Unexpected invitation author")
        messages = api(f"/channels/{channel_id}/messages?after={original_id}&limit=100")
        replies = [message for message in messages
                   if str(message.get("author", {}).get("id")) == recipient_id
                   and not message.get("author", {}).get("bot", False)
                   and message.get("type", 0) in (0, 19)
                   and int(message["id"]) > int(original_id)]
        state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        if not replies:
            save(state)
            return False
        reply = min(replies, key=lambda message: int(message["id"]))
        # Retain only identifiers, never DM text, attachments or the calendar.
        state["reply_id"] = str(reply["id"])
        state["status"] = "ready"
        save(state)

    if state["status"] != "ready":
        raise ValueError("Unexpected watch state")
    received = ((int(state["reply_id"]) >> 22) + 1420070400000) // 1000
    content = (f"📬 <@{owner_id}> **MemberFive has replied to your duo invitation.**\n"
               f"-# Received <t:{received}:f>.")
    # Reconcile a POST whose response was lost before retrying it after restart.
    recent = api(f"/channels/{notify_channel}/messages?limit=100")
    notification = next((message for message in recent
                         if str(message.get("author", {}).get("id")) == bot_id
                         and message.get("content") == content), None)
    if notification is None:
        notification = api(f"/channels/{notify_channel}/messages", "POST", {
            "content": content,
            "allowed_mentions": {"parse": [], "users": [owner_id]},
            "nonce": "dw" + original_id,
            "enforce_nonce": True,
        })
    message_id = str(notification["id"])
    verified = api(f"/channels/{notify_channel}/messages/{message_id}")
    if (str(verified.get("channel_id")) != notify_channel
            or str(verified.get("author", {}).get("id")) != bot_id
            or verified.get("content") != content):
        raise ValueError("Notification could not be verified")
    state["notification_id"] = message_id
    state["status"] = "notified"
    state["notified_at"] = datetime.now(timezone.utc).isoformat()
    save(state)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Keep a single process across manual starts and service restarts.
    with args.state.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.info("This reply watch is already running")
            return
        LOGGER.info("One-off DM reply watch started")
        while True:
            try:
                state = json.loads(args.state.read_text())
                if check_once(state, lambda value: save_watch(args.state, value)):
                    LOGGER.info("Reply notification verified; watch complete")
                    return
            except urllib.error.HTTPError as error:
                # Never print response bodies, private URLs, headers or tokens.
                LOGGER.warning("Discord check deferred: HTTP %s", error.code)
            except Exception as error:
                LOGGER.warning("Reply check deferred: %s", type(error).__name__)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
