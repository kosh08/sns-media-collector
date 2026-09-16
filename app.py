from __future__ import annotations

import json
import codecs
import uuid
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QSettings, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QPixmap

from core import (
    Catalog, LikeSeenRecord, TargetProfile, TargetStore, append_urls, archive_path_for, build_command as core_build_command,
    build_x_likes_anchor_command, build_x_likes_probe_command, find_likes_anchor_boundary,
    import_hitomi_x_archive, import_x_likes_seen_archive, incremental_baseline, iso_utc,
    likes_post_urls, normalize_target, parse_iso, parse_smc_file_line, parse_smc_like_seen_line, parse_smc_x_meta_line,
    redact_command, select_likes_anchor_records, snapshot_media_files, new_media_since_snapshot, normalized_local_path,
    MEDIA_EXTENSIONS, safe_to_advance_download_state, utc_now, atomic_write_json, partition_likes_records,
)

from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QInputDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QProgressBar,
    QRadioButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget,
    QTextEdit, QToolButton, QVBoxLayout, QWidget
)

APP_NAME = "SNS Media Collector"
APP_VERSION = "0.3.1"


DARK_QSS = r"""
QWidget { background: #0f141d; color: #e9eef7; font-size: 13px; }
QMainWindow { background: #0b1018; }
QFrame#card { background: #151c27; border: 1px solid #263246; border-radius: 10px; }
QFrame#sidebar { background: #101722; border-right: 1px solid #273246; }
QFrame#thumb { background: #101722; border: 1px solid #263246; border-radius: 8px; }
QFrame#thumb:hover { border: 1px solid #4b83f5; }
QLabel#title { font-size: 20px; font-weight: 700; }
QLabel#muted { color: #8f9bb0; }
QLabel#section { font-weight: 700; font-size: 14px; }
QLineEdit, QComboBox, QTextEdit, QSpinBox {
    background: #0f1621; border: 1px solid #2a3850; border-radius: 6px;
    padding: 7px; selection-background-color: #2d6cdf;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus { border: 1px solid #4b83f5; }
QPushButton, QToolButton {
    background: #202a3a; border: 1px solid #31405a; border-radius: 6px;
    padding: 7px 12px;
}
QPushButton:hover, QToolButton:hover { background: #29364a; }
QPushButton#primary { background: #2d6cdf; border: 1px solid #427ef0; font-weight: 700; }
QPushButton#primary:hover { background: #3b79ea; }
QPushButton#danger { background: #672f39; border: 1px solid #8a3f4c; }
QPushButton:disabled { color: #69758a; background: #171e29; border-color: #263246; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { padding: 9px; border-radius: 6px; }
QListWidget::item:selected { background: #244b8e; }
QCheckBox, QRadioButton { spacing: 7px; }
QProgressBar { border: 1px solid #2a3850; border-radius: 5px; text-align: center; background: #0f1621; }
QProgressBar::chunk { background: #2d6cdf; border-radius: 4px; }
QSplitter::handle { background: #0b1018; }
QScrollBar:vertical { background: #101722; width: 10px; margin: 0px; }
QScrollBar::handle:vertical { background: #35445f; min-height: 28px; border-radius: 5px; }
"""


@dataclass
class AccountProfile:
    name: str
    platform: str  # x | pixiv
    auth_mode: str = "none"  # none | cookies_file | browser | pixiv_token
    auth_value: str = ""
    profile_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class AccountDialog(QDialog):
    def __init__(self, parent=None, profile: Optional[AccountProfile] = None):
        super().__init__(parent)
        self.profile_id = profile.profile_id if profile else uuid.uuid4().hex
        self.setWindowTitle("アカウント追加 / 編集")
        self.resize(520, 260)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(profile.name if profile else "")
        self.platform = QComboBox()
        self.platform.addItem("X / Twitter", "x")
        self.platform.addItem("pixiv", "pixiv")
        self.auth_mode = QComboBox()
        self.auth_mode.addItem("認証なし", "none")
        self.auth_mode.addItem("cookies.txt", "cookies_file")
        self.auth_mode.addItem("ブラウザCookie", "browser")
        self.auth_mode.addItem("pixiv refresh token", "pixiv_token")
        self.auth_value = QLineEdit(profile.auth_value if profile else "")
        self.auth_value.setPlaceholderText("cookies.txt のパス / firefox / chrome / edge / pixiv refresh token")
        self.auth_value.setEchoMode(QLineEdit.EchoMode.Password if (profile and profile.auth_mode == "pixiv_token") else QLineEdit.EchoMode.Normal)
        self.auth_mode.currentIndexChanged.connect(self._auth_mode_changed)
        browse = QPushButton("参照")
        browse.clicked.connect(self.pick_cookie)
        row = QHBoxLayout(); row.addWidget(self.auth_value); row.addWidget(browse)
        form.addRow("表示名", self.name_edit)
        form.addRow("サービス", self.platform)
        form.addRow("認証方式", self.auth_mode)
        form.addRow("認証値", row)
        layout.addLayout(form)
        note = QLabel("X: cookies.txt / ブラウザCookie。pixiv: refresh tokenを直接登録できます（画面上は伏字）。")
        note.setObjectName("muted"); note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout(); buttons.addStretch()
        cancel = QPushButton("キャンセル"); ok = QPushButton("保存"); ok.setObjectName("primary")
        cancel.clicked.connect(self.reject); ok.clicked.connect(self.accept)
        buttons.addWidget(cancel); buttons.addWidget(ok); layout.addLayout(buttons)
        if profile:
            self.platform.setCurrentIndex(max(0, self.platform.findData(profile.platform)))
            self.auth_mode.setCurrentIndex(max(0, self.auth_mode.findData(profile.auth_mode)))

    def _auth_mode_changed(self):
        is_token = self.auth_mode.currentData() == "pixiv_token"
        self.auth_value.setEchoMode(QLineEdit.EchoMode.Password if is_token else QLineEdit.EchoMode.Normal)

    def pick_cookie(self):
        path, _ = QFileDialog.getOpenFileName(self, "cookies.txt を選択", "", "Cookie file (*.txt);;All files (*.*)")
        if path:
            self.auth_value.setText(path)
            self.auth_mode.setCurrentIndex(self.auth_mode.findData("cookies_file"))

    def result_profile(self) -> AccountProfile:
        return AccountProfile(
            profile_id=self.profile_id,
            name=self.name_edit.text().strip() or "未設定",
            platform=self.platform.currentData(),
            auth_mode=self.auth_mode.currentData(),
            auth_value=self.auth_value.text().strip(),
        )


class MediaThumbnail(QFrame):
    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = dict(info)
        self.path = Path(str(info.get("path") or ""))
        self.setObjectName("thumb")
        self.setFixedWidth(166)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(str(self.path))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)
        self.preview = QLabel()
        self.preview.setFixedSize(150, 120)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background:#0b1018; border-radius:5px; color:#7f8ca3;")
        layout.addWidget(self.preview)

        name = self.path.name or "(unknown)"
        self.name_label = QLabel(name)
        self.name_label.setWordWrap(False)
        self.name_label.setToolTip(name)
        layout.addWidget(self.name_label)

        detail_parts = []
        if info.get("author_name"):
            detail_parts.append("@" + str(info["author_name"]).lstrip("@"))
        elif info.get("target_name"):
            detail_parts.append(str(info["target_name"]))
        if info.get("platform"):
            detail_parts.append(str(info["platform"]).upper())
        detail = QLabel(" / ".join(detail_parts) if detail_parts else "取得済み")
        detail.setObjectName("muted")
        layout.addWidget(detail)
        self._load_preview()

    def _load_preview(self):
        suffix = self.path.suffix.lower()
        if self.path.exists() and suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif"}:
            pix = QPixmap(str(self.path))
            if not pix.isNull():
                self.preview.setPixmap(pix.scaled(
                    self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                ))
                return
        if suffix in {".mp4", ".webm", ".m4v", ".mov", ".mkv"}:
            self.preview.setText("▶ VIDEO\n" + suffix.lstrip(".").upper())
        else:
            self.preview.setText(suffix.lstrip(".").upper() or "MEDIA")

    def mouseDoubleClickEvent(self, event):
        if self.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        open_file = menu.addAction("開く")
        reveal = menu.addAction("エクスプローラーでこのファイルを選択")
        open_folder = menu.addAction("フォルダを開く")
        source = None
        if self.info.get("source_url"):
            source = menu.addAction("元投稿を開く")
        chosen = menu.exec(event.globalPos())
        if chosen == open_file and self.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))
        elif chosen == reveal:
            reveal_path_in_file_manager(self.path)
        elif chosen == open_folder:
            folder = self.path.parent if self.path.parent.exists() else Path.home()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        elif source is not None and chosen == source:
            QDesktopServices.openUrl(QUrl(str(self.info.get("source_url"))))


def reveal_path_in_file_manager(path: Path | str) -> None:
    p = Path(path).expanduser()
    try:
        if sys.platform.startswith("win") and p.exists():
            import subprocess
            subprocess.Popen(["explorer.exe", "/select,", str(p)])
            return
    except Exception:
        pass
    folder = p.parent if p.parent.exists() else p
    if folder.exists():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


