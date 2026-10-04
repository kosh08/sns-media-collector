"""Bounded asynchronous image and metadata loading for the review inbox."""
from collections import OrderedDict, deque
from pathlib import Path

from PySide6.QtCore import (QObject, Signal, QUrl, QSize, QByteArray, QBuffer,
                           QIODevice, QProcess, QProcessEnvironment, QTimer, Qt)
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply, QNetworkDiskCache
from PySide6.QtWidgets import QPushButton, QDialog, QVBoxLayout, QLabel

from core import parse_smc_post_line
from post_previews import preview_image_url


class PreviewImageLoader(QObject):
    ready = Signal(str, QPixmap)
    failed = Signal(str)
    MAX_BYTES = 2 * 1024 * 1024
    MAX_IMAGE_CACHE_BYTES = 32 * 1024 * 1024

    def __init__(self, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        cache = QNetworkDiskCache(self.manager)
        cache.setCacheDirectory(str(cache_dir))
        cache.setMaximumCacheSize(128 * 1024 * 1024)
        self.manager.setCache(cache)
        self.images = OrderedDict()
        self.pending = deque()
        self.active = {}

    @staticmethod
    def decode(data, max_size=QSize(1200, 1000)):
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > 20_000_000:
            return QPixmap()
        reader.setScaledSize(size.scaled(max_size, Qt.KeepAspectRatio))
        return QPixmap.fromImage(reader.read())

    def request(self, url, size="small"):
        key = preview_image_url(url, size)
        if not key:
            self.failed.emit(url)
            return
        if key in self.images:
            self.images.move_to_end(key)
            self.ready.emit(key, self.images[key])
            return
        if key not in self.active and key not in self.pending:
            self.pending.append(key)
        self._pump()

    def _pump(self):
        while self.pending and len(self.active) < 2:
            key = self.pending.popleft()
            request = QNetworkRequest(QUrl(key))
            request.setTransferTimeout(15000)
            request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
            request.setAttribute(QNetworkRequest.CacheLoadControlAttribute, QNetworkRequest.PreferCache)
            reply = self.manager.get(request)
            self.active[key] = (reply, bytearray())
            reply.setReadBufferSize(self.MAX_BYTES + 1)
            reply.readyRead.connect(lambda k=key: self._read(k))
            reply.finished.connect(lambda k=key: self._finish(k))

    def _read(self, key):
        state = self.active.get(key)
        if state:
            reply, data = state
            if len(data) > self.MAX_BYTES:
                return
            data.extend(bytes(reply.read(self.MAX_BYTES + 1 - len(data))))
            if len(data) > self.MAX_BYTES:
                reply.abort()

    def _finish(self, key):
        self._read(key)
        state = self.active.pop(key, None)
        if not state:
            return
        reply, data = state
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        max_size = QSize(1200, 1000) if "name=medium" in key else QSize(640, 480)
        pixmap = (self.decode(data, max_size) if reply.error() == QNetworkReply.NoError
                  and status == 200 and len(data) <= self.MAX_BYTES else QPixmap())
        reply.deleteLater()
        if pixmap.isNull():
            self.failed.emit(key)
        else:
            self.images[key] = pixmap
            while (len(self.images) > 48 or
                   sum(p.width() * p.height() * max(1, p.depth() // 8) for p in self.images.values())
                   > self.MAX_IMAGE_CACHE_BYTES):
                self.images.popitem(last=False)
            self.ready.emit(key, pixmap)
        self._pump()

    def stop(self):
        self.pending.clear()
        states = list(self.active.values())
        self.active.clear()
        for reply, _data in states:
            reply.readyRead.disconnect()
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()


class PreviewTile(QPushButton):
    def __init__(self, preview, loader, parent=None):
        super().__init__(parent)
        self.preview = preview
        self.loader = loader
        self.requested = False
        self.loaded = False
        self.pixmap = QPixmap()
        self.badge = {"image": "画像", "video": "動画", "gif": "GIF"}[preview.kind]
        self.setMinimumWidth(80)
        self.setFixedHeight(155)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(3)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.caption = QLabel()
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.image_label, 1)
        layout.addWidget(self.caption)
        self.setText(self.badge + " · 読み込み待ち")
        loader.ready.connect(self._ready)
        loader.failed.connect(self._failed)
        self.clicked.connect(self._open)

    def load(self):
        if not self.requested:
            self.requested = True
            self.setText(self.badge + " · 読み込み中…")
            self.loader.request(self.preview.url)

    def _ready(self, key, pixmap):
        if key == self.preview.url:
            self.loaded = True
            self.pixmap = pixmap.scaled(QSize(480, 112), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._scale_image()
            self.setText(self.badge + " · 拡大")

    def setText(self, text):
        self.caption.setText(text)
        self.setAccessibleName(text)

    def text(self):
        return self.caption.text()

    def _scale_image(self):
        if not self.pixmap.isNull():
            self.image_label.setPixmap(self.pixmap.scaled(QSize(max(20, self.width() - 20), 112),
                                                        Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale_image()

    def _failed(self, key):
        if key == self.preview.url:
            self.requested = False
            self.setText(self.badge + " · 読み込み失敗（押して再試行）")

    def _open(self):
        if not self.loaded:
            self.load()
            return
        viewer = QDialog(self.window())
        viewer.setAttribute(Qt.WA_DeleteOnClose)
        viewer.setWindowTitle(self.badge + "プレビュー")
        viewer.resize(850, 700)
        layout = QVBoxLayout(viewer)
        label = QLabel("拡大画像を読み込み中…")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label, 1)
        note = QLabel("動画・GIFの再生は「元投稿を開く」から確認できます。")
        note.setVisible(self.preview.kind != "image")
        layout.addWidget(note)
        key = preview_image_url(self.preview.url, "medium")
        # A QObject receiver disconnects automatically when the viewer closes.
        class Receiver(QObject):
            def ready(receiver, url, pixmap):
                if url == key:
                    label.setPixmap(pixmap.scaled(QSize(800, 600), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            def failed(receiver, url):
                if url == key:
                    label.setText("拡大画像を読み込めませんでした。元投稿で確認できます。")
        receiver = Receiver(viewer)
        self.loader.ready.connect(receiver.ready)
        self.loader.failed.connect(receiver.failed)
        viewer.show()
        self.loader.request(key, "medium")


class LegacyPreviewResolver(QObject):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, command_for_record, parent=None):
        super().__init__(parent)
        self.command = command_for_record
        self.queue = deque()
        self.attempted = set()
        self.current = None
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(env)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(30000)
        self.timer.timeout.connect(self.process.kill)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finish)
        self.process.errorOccurred.connect(self._error)
        self.data = bytearray()

    def request(self, record):
        if self.command and record.post_id not in self.attempted:
            self.attempted.add(record.post_id)
            self.queue.append(record)
            self._pump()

    def _pump(self):
        if self.current is not None or not self.queue:
            return
        self.current = self.queue.popleft()
        self.data.clear()
        try:
            command = self.command(self.current)
            self.process.start(command[0], command[1:])
            self.timer.start()
        except Exception:
            self._finish(-1)

    def _read(self):
        self.data.extend(bytes(self.process.readAllStandardOutput()))
        if len(self.data) > 1024 * 1024:
            self.data.clear()
            self.process.kill()

    def _error(self, error):
        if error == QProcess.FailedToStart:
            self._finish(-1)

    def _finish(self, code, *_args):
        self.timer.stop()
        if self.current is None:
            return
        self._read()
        expected = self.current
        self.current = None
        record = None
        if code == 0:
            for line in self.data.decode("utf-8", errors="replace").splitlines():
                parsed = parse_smc_post_line(line)
                if parsed and parsed.post_id == expected.post_id and parsed.previews:
                    record = parsed
                    break
        if record:
            self.ready.emit(record)
        else:
            self.failed.emit(expected.post_id)
        QTimer.singleShot(0, self._pump)

    def stop(self):
        self.timer.stop()
        self.queue.clear()
        self.attempted.clear()
        self.current = None
        self.process.kill()
        self.process.waitForFinished(1000)
