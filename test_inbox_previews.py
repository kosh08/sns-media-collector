"""Offline Qt tests: real cards and processes, with no real account/network."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal, QByteArray, QBuffer, QIODevice, QProcess, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtNetwork import QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton
from app import ReviewInboxDialog
from collection_profiles import CollectionProfile
from core import Catalog, PostRecord
from inbox_previews import PreviewImageLoader, PreviewTile, LegacyPreviewResolver
from post_previews import PostPreview, preview_image_url

APP = QApplication.instance() or QApplication([])
PREVIEW = PostPreview(preview_image_url("https://pbs.twimg.com/media/sample.jpg"))


def wait_until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        APP.processEvents()
        time.sleep(.01)
    return condition()


class FakeReply(QObject):
    readyRead = Signal()
    finished = Signal()
    def __init__(self):
        super().__init__()
        self.data = b""
        self.aborted = False
    def setReadBufferSize(self, _size):
        pass
    def read(self, size):
        value, self.data = self.data[:size], self.data[size:]
        return QByteArray(value)
    def error(self):
        return QNetworkReply.OperationCanceledError if self.aborted else QNetworkReply.NoError
    def attribute(self, _name):
        return 200
    def abort(self):
        self.aborted = True
        self.finished.emit()


class InboxPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="smc-regression-preview-")
        self.root = Path(self.temp.name)
        self.catalog = Catalog(self.root / "catalog.db")
        self.collection = CollectionProfile("Preview", "account", "x", collection_id="c")
        self.dialogs = []
    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
            dialog.deleteLater()
        APP.processEvents()
        self.catalog.close()
        self.temp.cleanup()
    def dialog(self, records, **kwargs):
        self.catalog.upsert_collection_posts("c", records)
        dialog = ReviewInboxDialog(self.collection, self.catalog, **kwargs)
        self.dialogs.append(dialog)
        return dialog

    def test_visible_cards_only_load_images_and_keep_selection(self):
        records = [PostRecord(str(1000+i), "42", "name", "2026-01-01", content="<b>plain text</b>",
                              media_count=1, previews=(PREVIEW,)) for i in range(100)]
        dialog = self.dialog(records)
        with patch.object(dialog.preview_loader, "request") as request:
            dialog.show()
            self.assertTrue(wait_until(lambda: request.call_count > 0))
            self.assertLess(request.call_count, 10)
            first = dialog.current_records[0]
            selector = dialog.selectors[first.post_id]
            selector.setCurrentIndex(selector.findData("skip"))
            pixmap = QPixmap(30, 20)
            pixmap.fill(Qt.blue)
            dialog.preview_loader.ready.emit(PREVIEW.url, pixmap)
            self.assertEqual(selector.currentData(), "skip")
            self.assertEqual(dialog._dirty_post_ids, {first.post_id})
            labels = dialog.findChildren(QLabel)
            body = next(label for label in labels if label.text() == "<b>plain text</b>")
            self.assertEqual(body.textFormat(), Qt.PlainText)
            dialog.preview_loader.failed.emit(PREVIEW.url)
            tile = dialog.preview_rows[first.post_id][2][0]
            self.assertIn("失敗", tile.text())

    def test_metadata_completion_preserves_body_and_draft_and_only_updates_previews(self):
        record = PostRecord("123", author_name="name", content="keep body", media_count=1)
        dialog = self.dialog([record], preview_command=lambda _r: [sys.executable, "-c", "pass"])
        selector = dialog.selectors["123"]
        selector.setCurrentIndex(selector.findData("text"))
        before = self.catalog.conn.execute("SELECT * FROM collection_posts").fetchone()
        dialog._preview_metadata_ready(replace(record, content="changed remote body", previews=(PREVIEW,)))
        after = self.catalog.conn.execute("SELECT * FROM collection_posts").fetchone()
        self.assertEqual(after[:-1], before[:-1])
        self.assertEqual(dialog.choices()["123"], "text")
        self.assertEqual(dialog.records_for_choices()[0].content, "keep body")
        self.assertEqual(dialog.records_for_choices()[0].previews, (PREVIEW,))

    def test_text_only_and_missing_preview_fallbacks(self):
        dialog = self.dialog([PostRecord("123", content="body"), PostRecord("124", media_count=1)])
        self.assertEqual(dialog.findChildren(PreviewTile), [])
        text = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertIn("本文のみの投稿", text)
        self.assertTrue(any("元投稿" in value for value in text))

    def test_long_body_expands_and_collapses(self):
        content = "本文" * 400
        dialog = self.dialog([PostRecord("123", content=content)])
        button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "全文を表示")
        button.click()
        self.assertTrue(any(l.text() == content for l in dialog.findChildren(QLabel)))
        button.click()
        self.assertEqual(button.text(), "全文を表示")

    def test_resolver_real_process_matching_id_and_failure(self):
        record = PostRecord("123", media_count=1)
        def command(_record):
            line = 'SMC_POST\t123\t42\t"name"\t2026-01-01\t\t"body"\t1\t' + json.dumps([{"url": PREVIEW.url}])
            return [sys.executable, "-c", "print(" + repr(line) + ")"]
        resolver = LegacyPreviewResolver(command)
        results, failures = [], []
        resolver.ready.connect(results.append)
        resolver.failed.connect(failures.append)
        resolver.request(record)
        self.assertTrue(wait_until(lambda: bool(results)))
        self.assertEqual(results[0].previews, (PREVIEW,))
        resolver.request(PostRecord("999", media_count=1))
        self.assertTrue(wait_until(lambda: bool(failures)))
        self.assertEqual(failures, ["999"])
        resolver.stop()

    def test_dialog_close_cancels_pending_metadata_process(self):
        dialog = self.dialog([PostRecord("123", media_count=1)],
                             preview_command=lambda _r: [sys.executable, "-c", "import time;time.sleep(30)"])
        dialog.show()
        self.assertTrue(wait_until(lambda: dialog.preview_resolver.current is not None))
        dialog.reject()
        self.assertIsNone(dialog.preview_resolver.current)
        self.assertEqual(dialog.preview_resolver.process.state(), QProcess.NotRunning)

    def test_image_network_is_bounded_rejects_redirects_and_aborts_oversize(self):
        loader = PreviewImageLoader(self.root / "cache")
        replies, requests, failures = [], [], []
        def get(request):
            reply = FakeReply()
            requests.append(request)
            replies.append(reply)
            return reply
        loader.failed.connect(failures.append)
        with patch.object(loader.manager, "get", get):
            loader.request("https://example.com/secret.jpg")
            self.assertEqual(len(requests), 0)
            for i in range(6):
                loader.request(f"https://pbs.twimg.com/media/{i}.jpg")
            self.assertEqual(len(replies), 2)
            self.assertEqual(len(loader.pending), 4)
            self.assertEqual(requests[0].attribute(QNetworkRequest.RedirectPolicyAttribute), QNetworkRequest.ManualRedirectPolicy)
            replies[0].data = b"x" * (loader.MAX_BYTES + 1)
            replies[0].readyRead.emit()
            self.assertTrue(replies[0].aborted)
            self.assertEqual(len(loader.active), 2)
            loader.stop()
            self.assertFalse(loader.active)
            self.assertFalse(loader.pending)
            self.assertTrue(all(r.aborted for r in replies))

    def test_decode_rejects_non_image_and_bounds_valid_image(self):
        self.assertTrue(PreviewImageLoader.decode(b"not an image").isNull())
        image = QImage(1800, 1200, QImage.Format_RGB32)
        image.fill(Qt.red)
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        pixmap = PreviewImageLoader.decode(bytes(buffer.data()))
        self.assertFalse(pixmap.isNull())
        self.assertLessEqual(pixmap.width(), 1200)
        self.assertLessEqual(pixmap.height(), 1000)

    def test_video_preview_opens_enlargement_and_closes_without_late_callbacks(self):
        preview = replace(PREVIEW, kind="video")
        dialog = self.dialog([PostRecord("123", media_count=1, previews=(preview,))])
        pixmap = QPixmap(200, 100)
        pixmap.fill(Qt.blue)
        dialog.preview_loader.images[PREVIEW.url] = pixmap
        dialog.preview_loader.images[preview_image_url(PREVIEW.url, "medium")] = pixmap
        tile = dialog.preview_rows["123"][2][0]
        tile.load()
        tile.click()
        viewer = dialog.findChildren(QDialog)[0]
        self.assertIn("動画", viewer.windowTitle())
        self.assertTrue(any(not label.pixmap().isNull() for label in viewer.findChildren(QLabel)))
        viewer.close()
        APP.processEvents()
        dialog.preview_loader.failed.emit(preview_image_url(PREVIEW.url, "medium"))

    def test_old_page_metadata_is_ignored_and_cross_page_choice_is_retained(self):
        records = [PostRecord(str(1000+i), media_count=1) for i in range(101)]
        dialog = self.dialog(records)
        record = dialog.current_records[0]
        selector = dialog.selectors[record.post_id]
        selector.setCurrentIndex(selector.findData("skip"))
        dialog.next_page()
        dialog._preview_metadata_ready(replace(record, previews=(PREVIEW,)))
        self.assertNotIn(record.post_id, dialog.preview_rows)
        dialog.previous_page()
        self.assertEqual(dialog.selectors[record.post_id].currentData(), "skip")
