import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from jarvis_bot.giveaways import GiveawayOutbox, deliver_one


def fixture(path: Path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE reward_notifications (event_id TEXT PRIMARY KEY,amount TEXT,currency TEXT,"
                   "evidence_ref TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,status TEXT DEFAULT 'pending',"
                   "message_id TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO reward_notifications(event_id,amount,currency,evidence_ref) VALUES(?,?,?,?)",
                   ("rain:123", "0.42", "Coins", "account-reward:456"))


class GiveawayDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivers_once_and_disables_mentions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            fixture(path)
            calls = []

            async def sender(channel, payload):
                calls.append((channel, payload))
                return {"id": "789", "channel_id": str(channel)}

            outbox = GiveawayOutbox(path)
            self.assertTrue(await deliver_one(outbox, 123, sender))
            self.assertFalse(await deliver_one(GiveawayOutbox(path), 123, sender))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]["allowed_mentions"], {"parse": []})
            self.assertTrue(calls[0][1]["enforce_nonce"])
            self.assertIn("0.42 Coins", calls[0][1]["content"])

    async def test_ambiguous_send_is_retained_without_automatic_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            fixture(path)
            calls = []

            async def sender(channel, payload):
                calls.append(payload)
                raise TimeoutError()

            outbox = GiveawayOutbox(path)
            await deliver_one(outbox, 123, sender)
            self.assertFalse(await deliver_one(outbox, 123, sender))
            self.assertEqual(len(calls), 1)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT status FROM reward_notifications").fetchone()[0], "uncertain")

    async def test_crash_recovery_does_not_repost_accepted_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            fixture(path)
            GiveawayOutbox(path).claim()
            recovered = GiveawayOutbox(path)
            recovered.recover()
            self.assertIsNone(recovered.claim())

    async def test_malformed_reward_never_reaches_discord(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            fixture(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE reward_notifications SET currency='@everyone'")

            async def sender(channel, payload):
                self.fail("Malformed reward was sent")

            await deliver_one(GiveawayOutbox(path), 123, sender)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT status FROM reward_notifications").fetchone()[0], "uncertain")

    async def test_two_consumers_cannot_claim_the_same_reward(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            fixture(path)
            rows = await asyncio.gather(
                asyncio.to_thread(GiveawayOutbox(path).claim),
                asyncio.to_thread(GiveawayOutbox(path).claim),
            )
            self.assertEqual(sum(row is not None for row in rows), 1)


if __name__ == "__main__":
    unittest.main()
