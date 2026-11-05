import json
import unittest
import urllib.parse

from tests import payloads as P
from x_crawler.parser import (
    TweetPool,
    dig,
    extract_bottom_cursor,
    extract_user_profile_info,
    inject_cursor_into_url,
    iter_tweet_result_nodes,
    parse_timeline_payload,
    parse_twitter_date,
    unwrap_tweet,
)


class HelperTests(unittest.TestCase):
    def test_dig_handles_none_and_type_mismatch(self):
        self.assertEqual(dig({"a": {"b": 1}}, "a", "b"), 1)
        self.assertIsNone(dig({"a": None}, "a", "b"))
        self.assertIsNone(dig({"a": "str"}, "a", "b"))
        self.assertEqual(dig({"a": [1, 2]}, "a", 1), 2)
        self.assertEqual(dig({}, "x", default={}), {})

    def test_parse_twitter_date(self):
        self.assertEqual(parse_twitter_date("Wed Oct 10 20:19:24 +0800 2018"), "2018-10-10T12:19:24Z")
        self.assertEqual(parse_twitter_date("garbage"), "")
        self.assertEqual(parse_twitter_date(None), "")

    def test_unwrap(self):
        inner = P.tweet("1", "alice")
        self.assertIs(unwrap_tweet({"__typename": "TweetWithVisibilityResults", "tweet": inner}), inner)
        self.assertIsNone(unwrap_tweet({"__typename": "TweetTombstone"}))
        self.assertIsNone(unwrap_tweet({"__typename": "Tweet", "legacy": None}))

    def test_bottom_cursor_and_promoted_skip(self):
        payload = P.timeline(
            P.entry(P.tweet("1", "alice")),
            P.entry(P.tweet("2", "ads"), entry_id="promoted-tweet-2"),
            P.entry(P.tweet("3", "ads"), promoted=True),
            bottom="B1",
        )
        ids = [n["rest_id"] for n in iter_tweet_result_nodes(payload)]
        self.assertEqual(ids, ["1"])
        self.assertEqual(extract_bottom_cursor(payload), "B1")

    def test_inject_cursor_into_url(self):
        variables = {"userId": "1", "count": 20}
        url = "https://x.com/i/api/graphql/abc/UserTweets?" + urllib.parse.urlencode({"variables": json.dumps(variables), "features": "{}"})
        new_url = inject_cursor_into_url(url, "CUR==")
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(new_url).query))
        self.assertEqual(json.loads(qs["variables"]), {"userId": "1", "count": 20, "cursor": "CUR=="})
        self.assertEqual(qs["features"], "{}")
        self.assertIsNone(inject_cursor_into_url("https://x.com/i/api/graphql/abc/UserTweets?features=1", "c"))

    def test_profile_info_new_and_legacy_schema(self):
        info = extract_user_profile_info(P.user("alice", statuses_count=42, followers=7))
        self.assertEqual((info["screen_name"], info["total_tweets"], info["followers"]), ("alice", 42, 7))
        self.assertEqual(info["created_at"], "2010-05-14T18:06:19Z")
        legacy = extract_user_profile_info(P.user("bob", new_schema=False))
        self.assertEqual((legacy["screen_name"], legacy["followers"]), ("bob", 10))
        self.assertIsNone(extract_user_profile_info({"__typename": "UserUnavailable"}))


