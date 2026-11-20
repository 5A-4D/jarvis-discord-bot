import copy
import unittest

from jarvis_bot.dm_reply_watch import check_once


class ReplyWatchTests(unittest.TestCase):
    def setUp(self):
        self.state = {"status": "waiting", "dm_channel_id": "10", "recipient_id": "20",
                      "bot_id": "30", "after_message_id": "100", "notify_channel_id": "40",
                      "owner_id": "50"}
        self.saved = []
        self.posts = []
        self.messages = []
        self.notification = None

    def save(self, state):
        self.saved.append(copy.deepcopy(state))

    def api(self, path, method="GET", payload=None):
        if path == "/channels/10":
            return {"type": 1, "recipients": [{"id": "20"}]}
        if path == "/channels/10/messages/100":
            return {"channel_id": "10", "author": {"id": "30"}}
        if path == "/channels/10/messages?after=100&limit=100":
            return self.messages
        if path == "/channels/40/messages?limit=100":
            return [self.notification] if self.notification else []
        if path == "/channels/40/messages" and method == "POST":
            self.posts.append(payload)
            self.notification = {"id": "300", "channel_id": "40", "author": {"id": "30"},
                                 "content": payload["content"]}
            return self.notification
        if path == "/channels/40/messages/300":
            return self.notification
        raise AssertionError(path)

    def test_unrelated_bots_and_older_messages_do_not_trigger(self):
        self.messages = [{"id": "101", "author": {"id": "30", "bot": True}},
                         {"id": "102", "author": {"id": "99"}},
                         {"id": "99", "author": {"id": "20"}}]
        self.assertFalse(check_once(self.state, self.save, self.api))
        self.assertFalse(self.posts)
        self.assertIn("last_checked_at", self.state)

    def test_first_reply_notifies_once_and_never_forwards_dm_text(self):
        self.messages = [{"id": "200", "author": {"id": "20"}, "content": "PRIVATE secret"}]
        self.assertTrue(check_once(self.state, self.save, self.api))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.state["status"], "notified")
        self.assertNotIn("PRIVATE", repr(self.saved) + repr(self.posts))
        self.assertEqual(self.posts[0]["allowed_mentions"], {"parse": [], "users": ["50"]})
        self.assertTrue(check_once(self.state, self.save, self.api))
        self.assertEqual(len(self.posts), 1)

    def test_restart_reconciles_accepted_notification_after_lost_response(self):
        self.messages = [{"id": "200", "author": {"id": "20"}}]
        def interrupted(path, method="GET", payload=None):
            result = self.api(path, method, payload)
            if method == "POST":
                raise TimeoutError("Private transport detail")
            return result
        with self.assertRaises(TimeoutError):
            check_once(self.state, self.save, interrupted)
        self.assertEqual(self.state["status"], "ready")
        restarted = copy.deepcopy(self.saved[-1])
        self.assertTrue(check_once(restarted, self.save, self.api))
        self.assertEqual(len(self.posts), 1)

    def test_wrong_recipient_is_rejected_before_reading_history(self):
        with self.assertRaises(ValueError):
            check_once(self.state, self.save, lambda *_: {"type": 1, "recipients": [{"id": "99"}]})
        self.assertFalse(self.posts)
