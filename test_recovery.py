import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery import find_recovery_candidate, restore_candidate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="smc-recovery-test-")
        self.root = Path(self.temp.name)
        self.temp_root = self.root / "Temp"
        self.data = self.root / "SNSMediaCollector"
        self.temp_root.mkdir()
        self.data.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_candidate(self, name="smc-smoke-user"):
        source = self.temp_root / name
        source.mkdir()
        (source / "accounts.json").write_text(json.dumps([
            {"name": "sample", "platform": "x", "profile_id": "account-1"}
        ]), encoding="utf-8")
        (source / "collections.json").write_text(json.dumps({"items": [{
            "name": "images", "account_id": "account-1", "platform": "x",
            "collection_id": "collection-1", "destination": "D:/Original",
        }]}), encoding="utf-8")
        (source / "targets.json").write_text("[]", encoding="utf-8")
        return source

    def test_discovery_requires_real_account_data(self):
        synthetic = self.temp_root / "smc-smoke-synthetic"
        synthetic.mkdir()
        (synthetic / "targets.json").write_text("[]", encoding="utf-8")
        self.assertIsNone(find_recovery_candidate(self.data, self.temp_root))

        source = self.make_candidate()
        found = find_recovery_candidate(self.data, self.temp_root)
        self.assertEqual(found.path, source)
        self.assertEqual(found.account_count, 1)
        self.assertEqual(found.collection_count, 1)

    def test_restore_preserves_original_destination_and_backs_up_current_state(self):
        source = self.make_candidate()
        (self.data / "accounts.json").write_text('[{"name":"current"}]', encoding="utf-8")
        (self.data / "catalog.sqlite3-wal").write_bytes(b"stale journal")
        (source / "Library").mkdir()
        (source / "Library" / "recovered.jpg").write_bytes(b"recovered")
        (source / "auth" / "cookies").mkdir(parents=True)
        (source / "auth" / "cookies" / "x-account-1.txt").write_bytes(b"new auth")
        (self.data / "Library").mkdir()
        (self.data / "Library" / "keep.jpg").write_bytes(b"keep")
        (self.data / "auth" / "cookies").mkdir(parents=True)
        (self.data / "auth" / "cookies" / "x-account-1.txt").write_bytes(b"old auth")

        candidate = find_recovery_candidate(self.data, self.temp_root)
        report = restore_candidate(candidate, self.data)

        collections = json.loads((self.data / "collections.json").read_text(encoding="utf-8"))
        self.assertEqual(collections["items"][0]["destination"], "D:/Original")
        self.assertEqual((self.data / "Library" / "recovered.jpg").read_bytes(), b"recovered")
        self.assertEqual((self.data / "Library" / "keep.jpg").read_bytes(), b"keep")
        self.assertEqual(
            (self.data / "auth" / "cookies" / "x-account-1.txt").read_bytes(), b"new auth"
        )
        self.assertTrue((report.backup / "accounts.json").is_file())
        self.assertTrue((report.backup / "catalog.sqlite3-wal").is_file())
        self.assertEqual(
            (report.backup / "auth" / "cookies" / "x-account-1.txt").read_bytes(), b"old auth"
        )
        self.assertFalse((self.data / "catalog.sqlite3-wal").exists())
        self.assertTrue(source.is_dir())
        self.assertIsNone(find_recovery_candidate(self.data, self.temp_root))

    def test_existing_media_is_never_overwritten(self):
        source = self.make_candidate()
        (source / "Library").mkdir()
        (self.data / "Library").mkdir()
        (source / "Library" / "same.jpg").write_bytes(b"stranded")
        (self.data / "Library" / "same.jpg").write_bytes(b"current")

        report = restore_candidate(find_recovery_candidate(self.data, self.temp_root), self.data)
        self.assertEqual((self.data / "Library" / "same.jpg").read_bytes(), b"current")
        self.assertEqual(report.media_files, 0)
        self.assertEqual(report.media_conflicts, 1)

    def test_failed_restore_rolls_current_state_back(self):
        source = self.make_candidate()
        current = self.data / "accounts.json"
        current.write_bytes(b"current state")

        with patch("recovery._copy_tree", side_effect=OSError("simulated copy failure")):
            with self.assertRaises(OSError):
                restore_candidate(find_recovery_candidate(self.data, self.temp_root), self.data)

        self.assertEqual(current.read_bytes(), b"current state")
        self.assertTrue(source.is_dir())


if __name__ == "__main__":
    unittest.main()
