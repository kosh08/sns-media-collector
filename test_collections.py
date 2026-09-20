import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from collection_profiles import CollectionProfile, CollectionStore
from core import Catalog, PostRecord, build_x_bookmark_scan_command, parse_smc_post_line, write_post_markdown


class CollectionProfileTests(unittest.TestCase):
    def test_store_roundtrip_and_reorder(self):
        with tempfile.TemporaryDirectory() as td:
            store = CollectionStore(Path(td) / "collections.json")
            a = store.upsert(CollectionProfile("A", "acct", "x"))
            b = store.upsert(CollectionProfile("B", "acct", "x", source="bookmarks"))
            store.reorder([b.collection_id, a.collection_id])
            self.assertEqual([x.name for x in CollectionStore(store.path).items], ["B", "A"])

    def test_legacy_sessions_migrate_once(self):
        with tempfile.TemporaryDirectory() as td:
            store = CollectionStore(Path(td) / "collections.json")
            account = SimpleNamespace(profile_id="p1", name="Main", platform="x")
            sessions = {"p1": {"target_type": "likes", "target": "@other", "destination": "D:/x"}}
            self.assertEqual(store.migrate_account_sessions([account], sessions), 1)
            self.assertEqual(store.items[0].target_scope, "other")
            self.assertEqual(store.migrate_account_sessions([account], sessions), 0)

    def test_other_requires_target_but_self_does_not(self):
        CollectionProfile("Self", "a", "x", target_scope="self").validate()
        with self.assertRaises(ValueError):
            CollectionProfile("Other", "a", "x", target_scope="other").validate()


class BookmarkTests(unittest.TestCase):
    def test_parse_bookmark_metadata_with_json_escaped_text(self):
        line = 'SMC_POST\t1234567890123456789\t42\t"name"\t2026-01-02T03:04:05+0000\t2026-02-03T04:05:06+0000\t"hello\\nworld"\t2'
        rec = parse_smc_post_line(line)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.content, "hello\nworld")
        self.assertEqual(rec.media_count, 2)

    def test_bookmark_scan_is_text_aware_and_no_download(self):
        cmd = build_x_bookmark_scan_command(
            ["gallery-dl"], auth_mode="managed_x", auth_value="cookie.txt", max_posts=50,
        )
        self.assertIn("extractor.twitter.text-tweets=true", cmd)
        self.assertIn("--no-download", cmd)
        self.assertEqual(cmd[-1], "https://x.com/i/bookmarks")

    def test_catalog_inbox_and_markdown_persist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            catalog = Catalog(root / "catalog.sqlite3")
            rec = PostRecord("1234567890123456789", "42", "name", "2026-01-02", "2026-02-03", "body", 1)
            self.assertEqual(catalog.upsert_collection_posts("c1", [rec])["added"], 1)
            self.assertEqual(catalog.pending_collection_post_count("c1"), 1)
            path = write_post_markdown(rec, root / "text")
            catalog.set_collection_post_choice("c1", rec.post_id, "text", markdown_path=str(path))
            self.assertEqual(catalog.pending_collection_post_count("c1"), 0)
            self.assertIn("body", path.read_text(encoding="utf-8"))
            catalog.close()


if __name__ == "__main__":
    unittest.main()
