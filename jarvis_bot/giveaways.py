"""Consume confirmed rewards without giving the browser Discord credentials."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Awaitable, Callable

LOGGER = logging.getLogger(__name__)
CURRENCIES = frozenset({"Coins", "Gems", "Roll Chips", "Golden Potions"})


def reward_payload(row: sqlite3.Row) -> dict:
    event_id, amount, currency = row["event_id"], row["amount"], row["currency"]
    if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,120}", event_id):
        raise ValueError("Invalid event ID")
    if currency not in CURRENCIES or not isinstance(amount, str) or len(amount) > 32:
        raise ValueError("Invalid reward")
    try:
        value = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError("Invalid reward amount") from exc
    if not value.is_finite() or not 0 < value <= Decimal("1000000000"):
        raise ValueError("Invalid reward amount")
    evidence = row["evidence_ref"]
    if value.as_tuple().exponent < -8 or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500:
        raise ValueError("Invalid reward evidence")
    amount = format(value.normalize(), "f")
    return {
        "content": f"🎉 CSGORoll giveaway reward confirmed: **{amount} {currency}**.",
        "allowed_mentions": {"parse": []},
        "nonce": hashlib.sha256(f"csgoroll:{event_id}".encode()).hexdigest()[:24],
        "enforce_nonce": True,
    }


class GiveawayOutbox:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def recover(self):
        # A process may have died after Discord accepted a message. Its short
        # nonce-deduplication window is not sufficient for replaying old sends.
        # Keep these explicit for reconciliation instead of risking a duplicate.
        with closing(self.connect()) as db, db:
            db.execute("UPDATE reward_notifications SET status='uncertain', updated_at=CURRENT_TIMESTAMP "
                       "WHERE status='sending'")

    def claim(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reward_notifications WHERE status='pending' "
                             "ORDER BY created_at,event_id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE reward_notifications SET status='sending',updated_at=CURRENT_TIMESTAMP "
                           "WHERE event_id=? AND status='pending'", (row["event_id"],))
            return row

    def finish(self, event_id: str, message_id: str | None):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE reward_notifications SET status=?,message_id=?,updated_at=CURRENT_TIMESTAMP "
                       "WHERE event_id=? AND status='sending'",
                       ("sent" if message_id else "uncertain", message_id, event_id))


async def deliver_one(outbox: GiveawayOutbox, channel_id: int,
                      sender: Callable[[int, dict], Awaitable[dict]]) -> bool:
    row = await asyncio.to_thread(outbox.claim)
    if row is None:
        return False
    try:
        payload = reward_payload(row)
        result = await sender(channel_id, payload)
        message_id = str(result.get("id", ""))
        if not message_id.isdigit() or str(result.get("channel_id")) != str(channel_id):
            raise ValueError("Unexpected Discord delivery response")
        await asyncio.to_thread(outbox.finish, row["event_id"], message_id)
        LOGGER.info("Delivered CSGORoll reward notification")
    except Exception as exc:
        await asyncio.to_thread(outbox.finish, row["event_id"], None)
        LOGGER.warning("CSGORoll notification requires reconciliation: %s", type(exc).__name__)
    return True


async def giveaway_worker(client, path: Path, channel_id: int):
    from discord.http import Route

    await client.wait_until_ready()
    outbox = GiveawayOutbox(path)
    recovered = False

    async def sender(destination: int, payload: dict):
        return await client.http.request(
            Route("POST", "/channels/{channel_id}/messages", channel_id=destination), json=payload
        )

    while True:
        try:
            if path.is_file():
                if not recovered:
                    await asyncio.to_thread(outbox.recover)
                    recovered = True
                    LOGGER.info("CSGORoll reward notification worker ready for channel %s", channel_id)
                if await deliver_one(outbox, channel_id, sender):
                    continue
        except (OSError, sqlite3.Error) as exc:
            LOGGER.warning("CSGORoll reward queue unavailable: %s", type(exc).__name__)
        await asyncio.sleep(5)
