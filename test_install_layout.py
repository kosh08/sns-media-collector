from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from install_layout import APP_EXE, cleanup_old_versions, latest_executable, version_key


class InstallLayoutTests(unittest.TestCase):
    def make_version(self, root: Path, version: str) -> Path:
        folder = root / version
        folder.mkdir()
        (folder / APP_EXE).write_bytes(b"test")
        return folder

    def test_latest_version_uses_numeric_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "versions"
            root.mkdir()
            self.make_version(root, "0.3.9")
            expected = self.make_version(root, "0.3.12") / APP_EXE
            self.make_version(root, "0.3.10")
            self.assertEqual(latest_executable(root), expected)
            self.assertEqual(version_key("10.2.30"), (10, 2, 30))

    def test_cleanup_keeps_current_and_one_rollback_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "versions"
            root.mkdir()
            for version in ("0.3.8", "0.3.9", "0.3.10", "0.3.12"):
                self.make_version(root, version)
            unrelated = root / "manual-backup"
            unrelated.mkdir()
            (unrelated / "note.txt").write_text("keep me", encoding="utf-8")

            result = cleanup_old_versions(root, "0.3.12")

            self.assertEqual(set(result["kept"]), {"0.3.12", "0.3.10"})
            self.assertEqual(set(result["removed"]), {"0.3.8", "0.3.9"})
            self.assertTrue(unrelated.is_dir())

    def test_cleanup_rejects_broad_or_invalid_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                cleanup_old_versions(root, "0.3.12")
            versions = root / "versions"
            versions.mkdir()
            with self.assertRaises(ValueError):
                cleanup_old_versions(versions, "../outside")


if __name__ == "__main__":
    unittest.main()
