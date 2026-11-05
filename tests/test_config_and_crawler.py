import unittest
from datetime import UTC

from x_crawler.config import ALL_TYPES, ConfigError, build_config, parse_date_bound, parse_types, resolve_timezone
from x_crawler.crawler import TABS_BY_KEY, CrawlOutcome, _on_user_profile, compute_rate_limit_wait


class ConfigTests(unittest.TestCase):
    def test_username_validation(self):
        self.assertEqual(build_config([], {"TARGET_USER": " @Alice_1 "}).target_user, "Alice_1")
        for bad in ("../etc", "a b", "x" * 16, ""):
            with self.assertRaises(ConfigError):
                build_config(["--user", bad] if bad else [], {"TARGET_USER": bad or " "})

    def test_date_bounds_respect_timezone(self):
        sh = resolve_timezone("Asia/Shanghai")
        self.assertEqual(parse_date_bound("2025-01-01", tz=sh), "2024-12-31T16:00:00Z")
        self.assertEqual(parse_date_bound("2025-01-01", is_end=True, tz=sh), "2025-01-01T15:59:59Z")
        self.assertEqual(parse_date_bound("2025/01/01", tz=UTC), "2025-01-01T00:00:00Z")
        self.assertEqual(parse_date_bound("2025-01-01T08:00:00+08:00"), "2025-01-01T00:00:00Z")
        self.assertIsNone(parse_date_bound("  "))
        with self.assertRaises(ConfigError):
            parse_date_bound("01-2025-01")
        with self.assertRaisesRegex(ConfigError, "日期不存在"):
            parse_date_bound("2025-06-31")

    def test_cli_overrides_env_and_validates_range(self):
        cfg = build_config(["--since", "2025-01-01", "--tz", "UTC", "--types", "replies,posts"], {"SINCE_DATE": "2020-01-01"})
        self.assertEqual(cfg.since_bound, "2025-01-01T00:00:00Z")
        self.assertEqual(cfg.types, ("posts", "replies"))
        with self.assertRaises(ConfigError):
            build_config(["--since", "2025-02-01", "--until", "2025-01-01"], {})
        with self.assertRaises(ConfigError):
            build_config([], {"HEADLESS": "maybe"})

    def test_parse_types(self):
        self.assertEqual(parse_types("all"), ALL_TYPES)
        self.assertIsNone(parse_types(""))
        with self.assertRaises(ConfigError):
            parse_types("likes")


class CrawlerHelperTests(unittest.TestCase):
    def test_rate_limit_wait_prefers_reset_header(self):
        wait = compute_rate_limit_wait(reset_epoch=1000 + 300, attempt=1, now=1000)
        self.assertTrue(305 <= wait <= 320)
        self.assertTrue(60 <= compute_rate_limit_wait(None, 1, now=0) <= 75)
        self.assertTrue(240 <= compute_rate_limit_wait(None, 3, now=0) <= 255)
        self.assertLessEqual(compute_rate_limit_wait(None, 20, now=0), 20 * 60)

    def test_only_genuine_end_conditions_complete_a_tab(self):
        incomplete = {CrawlOutcome.STALLED, CrawlOutcome.RATE_LIMITED, CrawlOutcome.NO_DATA}
        for o in CrawlOutcome:
            self.assertEqual(o.is_complete, o not in incomplete, o)

    def test_tab_endpoint_patterns(self):
        posts = TABS_BY_KEY["posts"].endpoint_re
        replies = TABS_BY_KEY["replies"].endpoint_re
        self.assertTrue(posts.search("/graphql/q/UserTweets?variables=1"))
        self.assertTrue(posts.search("/graphql/q/UserOriginalsTimeline?variables=1"))
        self.assertFalse(posts.search("/graphql/q/UserTweetsAndReplies?variables=1"))
        self.assertTrue(replies.search("/graphql/q/UserTweetsAndReplies?variables=1"))
        self.assertTrue(TABS_BY_KEY["reposts"].endpoint_re.search("/graphql/q/UserRepostsTimeline?v"))

    def test_profile_path_is_not_prefix_match(self):
        self.assertTrue(_on_user_profile("https://x.com/bob", "bob"))
        self.assertTrue(_on_user_profile("https://x.com/Bob/with_replies", "bob"))
        self.assertFalse(_on_user_profile("https://x.com/bobby", "bob"))


if __name__ == "__main__":
    unittest.main()
