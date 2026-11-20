import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis_bot.send_reminder import (
    edit_message,
    send_message,
    send_message_with_attachment,
)


class ReminderDeliveryTests(unittest.TestCase):
    def test_reminder_suppresses_discord_embeds(self) -> None:
        response = io.BytesIO(
            json.dumps(
                {
                    "id": "789",
                    "channel_id": "123",
                    "content": "https://example.com/details",
                }
            ).encode("utf-8")
        )

        with (
            patch(
                "jarvis_bot.send_reminder.dotenv_values",
                return_value={"DISCORD_BOT_TOKEN": "token"},
            ),
            patch(
                "jarvis_bot.send_reminder.urllib.request.urlopen"
            ) as urlopen,
        ):
            urlopen.return_value.__enter__.return_value = response
            message_id = send_message(123, "https://example.com/details")

        self.assertEqual(message_id, "789")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["flags"], 4)

    def test_edit_message_uses_patch_and_verifies_response(self) -> None:
        response = io.BytesIO(
            json.dumps(
                {
                    "id": "789",
                    "channel_id": "123",
                    "content": "updated",
                }
            ).encode("utf-8")
        )

        with (
            patch(
                "jarvis_bot.send_reminder.dotenv_values",
                return_value={"DISCORD_BOT_TOKEN": "token"},
            ),
            patch("jarvis_bot.send_reminder.urllib.request.urlopen") as urlopen,
        ):
            urlopen.return_value.__enter__.return_value = response
            message_id = edit_message(123, "789", "updated")

        self.assertEqual(message_id, "789")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "PATCH")
        self.assertTrue(request.full_url.endswith("/channels/123/messages/789"))
        self.assertEqual(json.loads(request.data)["content"], "updated")

    def test_attachment_message_is_multipart_and_verified(self) -> None:
        response = io.BytesIO(
            json.dumps(
                {
                    "id": "790",
                    "channel_id": "123",
                    "content": "Jarvis offline",
                    "attachments": [{"filename": "offline.gif"}],
                }
            ).encode("utf-8")
        )

        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "offline.gif"
            attachment.write_bytes(b"GIF89a-test")
            with patch("jarvis_bot.send_reminder.urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value = response
                message_id = send_message_with_attachment(
                    123,
                    "Jarvis offline",
                    attachment,
                    token="token",
                )

        self.assertEqual(message_id, "790")
        request = urlopen.call_args.args[0]
        self.assertIn("multipart/form-data", request.headers["Content-type"])
        self.assertIn(b"offline.gif", request.data)
        self.assertIn(b"GIF89a-test", request.data)


if __name__ == "__main__":
    unittest.main()
