from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from updater_core import download_update, select_update, version_tuple


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        if self.offset >= len(self.payload):
            return b""
        end = len(self.payload) if size < 0 else min(len(self.payload), self.offset + size)
        block = self.payload[self.offset:end]
        self.offset = end
        return block


class UpdaterTests(unittest.TestCase):
    def release(self, version="0.3.2", digest=None):
        digest = digest or ("a" * 64)
        return {
            "tag_name": f"v{version}", "draft": False, "prerelease": False,
            "body": "fixes", "assets": [{
                "name": f"SNSMediaCollector-Setup-{version}.exe",
                "browser_download_url": f"https://github.com/kosh08/sns-media-collector/releases/download/v{version}/setup.exe",
                "digest": f"sha256:{digest}",
            }],
        }

    def test_version_order_and_release_selection(self):
        self.assertLess(version_tuple("0.3.1"), version_tuple("v0.3.2"))
        self.assertEqual(select_update(self.release(), "0.3.1")["version"], "0.3.2")
        self.assertIsNone(select_update(self.release(), "0.3.2"))

    def test_release_requires_signed_digest(self):
        release = self.release(); release["assets"][0]["digest"] = None
        with self.assertRaises(ValueError):
            select_update(release, "0.3.1")

    def test_download_verifies_sha256_before_replacing(self):
        payload = b"verified installer bytes"
        info = select_update(self.release(digest=hashlib.sha256(payload).hexdigest()), "0.3.1")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / info["name"]
            with patch("updater_core.urllib.request.urlopen", return_value=FakeResponse(payload)):
                self.assertEqual(download_update(info, path).read_bytes(), payload)
            bad = dict(info, sha256="0" * 64)
            with patch("updater_core.urllib.request.urlopen", return_value=FakeResponse(payload)):
                with self.assertRaises(ValueError):
                    download_update(bad, path)
            self.assertEqual(path.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
