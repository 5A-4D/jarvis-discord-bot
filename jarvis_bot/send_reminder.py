from __future__ import annotations

import argparse
import json
import mimetypes
import os
import uuid
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import dotenv_values


def _discord_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        project_root = Path(__file__).resolve().parent.parent
        token = (dotenv_values(project_root / ".env").get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Discord bot token is unavailable")
    return token


def send_message(channel_id: int, content: str) -> str:
    token = _discord_token()

    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["users"]},
        "flags": 1 << 2,  # Discord SUPPRESS_EMBEDS
    }
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "JarvisDiscordBot/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"Discord returned HTTP {error.code}: {detail}") from error

    if str(result.get("channel_id")) != str(channel_id):
        raise RuntimeError("Discord returned an unexpected channel ID")
    if result.get("content") != content:
        raise RuntimeError("Discord returned unexpected reminder content")
    message_id = result.get("id")
    if not message_id:
        raise RuntimeError("Discord returned no reminder message ID")
    return str(message_id)


def send_message_with_attachment(
    channel_id: int,
    content: str,
    attachment_path: Path,
    *,
    token: str | None = None,
) -> str:
    """Send and verify a Discord message containing one local attachment."""
    if not attachment_path.is_file():
        raise RuntimeError(f"Attachment does not exist: {attachment_path}")
    resolved_token = token or _discord_token()
    filename = attachment_path.name
    payload = {
        "content": content,
        "allowed_mentions": {"parse": []},
        "attachments": [{"id": 0, "filename": filename}],
    }
    boundary = f"JarvisBoundary{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = bytearray()

    def append(value: str | bytes) -> None:
        body.extend(value.encode("utf-8") if isinstance(value, str) else value)

    append(f"--{boundary}\r\n")
    append('Content-Disposition: form-data; name="payload_json"\r\n')
    append("Content-Type: application/json\r\n\r\n")
    append(json.dumps(payload))
    append("\r\n")
    append(f"--{boundary}\r\n")
    append(
        f'Content-Disposition: form-data; name="files[0]"; '
        f'filename="{filename}"\r\n'
    )
    append(f"Content-Type: {mime_type}\r\n\r\n")
    append(attachment_path.read_bytes())
    append("\r\n")
    append(f"--{boundary}--\r\n")

    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=bytes(body),
        headers={
            "Authorization": f"Bot {resolved_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "JarvisDiscordBot/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"Discord returned HTTP {error.code}: {detail}") from error

    if str(result.get("channel_id")) != str(channel_id):
        raise RuntimeError("Discord returned an unexpected channel ID")
    if result.get("content") != content:
        raise RuntimeError("Discord returned unexpected message content")
    returned_names = {
        item.get("filename")
        for item in result.get("attachments", [])
        if isinstance(item, dict)
    }
    if filename not in returned_names:
        raise RuntimeError("Discord did not return the expected attachment")
    message_id = result.get("id")
    if not message_id:
        raise RuntimeError("Discord returned no message ID")
    return str(message_id)


def edit_message(channel_id: int, message_id: str, content: str) -> str:
    token = _discord_token()

    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["users"]},
    }
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "JarvisDiscordBot/1.0",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"Discord returned HTTP {error.code}: {detail}") from error

    if str(result.get("channel_id")) != str(channel_id):
        raise RuntimeError("Discord returned an unexpected channel ID")
    if str(result.get("id")) != str(message_id):
        raise RuntimeError("Discord returned an unexpected message ID")
    if result.get("content") != content:
        raise RuntimeError("Discord returned unexpected reminder content")
    return str(message_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one verified Discord reminder")
    parser.add_argument("--channel-id", required=True, type=int)
    content_source = parser.add_mutually_exclusive_group(required=True)
    content_source.add_argument("--content")
    content_source.add_argument("--content-file", type=Path)
    args = parser.parse_args()
    content = args.content
    if args.content_file is not None:
        content = args.content_file.read_text(encoding="utf-8").strip()
        if not content:
            raise RuntimeError("Reminder content file is empty")
    message_id = send_message(args.channel_id, content)
    print(f"Discord reminder sent and verified: message_id={message_id}")


if __name__ == "__main__":
    main()
