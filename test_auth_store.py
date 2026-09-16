from pathlib import Path
import pickle
import sqlite3
import tempfile
import unittest

from auth_store import (
    BrowserCookie, classify_pixiv_auth_test, classify_x_auth_test,
    import_netscape_cookie_file, inspect_netscape_cookie_file, inspect_pixiv_cache,
    managed_pixiv_cache_path, managed_x_cookie_path, managed_x_web_profile_dir,
    write_netscape_cookie_file,
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
        self.assertIn("--config-ignore", cmd)
        self.assertEqual(cmd[cmd.index("--cookies") + 1], "account.txt")

    def test_auth_probe_classifies_known_failures_without_echoing_output(self):
        secret = "secret-local-path-and-cookie-value"
        ok, message = classify_x_auth_test(
            1, f"[twitter][error] Could not authenticate you {secret}"
        )
        self.assertFalse(ok)
        self.assertIn("Cookie", message)
        self.assertNotIn(secret, message)

        ok, message = classify_x_auth_test(
            1, "[cookies][warning] Failed to decrypt cookie (DPAPI)"
        )
        self.assertFalse(ok)
        self.assertIn("アプリ内Xログイン", message)

        self.assertEqual(
            classify_x_auth_test(0, "bookmark probe completed"),
            (True, "X認証OK。このアカウントで取得できます。"),
        )

    def test_pixiv_cache_is_per_account_and_inspected_without_token_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "日本語 # auth"
            path = managed_pixiv_cache_path(root, "pixiv-one")
            self.assertNotEqual(path, managed_pixiv_cache_path(root, "pixiv-two"))
            self.assertFalse(inspect_pixiv_cache(path)["authenticated"])
            path.parent.mkdir(parents=True)
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE data (key TEXT PRIMARY KEY, value TEXT, expires INTEGER)")
            con.execute(
                "INSERT INTO data VALUES (?,?,?)",
                ("gallery_dl.extractor.pixiv._refresh_token_cache-None",
                 pickle.dumps("SUPER-SECRET-REFRESH-TOKEN"), 0),
            )
            con.commit(); con.close()
            result = inspect_pixiv_cache(path)
            self.assertTrue(result["authenticated"])
            self.assertNotIn("SUPER-SECRET", repr(result))

    def test_pixiv_auth_probe_classifies_invalid_token(self):
        ok, message = classify_pixiv_auth_test(1, "AuthenticationError: Invalid refresh token")
        self.assertFalse(ok)
        self.assertIn("無効", message)
        self.assertTrue(classify_pixiv_auth_test(0, "ok")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
