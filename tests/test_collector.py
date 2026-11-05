import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tests import payloads as P
from x_crawler.collector import TimelineCollector
from x_crawler.storage import CheckpointState, Storage

DATE_2018 = "Wed Oct 10 20:19:24 +0000 2018"
DATE_2020 = "Wed Oct 14 20:19:24 +0000 2020"


class FakeResponse:
    def __init__(self, url, status=200, payload=None, headers=None, raise_json=False):
        self.url = url
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self._raise = raise_json

    async def json(self):
        if self._raise:
            raise ValueError("bad json")
        return self._payload


TIMELINE_URL = "https://x.com/i/api/graphql/q/UserTweets?variables=%7B%7D"


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name), "alice")
        self.sink = {}
        self.state = CheckpointState()

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, **kw):
        c = TimelineCollector("alice", self.storage, self.sink, self.state, **kw)
        c.begin_tab("Posts", None, None)
        return c

    def journal_ids(self):
        if not self.storage.journal_file.exists():
            return []
        return [json.loads(line)["id"] for line in self.storage.journal_file.read_text(encoding="utf-8").splitlines()]

    def test_filters_foreign_authors_and_persists_before_cursor(self):
        c = self.make()
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("1", "alice")), P.entry(P.tweet("2", "bob")), bottom="B1"))
        self.assertEqual(set(self.sink), {"1"})
        self.assertEqual(c.seen_timeline_ids, {"1", "2"})
        self.assertEqual(self.journal_ids(), ["1"])
        saved = json.loads(self.storage.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["cursors"], {"Posts": "B1"})

    def test_cursor_not_advanced_when_journal_write_fails(self):
        c = self.make()

        def boom(_records):
            raise OSError("disk full")

        self.storage.append_journal = boom
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("1", "alice")), bottom="B1"))
        self.assertNotIn("Posts", self.state.cursors)

    def test_hold_cursor_keeps_checkpoint(self):
        self.state.cursors["Posts"] = "OLD"
        c = TimelineCollector("alice", self.storage, self.sink, self.state)
        c.begin_tab("Posts", None, None, hold_cursor=True)
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("1", "alice")), bottom="PAGE1"))
        self.assertEqual(self.state.cursors["Posts"], "OLD")
        c.hold_cursor = False
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("2", "alice")), bottom="PAGE2"))
        self.assertEqual(self.state.cursors["Posts"], "PAGE2")

    def test_date_range_and_since_streak(self):
        c = self.make(since_bound="2019-01-01T00:00:00Z", until_bound="2020-12-31T23:59:59Z")
        c.process_timeline_payload(
            P.timeline(
                P.entry(P.tweet("new", "alice", created="Wed Oct 14 20:19:24 +0000 2021")),
                P.entry(P.tweet("mid", "alice", created=DATE_2020)),
                P.entry(P.tweet("old1", "alice", created=DATE_2018)),
                P.entry(P.tweet("old2", "alice", created=DATE_2018)),
            )
        )
        self.assertEqual(set(self.sink), {"mid"})
        self.assertEqual(c.consecutive_older_than_since, 2)

    def test_tab_filter(self):
        c = TimelineCollector("alice", self.storage, self.sink, self.state)
        c.begin_tab("Replies", None, lambda t: t["type"] == "reply")
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("1", "alice")), P.entry(P.tweet("2", "alice", reply_to=("9", "bob")))))
        self.assertEqual(set(self.sink), {"2"})

    def test_reply_context_backfilled_across_pages_and_journaled(self):
        c = self.make()
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("2", "alice", reply_to=("1", "bob")))))
        self.assertIsNone(self.sink["2"]["reply_to"]["text"])
        self.assertEqual(c.pending_reply_count, 1)
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("1", "bob", "parent"))))
        self.assertEqual(self.sink["2"]["reply_to"]["text"], "parent")
        self.assertEqual(c.pending_reply_count, 0)
        self.assertEqual(self.journal_ids(), ["2", "2"])  # updated record re-journaled

    def test_media_only_parent_not_pending_and_journaled_once(self):
        c = self.make()
        reply = P.tweet("2", "alice", "nice pic", reply_to=("1", "bob"))
        parent = P.tweet("1", "bob", "")
        c.process_timeline_payload(P.timeline(P.module(reply, parent)))
        self.assertEqual(self.sink["2"]["reply_to"]["text"], "")
        self.assertIsNotNone(self.sink["2"]["reply_to"]["created_at"])
        self.assertEqual(c.pending_reply_count, 0)
        self.assertEqual(self.journal_ids(), ["2"])
        # Reloading the record must not re-flag it as missing context
        c2 = TimelineCollector("alice", self.storage, dict(self.sink), CheckpointState())
        self.assertEqual(c2.pending_reply_count, 0)

    def test_same_page_unresolved_then_resolved_is_journaled_once(self):
        c = self.make()
        # parent registered in the pool by an earlier page, reply arrives later: resolved at build time
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("1", "bob", "parent"))))
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("2", "alice", reply_to=("1", "bob")))))
        self.assertEqual(self.journal_ids(), ["2"])

    def test_dirty_flag_tracks_changes_even_if_journal_fails(self):
        c = self.make()
        self.assertFalse(c.dirty)
        self.storage.append_journal = lambda _r: (_ for _ in ()).throw(OSError("disk full"))
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("1", "alice"))))
        self.assertTrue(c.dirty)
        del self.storage.append_journal
        c.export()
        self.assertFalse(c.dirty)
        self.assertTrue(self.storage.tweets_file.exists())

    def test_error_response_does_not_count_as_end(self):
        c = self.make()
        c.process_timeline_payload({"data": {"user": {"result": {}}}, "errors": [{"message": "Timeout"}]})
        self.assertEqual(c.empty_timeline_responses, 0)
        c.process_timeline_payload({"data": {}, "errors": [{"message": "Rate limit exceeded"}]})
        self.assertTrue(c.rate_limited)

    def test_empty_timeline_counter(self):
        c = self.make()
        c.process_timeline_payload(P.timeline(bottom="END"))
        c.process_timeline_payload(P.timeline(bottom="END"))
        self.assertEqual(c.empty_timeline_responses, 2)

    def test_bottom_terminator_detected(self):
        c = self.make()
        payload = P.timeline(P.entry(P.tweet("1", "alice")))
        payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"].append(
            {"type": "TimelineTerminateTimeline", "direction": "Bottom"}
        )
        c.process_timeline_payload(payload)
        self.assertTrue(c.timeline_terminated)
        c.reset_progress()
        self.assertFalse(c.timeline_terminated)

    def test_profile_fallback_from_tweet_author(self):
        c = self.make()
        c.process_timeline_payload(P.timeline(P.entry(P.tweet("1", "alice"))))
        self.assertEqual(c.user_profile["screen_name"], "alice")
        self.assertTrue(self.storage.profile_file.exists())

    def test_handle_response_rate_limit_and_errors_always_signal(self):
        async def scenario():
            c = self.make()
            await c.handle_response(FakeResponse(TIMELINE_URL, status=429, headers={"x-rate-limit-reset": "1700000000"}))
            self.assertTrue(c.rate_limited)
            self.assertEqual(c.rate_limit_reset, 1700000000.0)
            self.assertTrue(c.response_event.is_set())

            c.response_event.clear()
            await c.handle_response(FakeResponse(TIMELINE_URL, payload="not-a-dict"))
            self.assertTrue(c.response_event.is_set())

            c.response_event.clear()
            await c.handle_response(FakeResponse(TIMELINE_URL, raise_json=True))
            self.assertTrue(c.response_event.is_set())

            c.response_event.clear()
            await c.handle_response(FakeResponse("https://x.com/i/api/graphql/q/HomeTimeline"))
            self.assertFalse(c.response_event.is_set())

        asyncio.run(scenario())

    def test_handle_response_respects_expected_endpoint(self):
        import re

        async def scenario():
            c = TimelineCollector("alice", self.storage, self.sink, self.state)
            c.begin_tab("Reposts", re.compile(r"UserRepostsTimeline"), None)
            await c.handle_response(FakeResponse(TIMELINE_URL, payload=P.timeline(P.entry(P.tweet("1", "alice")))))
            self.assertEqual(self.sink, {})

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