class DownloadJob(QWidget):
    finished = Signal(object, int)
    file_saved = Signal(object, str)

    def __init__(self, title: str, command: list[str], parent=None):
        super().__init__(parent)
        self.title = title
        self.command = command
        self.process = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUTF8", "1")
        self.process.setProcessEnvironment(env)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.completed = False
        self.cancelled = False
        self.verification_error = ""
        self.output_lines: list[str] = []
        self.machine_lines: list[str] = []
        self._stdout_buffer = ""
        self._stdout_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.stop_on_post_ids: set[str] = set()
        self.anchor_hit: str = ""
        self._anchor_stop_requested = False
        self._rate_limit_until: Optional[datetime] = None
        self._rate_timer = QTimer(self)
        self._rate_timer.setInterval(1000)
        self._rate_timer.timeout.connect(self._update_rate_limit_status)
        # v0.1.9: a gallery-dl output event is only a report, not proof.
        # Count a save only after the file actually exists and was absent before this job.
        self.files_saved_count = 0
        self.seen_media_count = 0
        self.meta_media_count = 0
        self.reported_file_paths: list[str] = []
        self.verified_file_paths: list[str] = []
        self.missing_reported_paths: list[str] = []
        self.preexisting_media: dict[str, int] = {}
        self.verification_root: Optional[Path] = None
        self.verification_recursive = False

        layout = QVBoxLayout(self); layout.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        self.job_preview = QLabel()
        self.job_preview.setFixedSize(62, 62)
        self.job_preview.setAlignment(Qt.AlignCenter)
        self.job_preview.setStyleSheet("background:#0b1018;border:1px solid #263246;border-radius:5px;color:#75829a;")
        self.job_preview.setText("…")
        top.addWidget(self.job_preview)
        info = QVBoxLayout()
        self.title_label = QLabel(title); self.title_label.setStyleSheet("font-weight:700;")
        self.title_label.setWordWrap(True)
        self.status = QLabel("待機中"); self.status.setObjectName("muted"); self.status.setWordWrap(True)
        info.addWidget(self.title_label); info.addWidget(self.status)
        top.addLayout(info, 1)
        layout.addLayout(top)
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide()
        layout.addWidget(self.progress)

    def start(self):
        self.status.setText("実行中")
        self.progress.show()
        ctx = getattr(self, "smc_context", {})
        dest = str(ctx.get("destination") or "").strip()
        if dest and ctx.get("job_kind") not in {"likes_probe", "likes_anchor"}:
            try:
                self.verification_root = Path(dest).expanduser()
                self.verification_root.mkdir(parents=True, exist_ok=True)
                self.verification_recursive = not bool(ctx.get("direct_folder", True))
                self.preexisting_media = snapshot_media_files(
                    self.verification_root, recursive=self.verification_recursive
                )
            except Exception as exc:
                self.verification_error = str(exc)
                self.output_lines.append(f"[SAVE CHECK ERROR] {exc}")
                self._finished(-2, QProcess.NormalExit)
                return
        self.process.start(self.command[0], self.command[1:])

    def _process_error(self, error):
        if error == QProcess.FailedToStart:
            self.output_lines.append(f"[START ERROR] {self.process.errorString()}")
            self._finished(-1, QProcess.NormalExit)

    def stop(self):
        self.cancelled = True
        if self.process.state() != QProcess.NotRunning:
            self.status.setText("停止中…")
            self.process.terminate()
            QTimer.singleShot(1500, self._kill_cancelled)

    def _kill_cancelled(self):
        if self.cancelled and self.process.state() != QProcess.NotRunning:
            self.process.kill()

    def _set_rate_limit(self, line: str) -> bool:
        m = re.search(r"Waiting for .*?until\s+(\d{1,2}:\d{2}:\d{2}).*?rate limit", line, re.I)
        if not m:
            return False
        try:
            now = datetime.now().astimezone()
            h, mi, sec = (int(x) for x in m.group(1).split(":"))
            target = now.replace(hour=h, minute=mi, second=sec, microsecond=0)
            if target < now:
                from datetime import timedelta
                target += timedelta(days=1)
            self._rate_limit_until = target
            if not self._rate_timer.isActive():
                self._rate_timer.start()
            self._update_rate_limit_status()
        except Exception:
            self.status.setText(line.strip()[-120:])
        return True

    def _update_rate_limit_status(self):
        if not self._rate_limit_until:
            self._rate_timer.stop()
            return
        now = datetime.now().astimezone()
        remaining = max(0, int((self._rate_limit_until - now).total_seconds()))
        if remaining <= 0:
            self.status.setText("Xレート制限解除待ち… 再開処理中")
            self._rate_timer.stop()
            return
        mm, ss = divmod(remaining, 60)
        hh, mm = divmod(mm, 60)
        left = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        self.status.setText(
            f"Xレート制限中 / 再開予定 {self._rate_limit_until.strftime('%H:%M:%S')} / あと {left}"
        )

    def _request_anchor_stop(self):
        if self._anchor_stop_requested or self.process.state() == QProcess.NotRunning:
            return
        self._anchor_stop_requested = True
        self.status.setText(f"アンカー到達 ({self.anchor_hit}) / 差分確認完了")
        self.process.terminate()
        QTimer.singleShot(1200, self._kill_if_still_running)

    def _kill_if_still_running(self):
        if self._anchor_stop_requested and self.process.state() != QProcess.NotRunning:
            self.process.kill()

    def _show_file_preview(self, path_text: str):
        path = Path(path_text)
        suffix = path.suffix.lower()
        if path.exists() and suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif"}:
            pix = QPixmap(str(path))
            if not pix.isNull():
                self.job_preview.setPixmap(pix.scaled(
                    self.job_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
                ))
                self.job_preview.setToolTip(str(path))
                return
        self.job_preview.setPixmap(QPixmap())
        self.job_preview.setText("VIDEO" if suffix in {".mp4", ".webm", ".m4v", ".mov", ".mkv"} else "MEDIA")
        self.job_preview.setToolTip(str(path))

    def _resolve_reported_path(self, path_text: str) -> Path:
        raw = str(path_text).strip().strip('"')
        p = Path(raw).expanduser()
        if p.exists():
            return p
        if self.verification_root is not None and not p.is_absolute():
            candidate = self.verification_root / p
            if candidate.exists():
                return candidate
        return p

    def _verify_file_path(self, path_text: str) -> bool:
        path = self._resolve_reported_path(path_text)
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
            if path.suffix.lower() not in MEDIA_EXTENSIONS:
                return False
            if self.verification_root is None:
                return False
            try:
                path.resolve().relative_to(self.verification_root.resolve())
            except ValueError:
                return False
            key = normalized_local_path(path)
            if key in self.preexisting_media:
                # Existing Hitomi/gallery-dl file; never count this as a new save.
                return False
            if key in {normalized_local_path(x) for x in self.verified_file_paths}:
                return True
            self.verified_file_paths.append(str(path))
            self.files_saved_count = len(self.verified_file_paths)
            self.status.setText(f"ダウンロード中 / 実在確認 {self.files_saved_count:,}件")
            self._show_file_preview(str(path))
            self.file_saved.emit(self, str(path))
            return True
        except OSError:
            return False

    def _retry_verify_reported(self, path_text: str, attempt: int = 0):
        if self.completed:
            return
        if self._verify_file_path(path_text):
            return
        if attempt < 3:
            QTimer.singleShot((150, 500, 1200)[attempt], lambda: self._retry_verify_reported(path_text, attempt + 1))

    def _reconcile_actual_files(self):
        if self.verification_root is None:
            return
        try:
            # Only attribute files explicitly reported by this engine to this job.
            for path in self.reported_file_paths:
                self._verify_file_path(path)
            recovered_count = 0

            # A frozen gallery-dl process can write console paths using the
            # Windows ANSI code page even when PYTHONIOENCODING is set.  That
            # corrupts Japanese directory names before they reach Qt.  For a
            # Likes job, recover by scanning only files created during this
            # job and accepting only filenames containing a Tweet ID that the
            # immediately preceding probe selected.
            ctx = getattr(self, "smc_context", {})
            if (ctx.get("job_kind") == "likes_download" and
                    len(self.verified_file_paths) < len(set(self.reported_file_paths))):
                expected_ids = {
                    str(getattr(rec, "post_id", "") or "")
                    for rec in (ctx.get("probe_new_records") or [])
                }
                expected_ids.discard("")
                if expected_ids:
                    verified_before_recovery = len(self.verified_file_paths)
                    actual_new = new_media_since_snapshot(
                        self.preexisting_media,
                        self.verification_root,
                        recursive=self.verification_recursive,
                    )
                    for candidate in actual_new:
                        if any(post_id in candidate.name for post_id in expected_ids):
                            self._verify_file_path(str(candidate))
                    recovered_count = len(self.verified_file_paths) - verified_before_recovery

            verified_keys = {normalized_local_path(x) for x in self.verified_file_paths}
            missing = []
            for raw in self.reported_file_paths:
                p = self._resolve_reported_path(raw)
                key = normalized_local_path(p) if p.exists() else ""
                if not key or key not in verified_keys:
                    # Existing pre-job files are skipped, not missing-save errors.
                    if key and key in self.preexisting_media and p.is_file() and p.stat().st_size > 0:
                        continue
                    missing.append(raw)
            # The recovered files correspond one-for-one with successful
            # after-events.  Their original text may be irreversibly mojibaked,
            # so clear that many unmatched notification strings by count.
            if recovered_count and missing:
                missing = missing[recovered_count:]
            self.missing_reported_paths = list(dict.fromkeys(missing))
        except Exception as exc:
            self.verification_error = str(exc)
            self.output_lines.append(f"[SAVE CHECK ERROR] {exc}")

    def _consume_output_line(self, line: str):
        line = line.rstrip("\r")
        if line.startswith(("SMC_META\t", "SMC_LIKE_SEEN\t", "SMC_FILE\t")):
            self.machine_lines.append(line)
            if line.startswith("SMC_FILE\t"):
                path = parse_smc_file_line(line)
                if path:
                    self.reported_file_paths.append(path)
                    self.status.setText(f"保存通知を確認中 / 通知 {len(self.reported_file_paths):,}件")
                    self._retry_verify_reported(path)
            elif line.startswith("SMC_LIKE_SEEN\t"):
                self.seen_media_count += 1
                self.status.setText(f"Likes確認中 / {self.seen_media_count:,}メディア")
                if self.stop_on_post_ids and not self.anchor_hit:
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[1] in self.stop_on_post_ids:
                        self.anchor_hit = parts[1]
                        QTimer.singleShot(0, self._request_anchor_stop)
            elif line.startswith("SMC_META\t"):
                self.meta_media_count += 1
                if not self.files_saved_count:
                    self.status.setText(f"メディア確認中 / {self.meta_media_count:,}件")
        else:
            self.output_lines.append(line)
            if len(self.output_lines) > 1000:
                self.output_lines = self.output_lines[-1000:]
            if line.strip() and not self._set_rate_limit(line):
                self.status.setText(line.strip()[-120:])

    def _read(self):
        data = self._stdout_decoder.decode(bytes(self.process.readAllStandardOutput()))
        self._stdout_buffer += data
        parts = self._stdout_buffer.split("\n")
        self._stdout_buffer = parts.pop() if parts else ""
        for line in parts:
            self._consume_output_line(line)

    def _finished(self, code, _status):
        if self.completed:
            return
        self._read()
        self._stdout_buffer += self._stdout_decoder.decode(b"", final=True)
        # Flush a final line that did not end with a newline.
        if self._stdout_buffer:
            self._consume_output_line(self._stdout_buffer)
            self._stdout_buffer = ""
        self.progress.hide()
        self._rate_timer.stop()
        self._reconcile_actual_files()
        self.completed = True
        if self.cancelled:
            code = -3
            self.status.setText(f"停止 / 保存 {self.files_saved_count:,}件・次回再確認")
        elif self.verification_error or self.missing_reported_paths:
            self.status.setText(f"保存確認エラー / 保存 {self.files_saved_count:,}件・次回再確認")
        elif self.anchor_hit:
            self.status.setText(f"アンカー到達 / 差分確認完了 ({self.anchor_hit})")
        elif code == 0 and self.files_saved_count:
            self.status.setText(f"完了 / 実在確認 {self.files_saved_count:,}件")
        elif code == 0 and self.reported_file_paths and not self.files_saved_count:
            self.status.setText("完了 / 今回の保存 0件（既存ファイル）")
        elif code == 0 and self.seen_media_count:
            self.status.setText(f"完了 / 確認 {self.seen_media_count:,}メディア")
        else:
            self.status.setText("完了 / 今回の保存 0件" if code == 0 else f"エラー (code {code})")
        self.finished.emit(self, code)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  {APP_VERSION}")
        self.resize(1440, 900)
        self._closing = False
        test_data_dir = os.environ.get("SMC_TEST_DATA_DIR", "").strip()
        self.settings = (QSettings(str(Path(test_data_dir) / "settings.ini"), QSettings.IniFormat)
                         if test_data_dir else QSettings("MasterTools", APP_NAME))
        self.data_dir = Path(test_data_dir) if test_data_dir else Path(self.settings.value("data_dir", str(Path.home() / "SNSMediaCollector")))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounts_file = self.data_dir / "accounts.json"
        self.accounts: list[AccountProfile] = self.load_accounts()
        self.target_store = TargetStore(self.data_dir / "targets.json")
        self.catalog = Catalog(self.data_dir / "catalog.sqlite3")
        self.jobs: list[DownloadJob] = []
        self.active_job: Optional[DownloadJob] = None
        self.last_saved_path: Optional[Path] = None

        self.build_ui()
        self.refresh_accounts()
        self.refresh_engine_status()
        self.refresh_recent_downloads()
        self.restore_session()
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.setInterval(300)
        self._session_timer.timeout.connect(self.save_session)
        for widget in (self.url_edit, self.dest_edit, self.date_after_edit):
            widget.textChanged.connect(lambda *_: self._session_timer.start())
        for widget in (self.account_combo, self.platform_combo):
            widget.currentIndexChanged.connect(lambda *_: self._session_timer.start())
        for group in (self.target_group, self.range_group):
            group.buttonClicked.connect(lambda *_: self._session_timer.start())
        for widget in self.session_checkboxes().values():
            widget.toggled.connect(lambda *_: self._session_timer.start())

    def session_checkboxes(self):
        return {name: getattr(self, name) for name in (
            "chk_direct_folder", "chk_remember_dest", "chk_archive", "chk_internal_meta",
            "chk_hitomi_name", "chk_img", "chk_video", "chk_gif")}

    def save_session(self):
        idx = self.account_combo.currentData()
        account_id = self.accounts[idx].profile_id if isinstance(idx, int) and 0 <= idx < len(self.accounts) else ""
        checked_range = self.range_group.checkedButton()
        state = dict(account_id=account_id, platform=self.platform_combo.currentData(),
                     target=self.url_edit.text(), target_type=self.selected_target(),
                     destination=self.dest_edit.text(), range_mode=checked_range.property("key"),
                     date_after=self.date_after_edit.text(),
                     options={name: widget.isChecked() for name, widget in self.session_checkboxes().items()})
        self.settings.setValue("last_session", json.dumps(state, ensure_ascii=False))
        self.settings.sync()

    def restore_session(self):
        try:
            state = json.loads(str(self.settings.value("last_session", "{}")))
            if not isinstance(state, dict) or not state:
                return
            idx = next((i for i, a in enumerate(self.accounts) if a.profile_id == state.get("account_id")), -1)
            self.account_combo.setCurrentIndex(self.account_combo.findData(idx))
            platform = self.platform_combo.findData(state.get("platform", "x"))
            if platform >= 0:
                self.platform_combo.setCurrentIndex(platform)
            self.url_edit.setText(str(state.get("target", "")))
            for button in self.target_buttons:
                if button.property("key") == state.get("target_type"):
                    button.setChecked(True)
            self.range_buttons.get(state.get("range_mode"), self.range_buttons["incremental"]).setChecked(True)
            self.date_after_edit.setText(str(state.get("date_after", "")))
            self.sync_target_ui()
            self.dest_edit.setText(str(state.get("destination") or self.dest_edit.text()))
            for name, widget in self.session_checkboxes().items():
                value = state.get("options", {}).get(name)
                if isinstance(value, bool):
                    widget.setChecked(value)
        except (ValueError, TypeError, AttributeError) as exc:
            self.log.append(f"[SESSION] 前回の画面設定を復元できませんでした: {exc}")

    def build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        layout = QVBoxLayout(root); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)

        header = QFrame(); header.setFixedHeight(66)
        hl = QHBoxLayout(header); hl.setContentsMargins(18, 8, 18, 8)
        titles = QVBoxLayout(); t = QLabel(APP_NAME); t.setObjectName("title")
        sub = QLabel("X / Twitter & pixiv メディア収集クライアント — test build"); sub.setObjectName("muted")
        titles.addWidget(t); titles.addWidget(sub); hl.addLayout(titles); hl.addStretch()
        self.engine_badge = QLabel("gallery-dl: 確認中…"); self.engine_badge.setObjectName("muted")
        hl.addWidget(self.engine_badge)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.build_sidebar())
        splitter.addWidget(self.build_center())
        splitter.addWidget(self.build_queue())
        splitter.setSizes([260, 790, 390])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        footer = QFrame(); footer.setFixedHeight(36)
        fl = QHBoxLayout(footer); fl.setContentsMargins(16, 4, 16, 4)
        self.footer_status = QLabel("準備完了"); self.footer_status.setObjectName("muted")
        fl.addWidget(self.footer_status); fl.addStretch()
        self.data_label = QLabel(str(self.data_dir)); self.data_label.setObjectName("muted")
        fl.addWidget(self.data_label)
        layout.addWidget(footer)

    def build_sidebar(self):
        w = QFrame(); w.setObjectName("sidebar"); w.setMinimumWidth(235)
        l = QVBoxLayout(w); l.setContentsMargins(12, 12, 12, 12)
        top = QHBoxLayout(); sec = QLabel("アカウント"); sec.setObjectName("section")
        add = QPushButton("＋ 追加"); add.clicked.connect(self.add_account)
        top.addWidget(sec); top.addStretch(); top.addWidget(add); l.addLayout(top)
        self.account_list = QListWidget(); self.account_list.currentRowChanged.connect(self.account_selected)
        l.addWidget(self.account_list, 1)
        edit = QPushButton("選択アカウントを編集"); edit.clicked.connect(self.edit_account)
        delete = QPushButton("選択アカウントを削除"); delete.clicked.connect(self.delete_account)
        l.addWidget(edit); l.addWidget(delete)
        l.addSpacing(10)
        oauth = QPushButton("pixiv OAuth を開始"); oauth.clicked.connect(self.start_pixiv_oauth)
        l.addWidget(oauth)
        open_data = QPushButton("データフォルダを開く"); open_data.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir))))
        l.addWidget(open_data)
        return w

    def build_center(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget(); l = QVBoxLayout(content); l.setContentsMargins(14, 12, 14, 12); l.setSpacing(12)

        card = QFrame(); card.setObjectName("card"); cl = QVBoxLayout(card); cl.setContentsMargins(18, 18, 18, 18); cl.setSpacing(12)
        sec = QLabel("新しいダウンロード"); sec.setObjectName("section"); cl.addWidget(sec)

        platform_row = QHBoxLayout(); platform_row.addWidget(QLabel("取得元"))
        self.platform_combo = QComboBox(); self.platform_combo.addItem("X / Twitter", "x"); self.platform_combo.addItem("pixiv", "pixiv")
        self.platform_combo.currentIndexChanged.connect(self.platform_changed)
        platform_row.addWidget(self.platform_combo, 1); cl.addLayout(platform_row)

        account_row = QHBoxLayout(); account_row.addWidget(QLabel("アカウント"))
        self.account_combo = QComboBox(); self.account_combo.currentIndexChanged.connect(self.account_combo_changed)
        account_row.addWidget(self.account_combo, 1); cl.addLayout(account_row)

        cl.addWidget(QLabel("URL / ユーザー名 / ID"))
        self.url_edit = QLineEdit(); self.url_edit.setPlaceholderText("例: https://x.com/username / @username / pixiv user ID")
        self.url_edit.editingFinished.connect(self.sync_target_ui)
        cl.addWidget(self.url_edit)

        target_row = QHBoxLayout(); target_row.addWidget(QLabel("取得対象"))
        self.target_group = QButtonGroup(self)
        self.target_buttons: list[QRadioButton] = []
        for text, key in [("投稿", "posts"), ("メディア", "media"), ("いいね", "likes")]:
            b = QRadioButton(text); b.setProperty("key", key); self.target_group.addButton(b); self.target_buttons.append(b); target_row.addWidget(b)
        self.target_buttons[1].setChecked(True); target_row.addStretch(); cl.addLayout(target_row)
        self.target_group.buttonClicked.connect(lambda _b: self.sync_target_ui())

        range_row = QHBoxLayout(); range_row.addWidget(QLabel("取得範囲"))
        self.range_group = QButtonGroup(self)
        self.range_buttons = {}
        for text, key in [("前回 / 引継ぎ基準以降", "incremental"), ("すべて", "all"), ("日付以降", "date")]:
            b = QRadioButton(text); b.setProperty("key", key); self.range_group.addButton(b); self.range_buttons[key] = b; range_row.addWidget(b)
            if key == "incremental": b.setChecked(True)
        self.date_after_edit = QLineEdit(); self.date_after_edit.setPlaceholderText("例: 2026-08-01")
        self.date_after_edit.setMaximumWidth(170); range_row.addWidget(self.date_after_edit); range_row.addStretch(); cl.addLayout(range_row)
        range_note = QLabel(
            "Hitomi引継ぎ済みなら、最新の既存Tweetを初回基準にして10分前から再確認します。"
            "archiveで既存分を飛ばすので、基本は続きだけ取得します。"
        )
        range_note.setObjectName("muted"); range_note.setWordWrap(True); cl.addWidget(range_note)
        self.range_status = QLabel("取得基準を確認中…")
        self.range_status.setObjectName("muted"); self.range_status.setWordWrap(True); cl.addWidget(self.range_status)
        self.range_group.buttonClicked.connect(lambda _b: self.sync_target_ui())
        self.date_after_edit.textChanged.connect(lambda _t: self.sync_target_ui())

        likes_row = QHBoxLayout()
        self.likes_baseline_btn = QPushButton("Likesアンカーを作成（DLなし）")
        self.likes_baseline_btn.clicked.connect(self.start_likes_baseline)
        likes_row.addWidget(self.likes_baseline_btn)
        likes_row.addWidget(QLabel("アンカー"))
        self.likes_anchor_spin = QSpinBox(); self.likes_anchor_spin.setRange(10, 200); self.likes_anchor_spin.setValue(50)
        self.likes_anchor_spin.setSuffix(" 投稿")
        likes_row.addWidget(self.likes_anchor_spin)
        likes_row.addWidget(QLabel("探索上限"))
        self.likes_probe_spin = QSpinBox(); self.likes_probe_spin.setRange(50, 500); self.likes_probe_spin.setValue(300)
        self.likes_probe_spin.setSuffix(" メディア")
        likes_row.addWidget(self.likes_probe_spin)
        likes_row.addStretch(); cl.addLayout(likes_row)
        self.likes_note = QLabel(
            "Likesは先頭だけを軽く確認し、保存済みアンカーに到達したら終了します。アンカーより手前の新規投稿だけをDLします。"
            "アンカーが探索上限内で見つからない場合は、安全のため何もDLしません。"
        )
        self.likes_note.setObjectName("muted"); self.likes_note.setWordWrap(True); cl.addWidget(self.likes_note)

        media_row = QHBoxLayout(); media_row.addWidget(QLabel("メディア"))
        self.chk_img = QCheckBox("画像"); self.chk_img.setChecked(True)
        self.chk_video = QCheckBox("動画"); self.chk_video.setChecked(True)
        self.chk_gif = QCheckBox("GIF"); self.chk_gif.setChecked(True)
        media_row.addWidget(self.chk_img); media_row.addWidget(self.chk_video); media_row.addWidget(self.chk_gif); media_row.addStretch(); cl.addLayout(media_row)

        option_row = QHBoxLayout()
        self.chk_archive = QCheckBox("重複をスキップ（archive）"); self.chk_archive.setChecked(True)
        self.chk_internal_meta = QCheckBox("投稿者IDなどを内部DBに記録（JSONは作りません）"); self.chk_internal_meta.setChecked(True)
        option_row.addWidget(self.chk_archive); option_row.addWidget(self.chk_internal_meta); option_row.addStretch(); cl.addLayout(option_row)

        compat_row = QHBoxLayout()
        self.chk_hitomi_name = QCheckBox("Hitomi互換ファイル名  [YY-MM-DD] TweetID_p0"); self.chk_hitomi_name.setChecked(True)
        compat_row.addWidget(self.chk_hitomi_name); compat_row.addStretch(); cl.addLayout(compat_row)

        dest_row = QHBoxLayout(); dest_row.addWidget(QLabel("保存先"))
        self.dest_edit = QLineEdit(str(self.data_dir / "Library")); dest_row.addWidget(self.dest_edit, 1)
        browse = QPushButton("変更"); browse.clicked.connect(self.pick_destination); dest_row.addWidget(browse); cl.addLayout(dest_row)

        dest_opt = QHBoxLayout()
        self.chk_remember_dest = QCheckBox("この取得対象専用の保存先として記憶"); self.chk_remember_dest.setChecked(True)
        self.chk_direct_folder = QCheckBox("選択フォルダ直下に保存（Hitomi引継ぎ向け）"); self.chk_direct_folder.setChecked(True)
        dest_opt.addWidget(self.chk_remember_dest); dest_opt.addWidget(self.chk_direct_folder); dest_opt.addStretch(); cl.addLayout(dest_opt)

        target_row2 = QHBoxLayout()
        self.target_status = QLabel("この取得対象の保存先はまだ未登録です。"); self.target_status.setObjectName("muted"); self.target_status.setWordWrap(True)
        target_row2.addWidget(self.target_status, 1)
        import_btn = QPushButton("既存Hitomiフォルダを引継ぎ / archive登録")
        import_btn.clicked.connect(self.index_existing_hitomi_folder); target_row2.addWidget(import_btn); cl.addLayout(target_row2)

        buttons = QHBoxLayout(); self.preview_btn = QPushButton("コマンド確認"); self.preview_btn.clicked.connect(self.preview_command)
        self.start_btn = QPushButton("⬇ ダウンロード開始"); self.start_btn.setObjectName("primary"); self.start_btn.clicked.connect(self.enqueue_download)
        buttons.addStretch(); buttons.addWidget(self.preview_btn); buttons.addWidget(self.start_btn); cl.addLayout(buttons)
        l.addWidget(card)

        recent_card = QFrame(); recent_card.setObjectName("card")
        rl = QVBoxLayout(recent_card); rl.setContentsMargins(16, 16, 16, 16); rl.setSpacing(10)
        rhead = QHBoxLayout(); rsec = QLabel("最近のダウンロード"); rsec.setObjectName("section")
        rhead.addWidget(rsec)
        self.recent_count_label = QLabel("0件"); self.recent_count_label.setObjectName("muted")
        rhead.addWidget(self.recent_count_label); rhead.addStretch()
        self.reveal_last_btn = QPushButton("最後の保存ファイルを表示")
        self.reveal_last_btn.setEnabled(False)
        self.reveal_last_btn.clicked.connect(self.reveal_last_saved_file)
        open_recent = QPushButton("保存先を開く")
        open_recent.clicked.connect(self.open_current_destination)
        seed_recent = QPushButton("保存先から表示")
        seed_recent.clicked.connect(self.seed_recent_from_current_destination)
        refresh_recent = QPushButton("更新")
        refresh_recent.clicked.connect(self.refresh_recent_downloads)
        rhead.addWidget(self.reveal_last_btn); rhead.addWidget(open_recent); rhead.addWidget(seed_recent); rhead.addWidget(refresh_recent); rl.addLayout(rhead)
        self.last_saved_label = QLabel("今回の実保存: まだありません")
        self.last_saved_label.setObjectName("muted"); self.last_saved_label.setWordWrap(True); rl.addWidget(self.last_saved_label)
        hint = QLabel("Hitomi Downloaderのように、取得できたファイルをここへ即時表示します。ダブルクリックで開く / 右クリックでフォルダ・元投稿。")
        hint.setObjectName("muted"); hint.setWordWrap(True); rl.addWidget(hint)
        self.recent_grid = QGridLayout(); self.recent_grid.setSpacing(10); self.recent_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        recent_holder = QWidget(); recent_holder.setLayout(self.recent_grid)
        recent_scroll = QScrollArea(); recent_scroll.setWidgetResizable(True); recent_scroll.setFrameShape(QFrame.NoFrame)
        recent_scroll.setMinimumHeight(390); recent_scroll.setWidget(recent_holder)
        rl.addWidget(recent_scroll)
        l.addWidget(recent_card)

        log_card = QFrame(); log_card.setObjectName("card"); ll = QVBoxLayout(log_card); ll.setContentsMargins(16, 16, 16, 16)
        row = QHBoxLayout(); sec = QLabel("ログ / テスト情報"); sec.setObjectName("section"); row.addWidget(sec); row.addStretch()
        clear = QPushButton("クリア"); clear.clicked.connect(lambda: self.log.clear()); row.addWidget(clear); ll.addLayout(row)
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMinimumHeight(280); ll.addWidget(self.log)
        l.addWidget(log_card)
        l.addStretch()
        scroll.setWidget(content)
        return scroll

    def build_queue(self):
        w = QFrame(); w.setObjectName("sidebar"); w.setMinimumWidth(340)
        l = QVBoxLayout(w); l.setContentsMargins(12, 12, 12, 12)
        row = QHBoxLayout(); sec = QLabel("ダウンロードキュー"); sec.setObjectName("section"); row.addWidget(sec); row.addStretch()
        stop = QPushButton("停止"); stop.setObjectName("danger"); stop.clicked.connect(self.stop_active); row.addWidget(stop); l.addLayout(row)
        self.queue_layout = QVBoxLayout(); self.queue_layout.setAlignment(Qt.AlignTop)
        holder = QWidget(); holder.setLayout(self.queue_layout)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); scroll.setWidget(holder)
        l.addWidget(scroll, 1)
        return w

    def reveal_last_saved_file(self):
        if self.last_saved_path and self.last_saved_path.exists():
            reveal_path_in_file_manager(self.last_saved_path)
        else:
            QMessageBox.information(self, "実保存", "この起動中に実在確認できた新規ファイルはまだありません。")

    def open_current_destination(self):
        path = Path(self.dest_edit.text().strip()).expanduser()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, "保存先", "保存先フォルダはまだ存在しません。")

    def seed_recent_from_current_destination(self):
        root = Path(self.dest_edit.text().strip()).expanduser()
        if not root.exists():
            QMessageBox.information(self, "最近のダウンロード", "保存先フォルダが見つかりません。")
            return
        try:
            # Hitomi移行では直下保存が基本。直下だけなら巨大フォルダでも軽く、
            # サブフォルダ構成を選んだ場合だけ再帰検索します。
            iterator = root.iterdir() if self.chk_direct_folder.isChecked() else root.rglob("*")
            media = []
            allowed = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif", ".mp4", ".webm", ".m4v", ".mov", ".mkv"}
            for path in iterator:
                try:
                    if path.is_file() and path.suffix.lower() in allowed:
                        media.append((path.stat().st_mtime_ns, path))
                except OSError:
                    continue
            media.sort(key=lambda x: x[0], reverse=True)
            try:
                _url, target_key, target_name = self.current_target()
            except Exception:
                target_key, target_name = "", ""
            login_name, _mode, _value = self.current_auth()
            platform = str(self.platform_combo.currentData() or "")
            dialog = QDialog(self)
            dialog.setWindowTitle("保存先の画像（ダウンロード履歴とは別表示）")
            dialog.resize(760, 600)
            layout = QVBoxLayout(dialog)
            caption = QLabel(f"{root}\n更新日時順・最新 {min(24, len(media))}件")
            caption.setWordWrap(True)
            layout.addWidget(caption)
            scroll = QScrollArea(dialog); scroll.setWidgetResizable(True)
            holder = QWidget(); grid = QGridLayout(holder)
            for i, (_mtime, path) in enumerate(media[:24]):
                info = dict(path=str(path), platform=platform, target_key=target_key,
                            login_profile=login_name, target_name=target_name)
                grid.addWidget(MediaThumbnail(info, holder), i // 4, i % 4)
            scroll.setWidget(holder); layout.addWidget(scroll)
            dialog.exec()
        except Exception as exc:
            QMessageBox.warning(self, "最近のダウンロード", str(exc))

    def clear_recent_grid(self):
        if not hasattr(self, "recent_grid"):
            return
        while self.recent_grid.count():
            item = self.recent_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def refresh_recent_downloads(self):
        if not hasattr(self, "recent_grid"):
            return
        self.clear_recent_grid()
        try:
            rows = [r for r in self.catalog.recent_files(24) if Path(str(r.get("path") or "")).exists()]
        except Exception as exc:
            if hasattr(self, "log"):
                self.log.append(f"[RECENT ERROR] {exc}")
            rows = []
        for i, info in enumerate(rows):
            card = MediaThumbnail(info, self)
            self.recent_grid.addWidget(card, i // 4, i % 4)
        total = 0
        try:
            total = self.catalog.recent_file_count()
        except Exception:
            total = len(rows)
        self.recent_count_label.setText(f"表示 {len(rows)}件 / 履歴 {total:,}件")

    def job_file_saved(self, job: DownloadJob, path_text: str):
        ctx = getattr(job, "smc_context", {})
        path = Path(path_text)
        try:
            if not path.exists():
                self.log.append(f"[SAVE VERIFY ERROR] 通知されたパスが存在しません: {path}")
                return
            size = int(path.stat().st_size)
            self.catalog.record_recent_file(
                path,
                platform=str(ctx.get("platform") or ""),
                target_key=str(ctx.get("target_key") or ""),
                login_profile=str(ctx.get("login_name") or ""),
                target_name=str(ctx.get("target_name") or ""),
            )
            self.last_saved_path = path
            if hasattr(self, "reveal_last_btn"):
                self.reveal_last_btn.setEnabled(True)
            if hasattr(self, "last_saved_label"):
                self.last_saved_label.setText(f"今回の実保存: {path}")
                self.last_saved_label.setToolTip(str(path))
            self.footer_status.setText(f"実保存 {job.files_saved_count:,}件: {path}")
            self.log.append(f"[SAVED OK] {path} / {size:,} bytes")
            self.refresh_recent_downloads()
        except Exception as exc:
            self.log.append(f"[RECENT ERROR] {path}: {exc}")

    def connect_job(self, job: DownloadJob):
        job.finished.connect(self.job_finished)
        job.file_saved.connect(self.job_file_saved)
        return job

    def load_accounts(self) -> list[AccountProfile]:
        try:
            data = json.loads(self.accounts_file.read_text(encoding="utf-8"))
            profiles = [AccountProfile(**x) for x in data]
            if any(not x.get("profile_id") for x in data):
                try:
                    atomic_write_json(self.accounts_file, [asdict(a) for a in profiles])
                except OSError:
                    # A read-only file must not hide otherwise usable saved accounts.
                    pass
            return profiles
        except Exception:
            return []

    def save_accounts(self):
        atomic_write_json(self.accounts_file, [asdict(a) for a in self.accounts])

    def refresh_accounts(self, selected_id=None):
        if selected_id is None:
            selected_id = self.account_combo.currentData(Qt.UserRole + 1)
        first_load = self.account_combo.count() == 0
        self.account_list.blockSignals(True); self.account_combo.blockSignals(True)
        try:
            self.account_list.clear(); self.account_combo.clear()
            for i, a in enumerate(self.accounts):
                prefix = "𝕏" if a.platform == "x" else "P"
                self.account_list.addItem(f"{prefix}  {a.name}")
                self.account_combo.addItem(f"{prefix}  {a.name}", i)
                self.account_combo.setItemData(i, a.profile_id, Qt.UserRole + 1)
            self.account_combo.addItem("認証なし", -1)
            self.account_combo.setItemData(len(self.accounts), "", Qt.UserRole + 1)
            selected = next((i for i, a in enumerate(self.accounts) if a.profile_id == selected_id), -1)
            if selected < 0 and first_load and self.accounts:
                selected = 0
            self.account_combo.setCurrentIndex(self.account_combo.findData(selected))
            self.account_list.setCurrentRow(selected)
        finally:
            self.account_list.blockSignals(False); self.account_combo.blockSignals(False)
        self.account_combo_changed()

    def add_account(self):
        d = AccountDialog(self)
        if d.exec():
            self.accounts.append(d.result_profile()); self.save_accounts(); self.refresh_accounts(self.accounts[-1].profile_id)

    def edit_account(self):
        idx = self.account_list.currentRow()
        if idx < 0 or idx >= len(self.accounts): return
        d = AccountDialog(self, self.accounts[idx])
        if d.exec():
            self.accounts[idx] = d.result_profile(); self.save_accounts(); self.refresh_accounts(self.accounts[idx].profile_id)

    def delete_account(self):
        idx = self.account_list.currentRow()
        if idx < 0 or idx >= len(self.accounts): return
        if QMessageBox.question(self, "削除", f"「{self.accounts[idx].name}」を削除しますか？") == QMessageBox.Yes:
            self.accounts.pop(idx); self.save_accounts(); self.refresh_accounts()

    def account_selected(self, idx):
        if 0 <= idx < len(self.accounts):
            a = self.accounts[idx]
            pidx = self.platform_combo.findData(a.platform)
            if pidx >= 0: self.platform_combo.setCurrentIndex(pidx)
            cidx = self.account_combo.findData(idx)
            if cidx >= 0: self.account_combo.setCurrentIndex(cidx)

    def account_combo_changed(self):
        idx = self.account_combo.currentData()
        self.account_list.blockSignals(True)
        self.account_list.setCurrentRow(idx if isinstance(idx, int) else -1)
        self.account_list.blockSignals(False)
        if isinstance(idx, int) and 0 <= idx < len(self.accounts):
            a = self.accounts[idx]
            pidx = self.platform_combo.findData(a.platform)
            if pidx >= 0 and pidx != self.platform_combo.currentIndex():
                self.platform_combo.setCurrentIndex(pidx)

    def platform_changed(self):
        p = self.platform_combo.currentData()
        labels = [("投稿", "posts"), ("メディア", "media"), ("いいね", "likes")] if p == "x" else [("作品", "posts"), ("イラスト", "media"), ("ブックマーク", "likes")]
        for b, (text, key) in zip(self.target_buttons, labels):
            b.setText(text); b.setProperty("key", key)
        self.url_edit.setPlaceholderText("例: https://x.com/username / @username" if p == "x" else "例: https://www.pixiv.net/users/123456 / 123456")
        if hasattr(self, "chk_hitomi_name"):
            self.chk_hitomi_name.setEnabled(p == "x")
        self.sync_target_ui()

    def pick_destination(self):
        d = QFileDialog.getExistingDirectory(self, "保存先を選択", self.dest_edit.text())
        if d: self.dest_edit.setText(d)

    def index_existing_hitomi_folder(self):
        try:
            _url, key, name = self.current_target()
        except Exception as exc:
            QMessageBox.warning(self, "取得対象が必要です", str(exc)); return
        folder = QFileDialog.getExistingDirectory(self, "既存Hitomiフォルダを選択", self.dest_edit.text())
        if not folder:
            return
        self.dest_edit.setText(folder)
        platform = self.platform_combo.currentData()
        profile = self.target_store.ensure(key, platform, name)
        profile.destination = folder; profile.direct_folder = True
        self.chk_direct_folder.setChecked(True); self.chk_remember_dest.setChecked(True)
        try:
            count = self.catalog.index_existing_folder(Path(folder), hash_files=False)
            profile.indexed_files = count; profile.indexed_at = iso_utc(utc_now())
            detail = ""
            if platform == "x":
                idx = self.account_combo.currentData()
                account_name = self.accounts[idx].name if isinstance(idx, int) and 0 <= idx < len(self.accounts) else "認証なし"
                archive_scope = f"{key}:{self.selected_target()}"
                apath = archive_path_for(
                    self.data_dir / "archives", platform="x", account_name=account_name, archive_scope=archive_scope
                )
                imported = import_hitomi_x_archive(Path(folder), apath)
                profile.hitomi_matched_files = int(imported.get("matched", 0))
                profile.imported_archive_entries = int(imported.get("archive_total", 0))
                tweet_time = imported.get("latest_tweet_time", "")
                if tweet_time:
                    profile.hitomi_baseline_at = tweet_time
                    profile.hitomi_latest_tweet_id = str(imported.get("latest_tweet_id", ""))
                    profile.hitomi_source_folder = folder
                    # The import itself is not a successful network scan.  Keep the
                    # real scan fields empty so the UI can distinguish the first
                    # SMC run from later incremental runs.
                    if not profile.last_success_at:
                        profile.last_scan_started_at = ""
                detail = (
                    f"\nHitomi形式に一致: {imported.get('matched', 0):,} 件"
                    f"\n形式不一致: {imported.get('unmatched', 0):,} 件"
                    f"\narchiveへ新規登録: {imported.get('archive_added', 0):,} 件"
                    f" / 合計 {imported.get('archive_total', 0):,} 件"
                )
                if imported.get("latest_tweet_id"):
                    detail += (
                        f"\n最新Tweet ID {imported['latest_tweet_id']} を初回取得基準に設定しました。"
                        "\n次回はこの時刻の10分前から再確認し、archiveで既存分をスキップします。"
                    )
                    if hasattr(self, "range_buttons"):
                        self.range_buttons["incremental"].setChecked(True)
                self.log.append(
                    f"[HITOMI IMPORT] {name}: matched={imported.get('matched', 0)} "
                    f"unmatched={imported.get('unmatched', 0)} archive_added={imported.get('archive_added', 0)} "
                    f"archive={apath}"
                )
            else:
                detail = "\npixivは既存ファイル索引のみ行いました。X用archive変換は行いません。"
            self.target_store.save(); self.sync_target_ui()
            # Show existing Hitomi files immediately, so the gallery area is useful
            # before the first new network download.
            self.seed_recent_from_current_destination()
            QMessageBox.information(
                self, "引継ぎ完了",
                f"既存フォルダをこの取得対象に紐付けました。\nメディアファイル {count:,} 件を索引しました。{detail}"
                "\n\n既存ファイルは削除・移動しません。画像フォルダにJSONも作りません。"
            )
        except Exception as exc:
            QMessageBox.warning(self, "引継ぎエラー", str(exc))

    def selected_target(self) -> str:
        b = self.target_group.checkedButton()
        return str(b.property("key")) if b else "media"

    def normalize_url(self, platform: str, raw: str, target: str) -> str:
        return normalize_target(platform, raw, target)[0]

    def current_target(self) -> tuple[str, str, str]:
        platform = self.platform_combo.currentData()
        return normalize_target(platform, self.url_edit.text(), self.selected_target())

    def current_target_profile(self, create: bool = True) -> Optional[TargetProfile]:
        try:
            _url, key, name = self.current_target()
        except Exception:
            return None
        if create:
            return self.target_store.ensure(key, self.platform_combo.currentData(), name)
        return self.target_store.get(key)

    def sync_target_ui(self):
        profile = self.current_target_profile(create=False)
        is_x_likes = self.platform_combo.currentData() == "x" and self.selected_target() == "likes"
        if hasattr(self, "likes_baseline_btn"):
            self.likes_baseline_btn.setEnabled(is_x_likes and not self.has_pending_likes_job())
            self.likes_anchor_spin.setEnabled(is_x_likes)
            self.likes_probe_spin.setEnabled(is_x_likes)
            self.likes_note.setEnabled(is_x_likes)
            self.range_buttons["incremental"].setText("新しいいいねだけ" if is_x_likes else "前回 / 引継ぎ基準以降")
        if profile and profile.destination:
            self.dest_edit.setText(profile.destination)
            self.chk_direct_folder.setChecked(profile.direct_folder)
            self.target_status.setText(self.target_status_text(profile))
        else:
            self.dest_edit.setText(str(self.data_dir / "Library"))
            self.chk_direct_folder.setChecked(True)
            self.target_status.setText("この取得対象の保存先はまだ未登録です。")
        if hasattr(self, "range_status"):
            self.range_status.setText(self.range_status_text(profile))

    def display_local_time(self, value: str) -> str:
        dt = parse_iso(value)
        if not dt:
            return value
        try:
            return dt.astimezone().strftime("%Y/%m/%d %H:%M:%S")
        except Exception:
            return value

    def target_status_text(self, profile: TargetProfile) -> str:
        parts = []
        if profile.author_name or profile.author_id:
            author = ("@" + profile.author_name) if profile.author_name else profile.target_name
            if profile.author_id:
                author += f" / ID {profile.author_id}"
            parts.append(author)
        if profile.last_success_at:
            parts.append(f"最終成功: {self.display_local_time(profile.last_success_at)}")
        if profile.hitomi_baseline_at and not profile.last_success_at:
            text = f"Hitomi基準: {self.display_local_time(profile.hitomi_baseline_at)}"
            if profile.hitomi_latest_tweet_id:
                text += f" / Tweet {profile.hitomi_latest_tweet_id}"
            parts.append(text)
        if profile.indexed_files:
            parts.append(f"既存フォルダ: {profile.indexed_files:,}件を索引済み")
        if profile.imported_archive_entries:
            parts.append(f"archive: {profile.imported_archive_entries:,}件")
        if profile.likes_anchor_at:
            parts.append(
                f"Likesアンカー: {profile.likes_anchor_posts:,}投稿 / 確認済み {profile.likes_seen_posts:,}投稿"
            )
        return "  |  ".join(parts) if parts else "登録済み（まだ取得履歴なし）"

    def range_status_text(self, profile: Optional[TargetProfile]) -> str:
        button = self.range_group.checkedButton() if hasattr(self, "range_group") else None
        mode = str(button.property("key")) if button else "incremental"
        if mode == "all":
            return "✓ 『すべて』を明示選択中です。既存archiveにあるメディアはスキップしつつ、過去まで走査します。"
        if mode == "date":
            value = self.date_after_edit.text().strip() if hasattr(self, "date_after_edit") else ""
            return f"✓ 指定日以降を取得: {value}" if value else "⚠ 『日付以降』の日付を入力してください。"

        if self.platform_combo.currentData() == "x" and self.selected_target() == "likes":
            if not profile or not profile.likes_anchor_at:
                return "⚠ Likesアンカーが未作成です。『Likesアンカーを作成（DLなし）』を先に実行してください。"
            return (
                f"✓ Likesアンカー: {self.display_local_time(profile.likes_anchor_at)} / "
                f"認証 {profile.likes_anchor_login or '未記録'} / "
                f"{profile.likes_anchor_posts:,}投稿。差分取得はアンカー到達で即終了します。"
            )

        if self.platform_combo.currentData() != "x":
            if profile and (profile.last_scan_started_at or profile.last_success_at):
                dt, _source = incremental_baseline(profile)
                return f"差分基準: {dt.astimezone().strftime('%Y/%m/%d %H:%M:%S') if dt else '未設定'}"
            return "pixiv初回の差分基準は未設定のため、現状は全件対象になります。"

        dt, source = incremental_baseline(profile)
        if not dt:
            return "⚠ Xの初回基準は未設定です。Hitomiフォルダを引き継ぐか、『すべて』『日付以降』を明示してください。"
        local = dt.astimezone().strftime("%Y/%m/%d %H:%M:%S")
        if source == "hitomi":
            tid = f" / Tweet {profile.hitomi_latest_tweet_id}" if profile and profile.hitomi_latest_tweet_id else ""
            return f"✓ 初回基準: Hitomi既存データ {local}{tid}。実取得は安全のため10分前から再確認します。"
        return f"✓ 次回差分基準: 前回取得 {local}。実取得は安全のため10分前から再確認します。"

    def has_pending_likes_job(self, target_key: str = "") -> bool:
        for job in self.jobs:
            ctx = getattr(job, "smc_context", {})
            if ctx.get("job_kind") not in {"likes_anchor", "likes_probe", "likes_download"}:
                continue
            if target_key and ctx.get("target_key") != target_key:
                continue
            if not job.completed:
                return True
        return False

    def current_auth(self) -> tuple[str, str, str]:
        idx = self.account_combo.currentData()
        if isinstance(idx, int) and 0 <= idx < len(self.accounts):
            a = self.accounts[idx]
            return a.name, a.auth_mode, a.auth_value
        return "認証なし", "none", ""

    def selected_extensions(self) -> list[str]:
        extensions: list[str] = []
        if self.chk_img.isChecked():
            extensions += ["jpg", "jpeg", "png", "webp", "avif", "bmp"]
        if self.chk_video.isChecked():
            extensions += ["mp4", "webm", "m4v", "mov"]
        if self.chk_gif.isChecked():
            extensions += ["gif"]
        return extensions

    def is_x_likes_incremental(self) -> bool:
        if self.platform_combo.currentData() != "x" or self.selected_target() != "likes":
            return False
        button = self.range_group.checkedButton()
        return bool(button and str(button.property("key")) == "incremental")

    def build_likes_probe(self) -> tuple[list[str], str, dict]:
        url, target_key, target_name = self.current_target()
        account_name, auth_mode, auth_value = self.current_auth()
        profile = self.target_store.ensure(target_key, "x", target_name)
        if not profile.likes_anchor_at:
            raise ValueError("Likesアンカーがありません。先に『Likesアンカーを作成（DLなし）』を実行してください。")
        if profile.likes_anchor_login and profile.likes_anchor_login != account_name:
            raise ValueError(
                f"このLikesアンカーは認証プロファイル『{profile.likes_anchor_login}』で作成されています。"
                f"現在は『{account_name}』です。同じ認証プロファイルを選ぶか、アンカーを作り直してください。"
            )
        anchor_ids = self.catalog.likes_anchor_ids(target_key=target_key, login_profile=account_name)
        if not anchor_ids:
            raise ValueError("LikesアンカーDBが空です。『Likesアンカーを作成（DLなし）』をやり直してください。")
        cmd = build_x_likes_probe_command(
            self.engine_command(), url=url, auth_mode=auth_mode, auth_value=auth_value,
            max_media=self.likes_probe_spin.value(),
        )
        title = f"X / {account_name} / {target_name} / Likes差分確認（DLなし）"
        ctx = {
            "job_kind": "likes_probe",
            "platform": "x",
            "target_key": target_key,
            "target_name": target_name,
            "target_type": "likes",
            "login_name": account_name,
            "auth_mode": auth_mode,
            "auth_value": auth_value,
            "anchor_ids": anchor_ids,
            "anchor_posts": max(1, int(profile.likes_anchor_posts or self.likes_anchor_spin.value())),
            "archive_scope": f"{target_key}:likes",
            "destination": self.dest_edit.text().strip(),
            "extensions": self.selected_extensions(),
            "capture_internal_metadata": self.chk_internal_meta.isChecked(),
            "use_archive": self.chk_archive.isChecked(),
            "direct_folder": self.chk_direct_folder.isChecked(),
            "hitomi_compat": self.chk_hitomi_name.isChecked(),
            "started_at": iso_utc(utc_now()),
        }
        return cmd, title, ctx

    def enqueue_likes_probe(self):
        try:
            cmd, title, ctx = self.build_likes_probe()
        except Exception as exc:
            QMessageBox.warning(self, "Likes差分確認", str(exc))
            return
        if self.has_pending_likes_job(ctx.get("target_key", "")):
            QMessageBox.information(self, "Likes処理中", "この取得対象のLikes処理はすでにキューまたは実行中です。")
            return
        job = DownloadJob(title, cmd)
        job.stop_on_post_ids = set(ctx.get("anchor_ids", []))
        job.smc_context = ctx
        self.connect_job(job)
        self.jobs.append(job)
        self.queue_layout.addWidget(job)
        self.log.append(
            f"[LIKES PROBE] 最大{self.likes_probe_spin.value():,}メディアだけ確認し、"
            "アンカーに到達した時点で停止します。画像はまだDLしません。"
        )
        if self.active_job is None:
            self.start_next_job()

    def make_likes_download_job(self, probe_ctx: dict, new_records: list[LikeSeenRecord]) -> DownloadJob:
        urls = likes_post_urls(new_records)
        if not urls:
            raise ValueError("新規Likesの投稿URLを作成できませんでした。")
        destination = Path(str(probe_ctx.get("destination") or self.data_dir / "Library")).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        cmd = core_build_command(
            self.engine_command(),
            platform="x",
            url=urls[0],
            destination=destination,
            account_name=str(probe_ctx.get("login_name") or "認証なし"),
            auth_mode=str(probe_ctx.get("auth_mode") or "none"),
            auth_value=str(probe_ctx.get("auth_value") or ""),
            archive_scope=str(probe_ctx.get("archive_scope") or "likes"),
            extensions=list(probe_ctx.get("extensions") or []),
            capture_internal_metadata=bool(probe_ctx.get("capture_internal_metadata")),
            use_archive=bool(probe_ctx.get("use_archive", True)),
            archive_dir=self.data_dir / "archives",
            direct_folder=bool(probe_ctx.get("direct_folder", True)),
            range_mode="all",
            profile=self.target_store.get(str(probe_ctx.get("target_key") or "")),
            hitomi_compat_x=bool(probe_ctx.get("hitomi_compat", True)),
            target_type="likes",
        )
        cmd = append_urls(cmd, urls)
        title = (
            f"X / {probe_ctx.get('login_name', '認証なし')} / "
            f"{probe_ctx.get('target_name', '')} / Likes取得確認 {len(urls)}投稿"
        )
        job = DownloadJob(title, cmd)
        job.smc_context = dict(probe_ctx)
        job.smc_context.update({
            "job_kind": "likes_download",
            "destination": str(destination),
            "probe_new_records": list(probe_ctx.get("probe_all_records", new_records)),
            "started_at": iso_utc(utc_now()),
            "started_epoch": time.time(),
        })
        self.connect_job(job)
        return job

    def start_likes_baseline(self):
        if self.platform_combo.currentData() != "x" or self.selected_target() != "likes":
            QMessageBox.information(self, "Likesアンカー", "X / いいね を選択してから実行してください。")
            return
        if not self.engine_available():
            QMessageBox.warning(self, "gallery-dl がありません", "setup_and_run.cmd から起動してください。")
            return
        try:
            url, target_key, target_name = self.current_target()
        except Exception as exc:
            QMessageBox.warning(self, "取得対象が必要です", str(exc))
            return

        profile = self.target_store.ensure(target_key, "x", target_name)
        if profile.likes_anchor_at:
            ans = QMessageBox.question(
                self,
                "Likesアンカーを作り直しますか？",
                "すでにLikesアンカーがあります。再作成すると現在のLikes先頭付近を新しい境界として保存します。\n\n続行しますか？",
            )
            if ans != QMessageBox.Yes:
                return

        reserve, ok = QInputDialog.getInt(
            self,
            "Likesアンカー作成",
            "現在のLikes先頭だけを確認し、境界用アンカーを保存します（DLなし）。\n"
            "Hitomi停止後にすでにいいねした分を今すぐ取得したい場合は、\n"
            "最新何件のメディア投稿を『新規扱い』としてアンカーから除外しますか？\n\n"
            "通常は 0 でOKです。",
            0, 0, 500, 1,
        )
        if not ok:
            return

        idx = self.account_combo.currentData()
        account_name = "認証なし"; auth_mode = "none"; auth_value = ""
        if isinstance(idx, int) and 0 <= idx < len(self.accounts):
            a = self.accounts[idx]
            account_name = a.name; auth_mode = a.auth_mode; auth_value = a.auth_value

        if self.has_pending_likes_job(target_key):
            QMessageBox.information(self, "Likes処理中", "この取得対象のLikes処理はすでにキューまたは実行中です。")
            return
        cmd = build_x_likes_anchor_command(
            self.engine_command(), url=url, auth_mode=auth_mode, auth_value=auth_value,
            reserve_latest_posts=int(reserve), anchor_posts=self.likes_anchor_spin.value(),
        )
        title = f"X / {account_name} / {target_name} / Likesアンカー作成（DLなし）"
        job = DownloadJob(title, cmd)
        job.smc_context = {
            "job_kind": "likes_anchor",
            "platform": "x",
            "target_key": target_key,
            "target_name": target_name,
            "target_type": "likes",
            "login_name": account_name,
            "reserve_latest_posts": int(reserve),
            "anchor_posts": self.likes_anchor_spin.value(),
            "archive_scope": f"{target_key}:likes",
        }
        self.connect_job(job)
        self.jobs.append(job); self.queue_layout.addWidget(job)
        self.log.append(
            f"[LIKES ANCHOR] {target_name}: Likes先頭だけをDLせず確認します。"
            f" アンカー{self.likes_anchor_spin.value()}投稿 / 最新{reserve}投稿は新規扱いで残します。"
        )
        if self.active_job is None:
            self.start_next_job()

    def engine_command(self) -> list[str]:
        # Source build: use gallery-dl from the same venv.
        # Frozen Windows build: use the bundled gallery-dl.exe sidecar.
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
            for candidate in (base / "bin" / "gallery-dl.exe", base / "gallery-dl.exe"):
                if candidate.exists():
                    return [str(candidate)]
        return [sys.executable, "-m", "gallery_dl"]

    def build_command(self) -> tuple[list[str], str]:
        platform = self.platform_combo.currentData(); target = self.selected_target()
        url, target_key, target_name = normalize_target(platform, self.url_edit.text(), target)
        dest = Path(self.dest_edit.text().strip()).expanduser()
        dest.mkdir(parents=True, exist_ok=True)

        account_name, auth_mode, auth_value = self.current_auth()
        extensions = self.selected_extensions()

        profile = self.target_store.ensure(target_key, platform, target_name)
        if self.chk_remember_dest.isChecked():
            profile.destination = str(dest)
            profile.direct_folder = self.chk_direct_folder.isChecked()
            self.target_store.save()

        range_mode = self.range_group.checkedButton().property("key") if self.range_group.checkedButton() else "incremental"
        cmd = core_build_command(
            self.engine_command(), platform=platform, url=url, destination=dest,
            account_name=account_name, auth_mode=auth_mode, auth_value=auth_value, archive_scope=f"{target_key}:{target}",
            extensions=extensions, capture_internal_metadata=self.chk_internal_meta.isChecked(),
            use_archive=self.chk_archive.isChecked(), archive_dir=self.data_dir / "archives",
            direct_folder=self.chk_direct_folder.isChecked(), range_mode=str(range_mode),
            profile=profile, manual_date_after=self.date_after_edit.text().strip(), overlap_minutes=10,
            hitomi_compat_x=self.chk_hitomi_name.isChecked(), target_type=target,
        )
        title = f"{platform.upper()} / {account_name} / {target_name} / {target}"
        return cmd, title

    def preview_command(self):
        try:
            if self.is_x_likes_incremental():
                cmd, _title, _ctx = self.build_likes_probe()
                safe = redact_command(cmd)
                self.log.append("[LIKES] 1段目はアンカー探索（DLなし）。アンカー手前の投稿だけ2段目で直接DLします。")
            else:
                cmd, _ = self.build_command()
                safe = redact_command(cmd)
            self.log.append("$ " + " ".join(f'\"{x}\"' if " " in x else x for x in safe))
        except Exception as e:
            QMessageBox.warning(self, "入力エラー", str(e))

    def enqueue_download(self):
        if not self.engine_available():
            QMessageBox.warning(self, "gallery-dl がありません", "setup_and_run.cmd から起動するか、requirements.txt をインストールしてください。")
            return
        if self.is_x_likes_incremental():
            self.enqueue_likes_probe()
            return
        try:
            cmd, title = self.build_command()
        except Exception as e:
            QMessageBox.warning(self, "入力エラー", str(e)); return
        job = DownloadJob(title, cmd)
        try:
            _url, target_key, target_name = self.current_target()
        except Exception:
            target_key, target_name = "", ""
        idx = self.account_combo.currentData()
        login_name = self.accounts[idx].name if isinstance(idx, int) and 0 <= idx < len(self.accounts) else "認証なし"
        job.smc_context = {
            "platform": self.platform_combo.currentData(), "target_key": target_key, "target_name": target_name,
            "target_type": self.selected_target(), "destination": self.dest_edit.text().strip(), "login_name": login_name,
            "started_at": iso_utc(utc_now()), "started_epoch": time.time(),
            "hitomi_compat": self.chk_hitomi_name.isChecked(),
            "direct_folder": self.chk_direct_folder.isChecked(),
        }
        self.connect_job(job)
        self.jobs.append(job); self.queue_layout.addWidget(job)
        self.log.append(f"[QUEUE] {title}\n  {cmd[-1]}")
        if self.active_job is None: self.start_next_job()

    def start_next_job(self):
        if self._closing:
            return
        for job in self.jobs:
            if job.status.text() == "待機中":
                self.active_job = job
                self.footer_status.setText(f"実行中: {job.title}")
                job.start(); return
        self.active_job = None; self.footer_status.setText("準備完了")

    def classify_likes(self, ctx, records):
        apath = archive_path_for(self.data_dir / "archives", platform="x",
                                 account_name=ctx.get("login_name", "認証なし"),
                                 archive_scope=ctx.get("archive_scope", "likes"))
        return partition_likes_records(
            records, apath if ctx.get("use_archive", True) else None,
            destination=Path(ctx.get("destination") or self.data_dir / "Library"),
            hitomi_compat=bool(ctx.get("hitomi_compat", True) and ctx.get("direct_folder", True)))

    def commit_likes_boundary(self, ctx, records):
        key, login = ctx["target_key"], ctx.get("login_name", "")
        old = self.catalog.likes_anchor_ids(target_key=key, login_profile=login)
        rotated = self.catalog.rotate_likes_anchors(
            records, target_key=key, login_profile=login, max_posts=int(ctx.get("anchor_posts", 50)))
        actual = self.catalog.likes_anchor_ids(target_key=key, login_profile=login)
        if actual != rotated["post_ids"]:
            raise RuntimeError("アンカーの保存後確認に失敗しました")
        seen = self.catalog.record_likes_seen(records, target_key=key, login_profile=login)
        profile = self.target_store.ensure(key, "x", ctx.get("target_name", ""))
        profile.likes_anchor_at = iso_utc(utc_now())
        profile.likes_anchor_login = login
        profile.likes_anchor_posts = len(actual)
        profile.likes_seen_media = seen["media_total"]
        profile.likes_seen_posts = seen["posts_total"]
        self.target_store.save()
        self.log.append(f"[LIKES ANCHOR MOVE] {old[:1]} → {actual[:1]} / DB再読込確認 OK")

    def job_finished(self, job: DownloadJob, code: int):
        ctx = getattr(job, "smc_context", {})
        logical_ok = not job.cancelled and (code == 0 or (ctx.get("job_kind") == "likes_probe" and bool(job.anchor_hit)))
        self.log.append(f"[{'OK' if logical_ok else 'ERROR'}] {job.title}")
        for line in job.output_lines[-80:]:
            self.log.append(line)

        if job.reported_file_paths or job.verified_file_paths:
            self.log.append(
                f"[SAVE VERIFY] gallery-dl通知 {len(job.reported_file_paths):,}件 / "
                f"実在する新規ファイル {len(job.verified_file_paths):,}件"
            )
            for saved in job.verified_file_paths[-20:]:
                self.log.append(f"[SAVE PATH] {saved}")
            if job.missing_reported_paths:
                self.log.append(
                    f"[SAVE VERIFY WARNING] 通知は来たが新規ファイルとして確認できないパス "
                    f"{len(job.missing_reported_paths):,}件"
                )
                for missing in job.missing_reported_paths[-10:]:
                    self.log.append(f"[MISSING PATH] {missing}")

        save_integrity_ok = not job.verification_error and not job.cancelled and safe_to_advance_download_state(
            len(job.reported_file_paths), len(job.missing_reported_paths)
        )
        if not save_integrity_ok:
            self.log.append(
                "[SAVE INTEGRITY STOP] gallery-dlの保存通知と実ファイルが一致しないため、"
                "Likesアンカー/最終取得基準を進めません。次回も同じ差分を再確認できます。"
            )

        if ctx.get("job_kind") == "likes_anchor":
            if logical_ok:
                try:
                    records: list[LikeSeenRecord] = []
                    for line in job.machine_lines:
                        rec = parse_smc_like_seen_line(line)
                        if rec:
                            records.append(rec)
                    anchors, reserved = select_likes_anchor_records(
                        records,
                        reserve_latest_posts=int(ctx.get("reserve_latest_posts", 0)),
                        anchor_posts=int(ctx.get("anchor_posts", 50)),
                    )
                    if not anchors:
                        raise ValueError("アンカーにできるLikesメディア投稿を検出できませんでした。")
                    anchor_state = self.catalog.replace_likes_anchors(
                        anchors,
                        target_key=ctx.get("target_key", ""),
                        login_profile=ctx.get("login_name", "認証なし"),
                        max_posts=int(ctx.get("anchor_posts", 50)),
                    )
                    apath = archive_path_for(
                        self.data_dir / "archives",
                        platform="x",
                        account_name=ctx.get("login_name", "認証なし"),
                        archive_scope=ctx.get("archive_scope", f"{ctx.get('target_key', '')}:likes"),
                    )
                    archive_result = import_x_likes_seen_archive(anchors, apath)
                    seen = self.catalog.record_likes_seen(
                        anchors,
                        target_key=ctx.get("target_key", ""),
                        login_profile=ctx.get("login_name", "認証なし"),
                        state="seen",
                    )
                    profile = self.target_store.ensure(
                        ctx.get("target_key", ""), "x", ctx.get("target_name", "")
                    )
                    now = iso_utc(utc_now())
                    profile.likes_anchor_at = now
                    profile.likes_anchor_login = ctx.get("login_name", "認証なし")
                    profile.likes_anchor_posts = int(anchor_state.get("posts", 0))
                    profile.likes_anchor_media = len(anchors)
                    # Keep legacy fields populated so v0.1.6 state readers do not break.
                    profile.likes_baseline_at = now
                    profile.likes_baseline_login = ctx.get("login_name", "認証なし")
                    profile.likes_seen_media = int(seen.get("media_total", 0))
                    profile.likes_seen_posts = int(seen.get("posts_total", 0))
                    self.target_store.save()
                    self.sync_target_ui()
                    reserved_posts = len({r.post_id for r in reserved})
                    self.log.append(
                        f"[LIKES ANCHOR OK] 走査 {len(records):,}メディア / "
                        f"アンカー {profile.likes_anchor_posts:,}投稿・{profile.likes_anchor_media:,}メディア / "
                        f"archive新規 {archive_result['archive_added']:,} / "
                        f"新規扱いで残した {reserved_posts:,}投稿"
                    )
                    self.log.append(
                        "[LIKES] 次回から先頭を確認し、保存したアンカーに当たった瞬間に走査を止めます。"
                    )
                except Exception as exc:
                    self.log.append(f"[LIKES ANCHOR ERROR] {exc}")
            else:
                self.log.append(
                    "[LIKES ANCHOR] 走査が失敗・中断したため、部分結果はアンカーへ登録していません。"
                )
            self.active_job = None
            self.sync_target_ui()
            self.start_next_job()
            return

        if ctx.get("job_kind") == "likes_probe":
            try:
                if not logical_ok:
                    raise ValueError("差分確認が失敗・中断しました。取得ジョブは作成しません。")
                records: list[LikeSeenRecord] = []
                for line in job.machine_lines:
                    rec = parse_smc_like_seen_line(line)
                    if rec:
                        records.append(rec)
                boundary = find_likes_anchor_boundary(records, ctx.get("anchor_ids", []))
                if not boundary.get("found"):
                    job.status.setText("確認保留 / アンカー未検出・ダウンロードなし")
                    self.log.append(
                        f"[LIKES SAFE STOP] 探索上限内でアンカーを検出できませんでした "
                        f"（確認 {len(records):,}メディア）。昔削除した画像を復活させないため、"
                        "今回は1件もダウンロードしません。アンカーを作り直してください。"
                    )
                else:
                    new_records = list(boundary.get("records_before_anchor") or [])
                    new_posts = int(boundary.get("new_posts") or 0)
                    self.log.append(
                        f"[LIKES PROBE OK] アンカー {boundary.get('anchor_post_id')} に到達。"
                        f"手前に候補 {new_posts:,}投稿 / {len(new_records):,}メディア。"
                    )
                    known, pending = self.classify_likes(ctx, new_records)
                    self.log.append(f"[LIKES CHECK] 取得済み {len(known):,} / 未取得候補 {len(pending):,}メディア")
                    if not pending:
                        if new_records:
                            self.commit_likes_boundary(ctx, new_records)
                        job.status.setText(f"新規なし / 取得済み {len(known):,}メディア")
                        self.log.append("[LIKES] 新しい取得対象はありません。ダウンロード不要です。")
                    else:
                        ctx["probe_all_records"] = new_records
                        next_job = self.make_likes_download_job(ctx, pending)
                        try:
                            pos = self.jobs.index(job) + 1
                        except ValueError:
                            pos = len(self.jobs)
                        self.jobs.insert(pos, next_job)
                        self.queue_layout.insertWidget(pos, next_job)
                        self.log.append(
                            f"[LIKES QUEUE] 未取得候補を含む {len(likes_post_urls(pending)):,}投稿を確認します。"
                        )
            except Exception as exc:
                job.status.setText("停止 / 次回再確認" if job.cancelled else "差分確認エラー / ダウンロードなし")
                self.log.append(f"[LIKES PROBE ERROR] {exc}")
            self.active_job = None
            self.sync_target_ui()
            self.start_next_job()
            return

        if logical_ok and save_integrity_ok and ctx.get("job_kind") == "likes_download":
            try:
                records = list(ctx.get("probe_new_records") or [])
                known, pending = self.classify_likes(ctx, records)
                if pending:
                    save_integrity_ok = False
                    job.status.setText(f"保存 {job.files_saved_count:,}件 / 未確認 {len(pending):,}メディア・次回再確認")
                    self.log.append("[LIKES HOLD] 未確認または形式フィルター対象外のメディアがあります。境界を保持します。")
                elif records:
                    self.commit_likes_boundary(ctx, records)
            except Exception as exc:
                save_integrity_ok = False
                job.status.setText("取得状態の確認エラー / 次回再確認")
                self.log.append(f"[LIKES HOLD] {exc}")

        if code == 0 and ctx.get("target_key") and save_integrity_ok:
            profile = self.target_store.ensure(ctx["target_key"], ctx.get("platform", ""), ctx.get("target_name", ""))
            profile.last_scan_started_at = ctx.get("started_at", "")
            profile.last_success_at = iso_utc(utc_now())
            try:
                meta_count = 0
                last_author_id = ""
                last_author_name = ""
                like_records: list[LikeSeenRecord] = []
                if ctx.get("platform") == "x":
                    for line in job.machine_lines:
                        meta = parse_smc_x_meta_line(line)
                        if not meta:
                            continue
                        row = self.catalog.upsert_x_event(
                            meta, target_key=ctx.get("target_key", ""), login_profile=ctx.get("login_name", ""),
                            destination=Path(ctx.get("destination", "")), hitomi_compat=bool(ctx.get("hitomi_compat")),
                        )
                        meta_count += 1
                        last_author_id = row.get("author_id") or last_author_id
                        last_author_name = row.get("author_name") or last_author_name
                        if ctx.get("target_type") == "likes":
                            like_records.append(LikeSeenRecord(
                                post_id=str(meta.get("post_id") or ""),
                                media_num=int(meta.get("media_num") or 0),
                                author_id=str(meta.get("author_id") or ""),
                                author_name=str(meta.get("author_name") or ""),
                                post_date=str(meta.get("post_date") or ""),
                                extension=str(meta.get("extension") or ""),
                            ))
                    if ctx.get("target_type") == "likes" and like_records:
                        seen = self.catalog.record_likes_seen(
                            like_records,
                            target_key=ctx.get("target_key", ""),
                            login_profile=ctx.get("login_name", ""),
                            state="seen",
                        )
                        profile.likes_seen_media = int(seen.get("media_total", profile.likes_seen_media))
                        profile.likes_seen_posts = int(seen.get("posts_total", profile.likes_seen_posts))
                    elif ctx.get("target_type") != "likes":
                        if last_author_id:
                            profile.author_id = last_author_id
                        if last_author_name:
                            profile.author_name = last_author_name

                author_count = self.catalog.export_authors_csv(self.data_dir / "authors.csv")
                self.log.append(
                    f"[CATALOG] 内部メタデータ {meta_count}件を登録 / authors.csv {author_count}アカウント "
                    "（画像フォルダにJSONは作成していません）"
                )
            except Exception as exc:
                self.log.append(f"[CATALOG ERROR] {exc}")
            self.target_store.save()
            self.sync_target_ui()
            self.refresh_recent_downloads()
        elif code == 0 and ctx.get("target_key") and not save_integrity_ok:
            self.log.append("[RANGE] 保存実在確認に失敗したため取得基準は更新していません。")
        elif code != 0:
            self.log.append("[RANGE] 失敗したため最終取得時刻は更新していません。次回も同じ範囲を再確認します。")

        self.active_job = None; self.start_next_job()

    def stop_active(self):
        if self.active_job: self.active_job.stop()

    def start_pixiv_oauth(self):
        if not self.engine_available():
            QMessageBox.warning(self, "gallery-dl がありません", "先に setup_and_run.cmd で依存関係を入れてください。")
            return
        # OAuth needs interactive terminal/browser handling. Launch a visible console on Windows.
        if sys.platform.startswith("win"):
            import subprocess
            engine = self.engine_command()
            subprocess.Popen(["cmd", "/k"] + engine + ["oauth:pixiv"], cwd=str(self.data_dir))
            self.log.append("[OAUTH] pixiv OAuth用コンソールを開きました。表示される案内に従ってください。")
        else:
            QMessageBox.information(self, "pixiv OAuth", "Windowsテスト版では別コンソールでOAuthを開始します。\nこの環境ではターミナルから `python -m gallery_dl oauth:pixiv` を実行してください。")

    def engine_available(self) -> bool:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
            return (base / "bin" / "gallery-dl.exe").exists() or (base / "gallery-dl.exe").exists()
        import importlib.util
        return importlib.util.find_spec("gallery_dl") is not None

    def refresh_engine_status(self):
        if self.engine_available():
            self.engine_badge.setText("gallery-dl: ✓ 利用可能")
            self.engine_badge.setStyleSheet("color:#68d391;")
        else:
            self.engine_badge.setText("gallery-dl: 未インストール")
            self.engine_badge.setStyleSheet("color:#f6ad55;")

    def closeEvent(self, event):
        self._closing = True
        if hasattr(self, "_session_timer"):
            self._session_timer.stop()
            self.save_session()
        if self.active_job:
            job = self.active_job
            job.stop()
            if not job.process.waitForFinished(1500):
                job.process.kill()
                job.process.waitForFinished(1000)
        self.settings.setValue("data_dir", str(self.data_dir))
        try:
            self.catalog.close()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(DARK_QSS)
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
