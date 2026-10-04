from pathlib import Path
import sqlite3
import tempfile
import unittest

from core import Catalog, LikeSeenRecord, PostRecord, find_bookmark_boundary


class InboxResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="smc-regression-reset-")
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / "catalog.db")
        self.records = [PostRecord(str(1000+i), content=f"body {i}") for i in range(205)]
        self.catalog.upsert_collection_posts("one", self.records)
        self.catalog.upsert_collection_posts("two", [PostRecord("9999", content="other collection")])
        self.saved_file = self.root / "saved.md"
        self.saved_file.write_bytes(b"keep this saved file")
        self.catalog.set_collection_post_choice("one", "1000", "text", markdown_path=str(self.saved_file))
        self.catalog.replace_likes_anchors([LikeSeenRecord("1000", 1)], target_key="same target", login_profile="test")
        self.catalog.record_likes_seen([LikeSeenRecord("1000", 1)], target_key="same target", login_profile="test")

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def rows(self, collection):
        return self.catalog.conn.execute("SELECT * FROM collection_posts WHERE collection_id=? ORDER BY post_id", (collection,)).fetchall()

    def test_empty_clears_all_pages_without_erasing_known_ids_or_processed_files(self):
        other = self.rows("two")
        processed = self.rows("one")[0]
        self.assertEqual(self.catalog.reset_collection_inbox("one", "empty"), 204)
        self.assertEqual(self.catalog.pending_collection_post_count("one"), 0)
        self.assertEqual(len(self.catalog.collection_post_ids("one")), 205)
        self.assertEqual(self.rows("one")[0], processed)
        self.assertEqual(self.rows("two"), other)
        self.assertEqual(self.saved_file.read_bytes(), b"keep this saved file")
        self.assertEqual(self.catalog.upsert_collection_posts("one", self.records)["added"], 0)
        self.assertEqual(self.catalog.pending_collection_post_count("one"), 0)
        self.assertEqual(find_bookmark_boundary(self.records, self.catalog.collection_post_ids("one"))["records"], [])

    def test_history_reset_reimports_posts_and_preserves_other_collections_anchors_and_files(self):
        other = self.rows("two")
        anchors = self.catalog.likes_anchor_ids(target_key="same target", login_profile="test")
        seen = self.catalog.likes_seen_counts(target_key="same target", login_profile="test")
        self.assertEqual(self.catalog.reset_collection_inbox("one", "history"), 205)
        self.assertEqual(self.catalog.collection_post_ids("one"), set())
        self.assertEqual(self.rows("two"), other)
        self.assertEqual(self.catalog.likes_anchor_ids(target_key="same target", login_profile="test"), anchors)
        self.assertEqual(self.catalog.likes_seen_counts(target_key="same target", login_profile="test"), seen)
        self.assertEqual(self.saved_file.read_bytes(), b"keep this saved file")
        self.assertEqual(self.catalog.upsert_collection_posts("one", self.records)["added"], 205)
        self.assertEqual(self.catalog.pending_collection_post_count("one"), 205)

    def test_empty_survives_restart_and_does_not_recover_as_interrupted_work(self):
        self.catalog.reset_collection_inbox("one", "empty")
        self.catalog.close()
        self.catalog = Catalog(self.root / "catalog.db")
        self.assertEqual(self.catalog.recover_interrupted_collection_posts(), 0)
        self.assertEqual(self.catalog.pending_collection_post_count("one"), 0)
        self.assertEqual(self.catalog.reset_collection_inbox("one", "empty"), 0)
        self.assertEqual(self.catalog.reset_collection_inbox("missing", "history"), 0)

    def test_invalid_scope_or_mode_cannot_reset_every_collection(self):
        before = self.rows("one"), self.rows("two")
        for collection, mode in (("", "history"), (" ", "empty"), ("one", "unknown")):
            with self.assertRaises(ValueError):
                self.catalog.reset_collection_inbox(collection, mode)
        self.assertEqual((self.rows("one"), self.rows("two")), before)

    def test_failure_rolls_back_whole_page_spanning_reset(self):
        before = self.rows("one")
        self.catalog.conn.execute("""CREATE TRIGGER reset_failure BEFORE UPDATE ON collection_posts
                                   WHEN OLD.post_id='1100' BEGIN SELECT RAISE(ABORT,'test reset failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.catalog.reset_collection_inbox("one", "empty")
        self.assertEqual(self.rows("one"), before)