class RecordTests(unittest.TestCase):
    def parse(self, *entries, **kw):
        pool = TweetPool()
        page = parse_timeline_payload(P.timeline(*entries, **kw), pool)
        return {r["id"]: r for r in page.records}, page, pool

    def test_plain_tweet_with_html_entities(self):
        recs, page, _ = self.parse(P.entry(P.tweet("1", "alice", "a &amp; b")))
        self.assertEqual(recs["1"]["type"], "tweet")
        self.assertEqual(recs["1"]["text"], "a & b")
        self.assertEqual(recs["1"]["created_at"], "2018-10-10T20:19:24Z")
        self.assertEqual(page.bottom_cursor, "CUR-NEXT")

    def test_note_tweet_preferred(self):
        recs, _, _ = self.parse(P.entry(P.tweet("1", "alice", "short…", note="long full text")))
        self.assertEqual(recs["1"]["text"], "long full text")

    def test_reply_parent_resolved_regardless_of_order(self):
        reply = P.tweet("2", "alice", "my reply", reply_to=("1", "bob"))
        parent = P.tweet("1", "bob", "parent text")
        recs, _, _ = self.parse(P.module(reply, parent))
        self.assertEqual(recs["2"]["type"], "reply")
        self.assertEqual(recs["2"]["reply_to"]["text"], "parent text")
        self.assertEqual(recs["2"]["reply_to"]["author"], "bob")

    def test_reply_parent_missing(self):
        recs, _, _ = self.parse(P.entry(P.tweet("2", "alice", reply_to=("1", "bob"))))
        self.assertEqual(recs["2"]["reply_to"], {"id": "1", "author": "bob", "created_at": None, "text": None})

    def test_retweet_keeps_original_metadata_and_full_text(self):
        original = P.tweet("10", "bob", "full original", created="Mon Jan 01 00:00:00 +0000 2018")
        rt = P.tweet("20", "alice", "RT @bob: full orig…", retweeted=original)
        recs, _, _ = self.parse(P.entry(rt))
        r = recs["20"]
        self.assertEqual(r["type"], "retweet")
        self.assertEqual(r["text"], "full original")
        self.assertEqual((r["original_author"], r["original_id"], r["original_created_at"]), ("bob", "10", "2018-01-01T00:00:00Z"))

    def test_retweet_of_quote_preserves_quote(self):
        quoted = P.tweet("5", "carol", "quoted")
        original = P.tweet("10", "bob", "comment", quoted=quoted)
        recs, _, _ = self.parse(P.entry(P.tweet("20", "alice", "RT", retweeted=original)))
        self.assertEqual(recs["20"]["quoted_tweet"]["id"], "5")

    def test_quote_and_visibility_wrapper(self):
        quoted = P.tweet("5", "carol", "quoted", visibility_wrapped=True)
        recs, _, _ = self.parse(P.entry(P.tweet("6", "alice", "my take", quoted=quoted, visibility_wrapped=True)))
        self.assertEqual(recs["6"]["type"], "tweet")
        self.assertEqual(recs["6"]["quoted_tweet"]["author"], "carol")

    def test_null_fields_do_not_crash(self):
        t = P.tweet("1", "alice")
        t["core"] = None
        t["quoted_status_result"] = None
        t["note_tweet"] = None
        t["legacy"]["retweeted_status_result"] = None
        recs, _, _ = self.parse(P.entry(t))
        self.assertEqual(recs["1"]["author"], "")
        self.assertEqual(recs["1"]["type"], "tweet")

    def test_empty_timeline_and_rate_limit_error(self):
        _, page, _ = self.parse(bottom="END")
        self.assertTrue(page.is_empty_timeline)
        rl = parse_timeline_payload({"errors": [{"message": "Rate limit exceeded"}]}, TweetPool())
        self.assertTrue(rl.is_rate_limit_error)
        self.assertFalse(rl.is_empty_timeline)

    def test_graphql_errors_with_data_key_are_not_empty_timeline(self):
        rl = parse_timeline_payload({"data": {}, "errors": [{"message": "Rate limit exceeded"}]}, TweetPool())
        self.assertTrue(rl.is_rate_limit_error)
        self.assertFalse(rl.is_empty_timeline)
        internal = parse_timeline_payload(
            {"data": {"user": {"result": {}}}, "errors": [{"message": "Dependency: Internal error"}]}, TweetPool()
        )
        self.assertFalse(internal.is_empty_timeline)
        self.assertFalse(internal.is_rate_limit_error)
        no_timeline = parse_timeline_payload({"data": {"user": {"result": {}}}}, TweetPool())
        self.assertFalse(no_timeline.is_empty_timeline)

    def test_author_profile_extracted_when_requested(self):
        page = parse_timeline_payload(P.timeline(P.entry(P.tweet("1", "alice"))), TweetPool(), profile_screen_name="ALICE")
        self.assertEqual(page.author_profile["screen_name"], "alice")


if __name__ == "__main__":
    unittest.main()
