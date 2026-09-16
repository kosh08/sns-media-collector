from pathlib import Path
import tempfile
import unittest

from auth_store import (
    BrowserCookie, import_netscape_cookie_file, inspect_netscape_cookie_file,
    managed_x_cookie_path, managed_x_web_profile_dir, write_netscape_cookie_file,
)
from core import build_x_likes_probe_command


class AuthStoreTests(unittest.TestCase):
    def test_managed_paths_are_per_account_and_cannot_escape(self):
        root = Path("data")
        first = managed_x_cookie_path(root, "abc-123")
        second = managed_x_cookie_path(root, "def-456")
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, root / "auth" / "cookies")
        self.assertEqual(managed_x_web_profile_dir(root, "abc-123").parent,
                         root / "auth" / "web-profiles")
        with self.assertRaises(ValueError):
            managed_x_cookie_path(root, "../../")

    def test_write_and_inspect_never_exposes_cookie_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "account.txt"
            secret = "VERY-SECRET-AUTH-TOKEN"
            result = write_netscape_cookie_file(path, [
                BrowserCookie(".x.com", "/", True, 2000000000, "auth_token", secret),
                BrowserCookie(".x.com", "/", True, 2000000000, "ct0", "SECRET-CSRF"),
                BrowserCookie("example.com", "/", False, 0, "ignored", "ignored"),
            ])
            self.assertTrue(result["x_auth"])
            self.assertTrue(result["csrf"])
            self.assertEqual(result["cookie_count"], 2)
            self.assertNotIn(secret, repr(result))
            self.assertIn(secret, path.read_text())

    def test_missing_auth_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "auth_token"):
                write_netscape_cookie_file(Path(temporary) / "bad.txt", [
                    BrowserCookie(".x.com", "/", True, 0, "ct0", "csrf"),
                ])

    def test_import_validates_then_copies_to_account_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "export.txt"
            source.write_text(
                "# Netscape HTTP Cookie File\n"
                "#HttpOnly_.x.com\tTRUE\t/\tTRUE\t2000000000\tauth_token\tsecret\n"
                ".x.com\tTRUE\t/\tTRUE\t2000000000\tct0\tcsrf\n",
                encoding="utf-8",
            )
            destination = managed_x_cookie_path(root, "profile-one")
            result = import_netscape_cookie_file(source, destination)
            self.assertTrue(result["x_auth"])
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertTrue(inspect_netscape_cookie_file(destination)["csrf"])

    def test_managed_mode_uses_cookie_file_not_browser_decryption(self):
        cmd = build_x_likes_probe_command(
            ["gallery-dl"], url="https://x.com/example/likes",
            auth_mode="managed_x", auth_value="account.txt", max_media=10,
        )
        self.assertIn("--cookies", cmd)
        self.assertNotIn("--cookies-from-browser", cmd)
        self.assertEqual(cmd[cmd.index("--cookies") + 1], "account.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
