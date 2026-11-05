import json
import tempfile
import unittest
from pathlib import Path

from x_crawler.storage import CheckpointState, Storage, StorageError


def rec(i, created="2024-01-01T00:00:00Z"):
    return {"id": str(i), "created_at": created, "author": "alice", "type": "tweet", "text": f"t{i}"}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.s = Storage(self.data_dir, "alice")

    def tearDown(self):
        self._tmp.cleanup()

    def test_export_load_roundtrip_sorted_newest_first(self):
        self.s.export({"1": rec(1, "2020-01-01T00:00:00Z"), "2": rec(2, "2021-01-01T00:00:00Z")})
        items = json.loads(self.s.tweets_file.read_text(encoding="utf-8"))
        self.assertEqual([t["id"] for t in items], ["2", "1"])
        self.assertEqual(set(self.s.load_tweets()), {"1", "2"})

    def test_corrupt_tweets_file_raises_and_is_untouched(self):
        self.s.user_dir.mkdir(parents=True)
        self.s.tweets_file.write_text("[{broken", encoding="utf-8")
        with self.assertRaises(StorageError):
            self.s.load_tweets()
        self.assertEqual(self.s.tweets_file.read_text(encoding="utf-8"), "[{broken")

    def test_journal_replay_tolerates_partial_last_line(self):
        self.s.export({"1": rec(1)})
        self.s.append_journal([rec(2), {**rec(1), "text": "updated"}])
        with open(self.s.journal_file, "a", encoding="utf-8") as f:
            f.write('{"id": "3", "trunc')
        loaded = self.s.load_tweets()
        self.assertEqual(set(loaded), {"1", "2"})
        self.assertEqual(loaded["1"]["text"], "updated")
        self.assertFalse(self.s.journal_file.exists())  # merged into tweets.json
        self.assertEqual(len(json.loads(self.s.tweets_file.read_text(encoding="utf-8"))), 2)

    def test_legacy_migration_writes_before_delete_and_keeps_bad_files(self):
        good = self.data_dir / "alice.json"
        good.write_text(json.dumps([rec(5)]), encoding="utf-8")
        bad = self.data_dir / "alice_20240101_000000.json"
        bad.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        loaded = self.s.load_tweets()
        self.assertIn("5", loaded)
        self.assertFalse(good.exists())
        self.assertTrue(bad.exists())
        self.assertTrue(self.s.tweets_file.exists())

    def test_state_roundtrip_reset_on_range_change_and_legacy_keys(self):
        st = CheckpointState(cursors={"回复": "C1"}, completed_tabs={"推文+转推"})
        self.s.save_state(st, "2024-01-01T00:00:00Z", None)
        loaded = self.s.load_state("2024-01-01T00:00:00Z", None)
        self.assertEqual(loaded.cursors, {"回复(Replies)": "C1"})
        self.assertEqual(loaded.completed_tabs, {"原创推文(Posts)"})
        self.assertEqual(self.s.load_state("2025-01-01T00:00:00Z", None), CheckpointState())

    def test_corrupt_state_is_ignored(self):
        self.s.user_dir.mkdir(parents=True)
        self.s.state_file.write_text("nope", encoding="utf-8")
        self.assertEqual(self.s.load_state(None, None), CheckpointState())

    def test_backup_and_reset_moves_files(self):
        self.s.export({"1": rec(1)})
        self.s.save_state(CheckpointState(cursors={"a": "b"}), None, None)
        backup = self.s.backup_and_reset()
        self.assertFalse(self.s.tweets_file.exists())
        self.assertTrue((backup / "tweets.json").exists())
        self.assertTrue((backup / "state.json").exists())

    def test_profile_roundtrip_checks_user(self):
        self.s.save_profile({"screen_name": "Alice", "total_tweets": 3})
        self.assertEqual(self.s.load_profile()["total_tweets"], 3)
        Storage(self.data_dir, "bob").profile_file.parent.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(Storage(self.data_dir, "bob").load_profile())


if __name__ == "__main__":
    unittest.main()
