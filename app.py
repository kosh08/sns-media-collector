from __future__ import annotations

import json
import codecs
import uuid
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QProcess, QProcessEnvironment, QSettings, QSize, QThread, QTimer, QUrl, QUrlQuery, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontMetrics, QImageReader, QPixmap

from core import (
    Catalog, LikeSeenRecord, PostRecord, TargetProfile, TargetStore, append_urls, archive_path_for, build_command as core_build_command,
    build_x_bookmark_scan_command, find_bookmark_boundary,
    build_x_likes_anchor_command, build_x_likes_probe_command, find_likes_anchor_boundary,
    import_hitomi_x_archive, import_x_likes_seen_archive, incremental_baseline, iso_utc,
    likes_post_urls, normalize_target, parse_iso, parse_smc_file_line, parse_smc_like_seen_line, parse_smc_x_meta_line,
    redact_command, select_likes_anchor_records, snapshot_media_files, new_media_since_snapshot, normalized_local_path,
    MEDIA_EXTENSIONS, safe_to_advance_download_state, utc_now, atomic_write_json, partition_likes_records,
    parse_smc_post_line, write_post_markdown,
)
from collection_profiles import CollectionProfile, CollectionStore
from recovery import find_recovery_candidate, has_user_profile_data, restore_candidate

from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QInputDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QProgressBar,
    QRadioButton, QScrollArea, QSpinBox, QSplitter,
    QTextEdit, QToolButton, QVBoxLayout, QWidget
)
from updater_core import download_update, fetch_latest_update
from auth_store import (
    BrowserCookie, classify_pixiv_auth_test, classify_x_auth_test,
    import_netscape_cookie_file, inspect_netscape_cookie_file, inspect_pixiv_cache,
    managed_pixiv_cache_path, managed_pixiv_web_profile_dir,
    managed_x_cookie_path, managed_x_web_profile_dir, write_netscape_cookie_file,
)

APP_NAME = "SNS Media Collector"
APP_VERSION = "0.4.8"


def resolved_test_data_dir() -> str:
    """Allow test-data isolation in source tests and explicit packaged self-tests only."""
    value = os.environ.get("SMC_TEST_DATA_DIR", "").strip()
    if not value:
        return ""
    if getattr(sys, "frozen", False) and os.environ.get("SMC_PACKAGED_SELF_TEST") != "1":
        return ""
    return value


def is_ephemeral_test_path(value: str | Path) -> bool:
    """Recognize our own disposable test destinations without matching normal folders."""
    normalized = str(value or "").strip().replace("\\", "/").lower()
    if not normalized:
        return False
    in_temp = (
        "/appdata/local/temp/" in normalized
        or "/windows/temp/" in normalized
        or normalized.startswith("/tmp/")
    )
    marker = re.search(
        r"(?:^|/)(?:smc-(?:smoke|regression|portable-check|release-build|install-check)-)[^/]+",
        normalized,
    )
    return bool(in_temp and marker)


def resolved_application_data_dir(
    test_data_dir: str, configured_value: str, home_dir: Path | None = None,
) -> tuple[Path, str]:
    """Return the usable data root and a leaked temporary root that was repaired."""
    if test_data_dir:
        return Path(test_data_dir), ""
    default = Path(home_dir or Path.home()) / "SNSMediaCollector"
    configured = Path(str(configured_value or default))
    if is_ephemeral_test_path(configured):
        return default, str(configured)
    return configured, ""


class UpdateCheckWorker(QThread):
    found = Signal(dict)
    current = Signal()
    failed = Signal(str)

    def run(self):
        try:
            info = fetch_latest_update(APP_VERSION)
            self.found.emit(info) if info else self.current.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateDownloadWorker(QThread):
    progress_changed = Signal(int)
    ready = Signal(str, str)
    failed = Signal(str)

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = dict(info)

    def run(self):
        try:
            folder = Path(tempfile.gettempdir()) / "SNSMediaCollector" / "updates" / self.info["version"]
            path = download_update(
                self.info,
                folder / self.info["name"],
                self.progress_changed.emit,
            )
            self.ready.emit(str(path), self.info["version"])
        except Exception as exc:
            self.failed.emit(str(exc))


DARK_QSS = r"""
QWidget { background: transparent; color: #e9eef7; font-size: 13px; }
QMainWindow, QDialog { background: #090e15; }
QLabel { background: transparent; }
QFrame#appHeader { background: #0c121b; border-bottom: 1px solid #222e40; }
QFrame#appFooter { background: #0c121b; border-top: 1px solid #222e40; }
QFrame#card { background: #141c27; border: 1px solid #263449; border-radius: 12px; }
QFrame#card QLabel { background-color: transparent; }
QFrame#sidebar { background: #0f1620; border-right: 1px solid #253146; }
QFrame#queueSidebar { background: #0f1620; border-left: 1px solid #253146; }
QFrame#sideSection { background: #131c28; border: 1px solid #263449; border-radius: 10px; }
QFrame#thumb { background: #101722; border: 1px solid #263246; border-radius: 8px; }
QFrame#thumb:hover { border: 1px solid #4b83f5; }
QLabel#title { font-size: 20px; font-weight: 700; color: #f5f8ff; }
QLabel#editorTitle { font-size: 18px; font-weight: 700; color: #ffffff; }
QLabel#muted { color: #8f9bb0; }
QLabel#section { font-weight: 700; font-size: 14px; color: #f4f7fc; }
QLabel#step {
    color: #b8d1ff; background: #17243a; border-left: 3px solid #6096ef;
    border-radius: 4px; font-weight: 700; font-size: 13px; padding: 7px 10px;
}
QLabel#statusPill {
    color: #99e6bc; background: #123124; border: 1px solid #245b43;
    border-radius: 10px; padding: 3px 9px; font-weight: 600;
}
QLineEdit, QComboBox, QTextEdit, QSpinBox {
    background: #0f1621; border: 1px solid #2a3850; border-radius: 6px;
    padding: 7px; selection-background-color: #2d6cdf;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus { border: 2px solid #5f96ff; padding: 6px; }
QComboBox QAbstractItemView {
    background: #121a26; border: 1px solid #354968; outline: none;
    selection-background-color: #2d6cdf; selection-color: #ffffff;
}
QComboBox QAbstractItemView::item { min-height: 30px; padding: 4px 8px; }
QComboBox QAbstractItemView::item:hover { background: #20395f; }
QPushButton, QToolButton {
    background: #202a3a; border: 1px solid #31405a; border-radius: 6px;
    padding: 7px 12px;
}
QPushButton:hover, QToolButton:hover { background: #29364a; }
QPushButton#primary { background: #2d6cdf; border: 1px solid #427ef0; font-weight: 700; }
QPushButton#primary:hover { background: #3b79ea; }
QPushButton#danger { background: #672f39; border: 1px solid #8a3f4c; }
QPushButton#ghost { background: transparent; border-color: #2a3850; color: #aebbd0; }
QPushButton#ghost:hover { background: #1a2638; color: #ffffff; }
QPushButton#inbox {
    background: #1d3151; border: 1px solid #3d6399; color: #dceaff;
    font-weight: 700; text-align: left; padding: 9px 12px;
}
QPushButton#inbox:hover { background: #25436f; }
QPushButton:disabled { color: #69758a; background: #171e29; border-color: #263246; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item {
    color: #b8c3d5; background: transparent; border: 1px solid transparent;
    padding: 10px 11px; margin: 3px 0; border-radius: 7px;
}
QListWidget::item:hover { color: #ffffff; background: #1b2a40; border-color: #304665; }
QListWidget::item:selected {
    color: #ffffff; background: #244f91; border: 1px solid #79a9ff;
    font-weight: 700;
}
QRadioButton {
    color: #b7c2d4; background: #111925; border: 1px solid #2c3b52;
    border-radius: 7px; padding: 8px 12px; spacing: 8px;
}
QRadioButton:hover { color: #ffffff; background: #1b2b43; border-color: #4f6f9f; }
QRadioButton:checked {
    color: #ffffff; background: #285aa8; border: 2px solid #79a9ff;
    padding: 7px 11px; font-weight: 700;
}
QRadioButton::indicator { width: 14px; height: 14px; }
QRadioButton::indicator:unchecked {
    background: #0b111a; border: 2px solid #70809a; border-radius: 8px;
}
QRadioButton::indicator:checked {
    background: #ffffff; border: 4px solid #4f8dff; border-radius: 8px;
}
QCheckBox {
    color: #b7c2d4; background: transparent; border: 1px solid transparent;
    border-radius: 6px; padding: 5px 7px; spacing: 8px;
}
QCheckBox:hover { color: #ffffff; background: #19283d; border-color: #304665; }
QCheckBox:checked {
    color: #ffffff; background: #1d3e70; border-color: #4f83ce; font-weight: 600;
}
QCheckBox::indicator { width: 15px; height: 15px; }
QToolButton#disclosure { background: transparent; border: none; color: #aebbd0; text-align: left; padding: 6px 2px; }
QToolButton#disclosure:hover { color: #ffffff; background: transparent; }
QToolButton#disclosure:checked { color: #dce9ff; background: transparent; border: none; font-weight: 700; }
QToolButton:checked { color: #ffffff; background: #244f91; border-color: #6096e8; font-weight: 700; }
QProgressBar { border: 1px solid #2a3850; border-radius: 5px; text-align: center; background: #0f1621; }
QProgressBar::chunk { background: #2d6cdf; border-radius: 4px; }
QSplitter::handle { background: #090e15; width: 5px; }
QScrollBar:vertical { background: #101722; width: 10px; margin: 0px; }
QScrollBar::handle:vertical { background: #35445f; min-height: 28px; border-radius: 5px; }
"""


@dataclass
class AccountProfile:
    name: str
    platform: str  # x | pixiv
    auth_mode: str = "none"  # none | managed_x | managed_pixiv | cookies_file | browser | pixiv_token
    auth_value: str = ""
    user_id: str = ""
    username: str = ""
    profile_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class XLoginDialog(QDialog):
    """Isolated X login whose cookies are exported only to one account file."""
    def __init__(self, data_dir: Path, profile_id: str, cookie_path: Path, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.cookie_path = Path(cookie_path)
        self._cookies: dict[tuple[str, str, str], BrowserCookie] = {}
        self.setWindowTitle("Xログイン / Cookie更新")
        self.resize(1040, 760)
        layout = QVBoxLayout(self)
        note = QLabel(
            "この画面はこのアカウント専用です。Xへログインしたら、下の「このログインを保存」を押してください。\n"
            "Cookie値はログへ表示せず、SNS Media Collectorのローカルデータ内だけに保存します。"
        )
        note.setWordWrap(True); note.setObjectName("muted")
        layout.addWidget(note)
        self.status = QLabel("Xのログイン状態を確認してください。")
        layout.addWidget(self.status)
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except Exception as exc:
            raise RuntimeError(f"アプリ内ログイン機能を読み込めませんでした: {exc}") from exc
        web_root = managed_x_web_profile_dir(data_dir, profile_id)
        web_root.mkdir(parents=True, exist_ok=True)
        self.web_profile = QWebEngineProfile(f"smc-x-{profile_id}", self)
        self.web_profile.setPersistentStoragePath(str(web_root / "storage"))
        self.web_profile.setCachePath(str(web_root / "cache"))
        self.web_profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self.cookie_store = self.web_profile.cookieStore()
        self.cookie_store.cookieAdded.connect(self._cookie_added)
        self.cookie_store.cookieRemoved.connect(self._cookie_removed)
        self.page = QWebEnginePage(self.web_profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        layout.addWidget(self.view, 1)
        buttons = QHBoxLayout()
        login = QPushButton("ログイン画面")
        login.clicked.connect(lambda: self.view.setUrl(QUrl("https://x.com/i/flow/login")))
        home = QPushButton("Xホーム")
        home.clicked.connect(lambda: self.view.setUrl(QUrl("https://x.com/home")))
        reset = QPushButton("この画面のログインを消去")
        reset.setObjectName("danger")
        reset.clicked.connect(self._reset_login)
        self.save_button = QPushButton("このログインを保存")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._collect_and_save)
        close = QPushButton("閉じる"); close.clicked.connect(self.reject)
        buttons.addWidget(login); buttons.addWidget(home); buttons.addWidget(reset)
        buttons.addStretch(); buttons.addWidget(close); buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.cookie_store.loadAllCookies()
        self.view.setUrl(QUrl("https://x.com/i/flow/login"))

    def _cookie_added(self, cookie):
        try:
            domain = str(cookie.domain())
            if not domain.lstrip(".").lower().endswith(("x.com", "twitter.com")):
                return
            name = bytes(cookie.name()).decode("utf-8", "replace")
            value = bytes(cookie.value()).decode("utf-8", "replace")
            path = str(cookie.path()) or "/"
            expiration = cookie.expirationDate()
            expires = expiration.toSecsSinceEpoch() if expiration.isValid() else 0
            record = BrowserCookie(domain, path, bool(cookie.isSecure()), int(expires), name, value)
            self._cookies[(domain, path, name)] = record
            if name == "auth_token":
                self.status.setText("✓ XのログインCookieを検出しました。保存できます。")
        except Exception:
            return

    def _cookie_removed(self, cookie):
        try:
            domain = str(cookie.domain())
            name = bytes(cookie.name()).decode("utf-8", "replace")
            path = str(cookie.path()) or "/"
            self._cookies.pop((domain, path, name), None)
            if name == "auth_token":
                self.status.setText("Xからログアウトしました。再ログインしてから保存してください。")
        except Exception:
            return

    def _reset_login(self):
        answer = QMessageBox.question(
            self,
            "このログインを消去",
            "このアカウント専用画面のXログインと、保存済みCookieを消去します。\n\n続行しますか？",
        )
        if answer != QMessageBox.Yes:
            return
        self._cookies.clear()
        self.cookie_store.deleteAllCookies()
        try:
            self.cookie_path.unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "ログインを消去できません", str(exc))
            return
        self.status.setText("ログインを消去しました。使用するXアカウントでログインしてください。")
        self.view.setUrl(QUrl("https://x.com/i/flow/login"))

    def _collect_and_save(self):
        self.save_button.setEnabled(False)
        self.status.setText("Cookieを確認しています…")
        self.cookie_store.loadAllCookies()
        QTimer.singleShot(900, self._finish_save)

    def _finish_save(self):
        try:
            result = write_netscape_cookie_file(self.cookie_path, self._cookies.values())
            self.status.setText(f"✓ このアカウント専用Cookieを保存しました（{result['cookie_count']}件）")
            QMessageBox.information(self, "Xログインを保存", "アカウント専用Cookieを保存しました。")
            self.accept()
        except Exception as exc:
            self.save_button.setEnabled(True)
            self.status.setText("XのログインCookieを保存できませんでした。")
            QMessageBox.warning(self, "Xログインが必要です", str(exc))


def pixiv_callback_code(url: QUrl) -> str:
    """Return a pixiv OAuth code from either known callback form."""
    is_https_callback = (
        url.scheme() in {"http", "https"}
        and url.path().endswith("/web/v1/users/auth/pixiv/callback")
    )
    is_app_callback = (
        url.scheme() == "pixiv"
        and url.host() == "account"
        and url.path().rstrip("/") == "/login"
    )
    if not (is_https_callback or is_app_callback):
        return ""
    return QUrlQuery(url).queryItemValue("code").strip()


def sanitized_pixiv_oauth_diagnostic(output: str, returncode: int) -> str:
    """Create a useful OAuth failure summary without exposing credentials."""
    text = str(output or "")
    text = re.sub(
        r"((?:[?&]|\b)(?:code|token|secret|verifier|challenge)=)[^&\s]+",
        r"\1<redacted>", text, flags=re.I,
    )
    text = re.sub(r"(?i)(refresh[-_ ]?token\s*(?:is|:|=)?\s*)\S+", r"\1<redacted>", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful = [line for line in lines if any(word in line.lower() for word in (
        "error", "failed", "expired", "invalid", "abort", "eof", "exception", "traceback",
    ))]
    detail = " / ".join((useful or lines)[-4:])
    return f"終了コード {int(returncode)}" + (f"：{detail[:700]}" if detail else "")


def pixiv_oauth_command(engine: list[str], cache_path: Path) -> list[str]:
    """Build the OAuth command, enabling pipe input for our frozen sidecar."""
    command = list(engine)
    executable_name = Path(str(command[0]).replace("\\", "/")).name.lower() if command else ""
    if executable_name == "gallery-dl.exe":
        command.append("--smc-pixiv-stdin")
    command.extend([
        "--ignore-config", "--no-colors", "--cache-file", str(cache_path),
        "-o", "browser=false", "oauth:pixiv",
    ])
    return command


class PixivLoginDialog(QDialog):
    """Run gallery-dl's PKCE flow in an isolated embedded browser."""
    def __init__(self, data_dir: Path, profile_id: str, cache_path: Path,
                 engine: list[str], parent=None, *, persistent_web_profile: bool = True):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.data_dir = Path(data_dir)
        self.cache_path = Path(cache_path)
        self.engine = list(engine)
        self._oauth_output = ""
        self._login_url_loaded = False
        self._code_sent = False
        self._cancelled = False
        self.setWindowTitle("pixivログイン / 連携更新")
        self.resize(1040, 760)
        layout = QVBoxLayout(self)
        note = QLabel(
            "この画面はこのpixivアカウント専用です。ログイン後の認証コードはアプリが自動で受け取り、"
            "アカウント別の安全なキャッシュへ保存します。開発者ツールやコピー操作は不要です。"
        )
        note.setWordWrap(True); note.setObjectName("muted")
        layout.addWidget(note)
        self.status = QLabel("pixivの認証画面を準備しています…")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except Exception as exc:
            raise RuntimeError(f"アプリ内pixivログイン機能を読み込めませんでした: {exc}") from exc
        if persistent_web_profile:
            web_root = managed_pixiv_web_profile_dir(data_dir, profile_id)
            web_root.mkdir(parents=True, exist_ok=True)
            self.web_profile = QWebEngineProfile(f"smc-pixiv-{profile_id}", self)
            self.web_profile.setPersistentStoragePath(str(web_root / "storage"))
            self.web_profile.setCachePath(str(web_root / "cache"))
            self.web_profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
        else:
            self.web_profile = QWebEngineProfile(self)
        dialog = self
        class PixivOAuthPage(QWebEnginePage):
            def acceptNavigationRequest(page, url, navigation_type, is_main_frame):
                dialog._url_changed(url)
                if url.scheme() == "pixiv":
                    return False
                return super().acceptNavigationRequest(url, navigation_type, is_main_frame)

        self.page = PixivOAuthPage(self.web_profile, self)
        self.view = QWebEngineView(self); self.view.setPage(self.page)
        self.view.urlChanged.connect(self._url_changed)
        layout.addWidget(self.view, 1)
        buttons = QHBoxLayout(); buttons.addStretch()
        cancel = QPushButton("キャンセル"); cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel); layout.addLayout(buttons)

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process)
        self.process.finished.connect(self._process_finished)
        self.process.errorOccurred.connect(self._process_error)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        command = pixiv_oauth_command(self.engine, self.cache_path)
        self.process.start(command[0], command[1:])

    def _read_process(self):
        chunk = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        self._oauth_output = (self._oauth_output + chunk)[-65536:]
        if not self._login_url_loaded:
            match = re.search(r"https://app-api\.pixiv\.net/web/v1/login\?[^\s]+", self._oauth_output)
            if match:
                self._login_url_loaded = True
                self.status.setText("pixivへログインしてください。完了後は自動で連携します。")
                self.view.setUrl(QUrl(match.group(0)))

    def _url_changed(self, url: QUrl):
        if self._code_sent:
            return
        code = pixiv_callback_code(url)
        if not code:
            return
        self._code_sent = True
        self.status.setText("pixivの認証情報を安全に保存しています…")
        # gallery-dl accepts a callback URL only when the authorization code is
        # its final ``=value`` component.  pixiv can append other query
        # parameters, so always pass the already parsed code itself.
        self.process.write((code + "\n").encode("utf-8"))

    def _process_finished(self, code, _status):
        self._read_process()
        if self._cancelled:
            self._oauth_output = ""
            return
        authenticated = bool(inspect_pixiv_cache(self.cache_path).get("authenticated"))
        diagnostic = sanitized_pixiv_oauth_diagnostic(self._oauth_output, int(code))
        self._oauth_output = ""
        if int(code) == 0 and authenticated:
            self.status.setText("✓ pixiv連携を保存しました。")
            QMessageBox.information(self, "pixiv連携完了", "このアカウント専用のpixiv連携を保存しました。")
            self.accept()
        else:
            self.status.setText("pixiv連携を完了できませんでした。もう一度お試しください。")
            try:
                (self.data_dir / "pixiv-oauth-last.log").write_text(
                    diagnostic + "\n", encoding="utf-8"
                )
            except OSError:
                pass
            stage = "認証コードを受け取る前に認証処理が終了しました。" if not self._code_sent else "pixivが認証コードを受理しませんでした。"
            QMessageBox.warning(
                self, "pixiv連携エラー",
                f"{stage}\n{diagnostic}\n\n画面を閉じて再試行してください。",
            )

    def _process_error(self, _error):
        self.status.setText("pixiv認証用エンジンを起動できませんでした。")

    def reject(self):
        self._cancelled = True
        if self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(2000)
        self._oauth_output = ""
        super().reject()


class AccountDialog(QDialog):
    def __init__(self, parent=None, profile: Optional[AccountProfile] = None,
                 data_dir: Optional[Path] = None, engine: Optional[list[str]] = None):
        super().__init__(parent)
        self.profile_id = profile.profile_id if profile else uuid.uuid4().hex
        self._identity_platform = profile.platform if profile else "x"
        self._profile_user_id = profile.user_id if profile else ""
        self._profile_username = profile.username if profile else ""
        self.data_dir = Path(data_dir or Path.home() / "SNSMediaCollector")
        self.engine = list(engine or [])
        self._auth_process: Optional[QProcess] = None
        self.setAcceptDrops(True)
        self.setWindowTitle("アカウント追加 / 編集")
        self.resize(780, 470)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(profile.name if profile else "")
        self.platform = QComboBox()
        self.platform.addItem("X / Twitter", "x")
        self.platform.addItem("pixiv", "pixiv")
        self.auth_mode = QComboBox()
        self.auth_mode.addItem("認証なし", "none")
        self.auth_mode.addItem("アカウント別Cookie（推奨）", "managed_x")
        self.auth_mode.addItem("アプリ内pixiv連携（アカウント別・推奨）", "managed_pixiv")
        self.auth_mode.addItem("cookies.txt", "cookies_file")
        self.auth_mode.addItem("ブラウザCookie", "browser")
        self.auth_mode.addItem("pixiv refresh token", "pixiv_token")
        self.auth_value = QLineEdit(profile.auth_value if profile else "")
        self.auth_value.setPlaceholderText("cookies.txt のパス / firefox / chrome / edge / pixiv refresh token")
        self.auth_value.setEchoMode(QLineEdit.EchoMode.Password if (profile and profile.auth_mode == "pixiv_token") else QLineEdit.EchoMode.Normal)
        self.auth_mode.currentIndexChanged.connect(self._auth_mode_changed)
        self.browse = QPushButton("参照")
        self.browse.clicked.connect(self.pick_cookie)
        row = QHBoxLayout(); row.addWidget(self.auth_value); row.addWidget(self.browse)
        form.addRow("表示名", self.name_edit)
        form.addRow("サービス", self.platform)
        form.addRow("認証方式", self.auth_mode)
        form.addRow("認証値", row)
        layout.addLayout(form)
        self.x_guide = QLabel(
            "推奨手順：①通常ブラウザでXへログイン → ②Netscape形式のcookies.txtを書き出す → "
            "③ここへ取り込み。ファイルはこの画面へドラッグ＆ドロップもできます。"
        )
        self.x_guide.setObjectName("muted"); self.x_guide.setWordWrap(True)
        layout.addWidget(self.x_guide)
        self.x_auth_actions = QHBoxLayout()
        self.browser_button = QPushButton("① ブラウザでXを開く")
        self.browser_button.clicked.connect(self.open_external_x)
        self.import_button = QPushButton("② cookies.txtを取り込む")
        self.import_button.clicked.connect(self.import_cookie)
        self.test_button = QPushButton("③ X認証をテスト")
        self.test_button.clicked.connect(self.test_x_auth)
        self.login_button = QPushButton("アプリ内ログイン（予備）")
        self.login_button.clicked.connect(self.open_x_login)
        self.cookie_help_button = QPushButton("cookies.txtの作り方")
        self.cookie_help_button.clicked.connect(self.show_x_cookie_help)
        self.pixiv_login_button = QPushButton("pixivへログイン / 更新")
        self.pixiv_login_button.clicked.connect(self.open_pixiv_login)
        self.pixiv_test_button = QPushButton("pixiv認証をテスト")
        self.pixiv_test_button.clicked.connect(self.test_pixiv_auth)
        self.x_auth_actions.addWidget(self.browser_button)
        self.x_auth_actions.addWidget(self.import_button)
        self.x_auth_actions.addWidget(self.test_button)
        self.x_auth_actions.addStretch()
        layout.addLayout(self.x_auth_actions)
        self.x_secondary_actions = QHBoxLayout()
        self.x_secondary_actions.addWidget(self.cookie_help_button)
        self.x_secondary_actions.addWidget(self.login_button)
        self.x_secondary_actions.addStretch()
        layout.addLayout(self.x_secondary_actions)
        self.pixiv_auth_actions = QHBoxLayout()
        self.pixiv_auth_actions.addWidget(self.pixiv_login_button)
        self.pixiv_auth_actions.addWidget(self.pixiv_test_button)
        self.pixiv_auth_actions.addStretch()
        layout.addLayout(self.pixiv_auth_actions)
        self.auth_status = QLabel("")
        self.auth_status.setObjectName("muted"); self.auth_status.setWordWrap(True)
        layout.addWidget(self.auth_status)
        note = QLabel(
            "取り込んだCookieはアカウントごとの専用領域へコピーします。元ファイルの場所やChromeの状態には依存しません。"
            "Cookie値はログへ表示しません。アプリ内XログインはX側に拒否される場合があるため予備機能です。"
        )
        note.setObjectName("muted"); note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout(); buttons.addStretch()
        cancel = QPushButton("キャンセル"); ok = QPushButton("保存"); ok.setObjectName("primary")
        cancel.clicked.connect(self.reject); ok.clicked.connect(self.accept)
        buttons.addWidget(cancel); buttons.addWidget(ok); layout.addLayout(buttons)
        if profile:
            self.platform.setCurrentIndex(max(0, self.platform.findData(profile.platform)))
            self.auth_mode.setCurrentIndex(max(0, self.auth_mode.findData(profile.auth_mode)))
        else:
            self.auth_mode.setCurrentIndex(self.auth_mode.findData("managed_x"))
        self.platform.currentIndexChanged.connect(self._platform_changed)
        self._auth_mode_changed()

    def _platform_changed(self):
        platform = self.platform.currentData()
        mode = self.auth_mode.currentData()
        if platform == "x" and mode == "pixiv_token":
            self.auth_mode.setCurrentIndex(self.auth_mode.findData("managed_x"))
        elif platform == "x" and mode == "managed_pixiv":
            self.auth_mode.setCurrentIndex(self.auth_mode.findData("managed_x"))
        elif platform == "pixiv" and mode == "managed_x":
            self.auth_mode.setCurrentIndex(self.auth_mode.findData("managed_pixiv"))
        self._auth_mode_changed()

    def _auth_mode_changed(self):
        mode = self.auth_mode.currentData()
        is_token = mode == "pixiv_token"
        is_managed = mode == "managed_x" and self.platform.currentData() == "x"
        is_managed_pixiv = mode == "managed_pixiv" and self.platform.currentData() == "pixiv"
        if is_managed:
            self.auth_value.setText(str(managed_x_cookie_path(self.data_dir, self.profile_id)))
        elif is_managed_pixiv:
            self.auth_value.setText(str(managed_pixiv_cache_path(self.data_dir, self.profile_id)))
        self.auth_value.setEchoMode(QLineEdit.EchoMode.Password if is_token else QLineEdit.EchoMode.Normal)
        self.auth_value.setReadOnly(is_managed or is_managed_pixiv)
        self.browse.setEnabled(mode == "cookies_file")
        self.browser_button.setEnabled(is_managed)
        self.login_button.setEnabled(is_managed)
        self.import_button.setEnabled(is_managed)
        self.test_button.setEnabled(is_managed and bool(self.engine))
        self.pixiv_login_button.setEnabled(is_managed_pixiv and bool(self.engine))
        self.pixiv_test_button.setEnabled(is_managed_pixiv and bool(self.engine))
        self.x_guide.setVisible(is_managed)
        for button in (
            self.browser_button, self.login_button, self.import_button,
            self.test_button, self.cookie_help_button,
        ):
            button.setVisible(is_managed)
        for button in (self.pixiv_login_button, self.pixiv_test_button):
            button.setVisible(is_managed_pixiv)
        if is_managed or is_managed_pixiv:
            self._refresh_managed_status()
        else:
            self.auth_status.setText("")

    def _refresh_managed_status(self):
        if self.auth_mode.currentData() == "managed_pixiv":
            path = managed_pixiv_cache_path(self.data_dir, self.profile_id)
            info = inspect_pixiv_cache(path)
            if info.get("authenticated"):
                self.auth_status.setText(f"✓ アカウント専用pixiv連携を保存済み：{path}")
            else:
                self.auth_status.setText("未連携です。「pixivへログイン / 更新」を実行してください。")
            return
        path = managed_x_cookie_path(self.data_dir, self.profile_id)
        info = inspect_netscape_cookie_file(path)
        if info.get("x_auth"):
            identity = str(info.get("x_user_id") or self._profile_user_id or "")
            suffix = f" / XユーザーID {identity}" if identity else ""
            self.auth_status.setText(f"✓ アカウント専用Cookie保存済み{suffix}：{path}")
        else:
            self.auth_status.setText("未認証です。通常ブラウザでログインし、cookies.txtを取り込んでください。")

    def open_external_x(self):
        if QDesktopServices.openUrl(QUrl("https://x.com/home")):
            self.auth_status.setText(
                "ブラウザでXを開きました。使用するアカウントを確認し、Netscape形式のcookies.txtを書き出してください。"
            )
        else:
            QMessageBox.warning(self, "Xを開けません", "通常ブラウザで https://x.com/home を開いてください。")

    def show_x_cookie_help(self):
        QMessageBox.information(
            self,
            "cookies.txtの作り方",
            "1. 通常ブラウザで x.com を開き、使いたいアカウントへログインします。\n"
            "2. Cookie書き出し拡張機能で、現在のx.comをNetscape形式のcookies.txtとして保存します。\n"
            "3. この画面の「② cookies.txtを取り込む」で選ぶか、ファイルを画面へドロップします。\n\n"
            "Cookieはログイン情報そのものです。チャットやGitHubへアップロードしないでください。",
        )

    def open_x_login(self):
        try:
            dialog = XLoginDialog(
                self.data_dir, self.profile_id,
                managed_x_cookie_path(self.data_dir, self.profile_id), self,
            )
            accepted = dialog.exec() == QDialog.Accepted
            self._refresh_managed_status()
            if accepted:
                self.test_x_auth()
        except Exception as exc:
            QMessageBox.warning(self, "アプリ内Xログイン", str(exc))

    def import_cookie(self):
        source, _ = QFileDialog.getOpenFileName(
            self, "Xのcookies.txtを取り込む", "", "Cookie file (*.txt);;All files (*.*)"
        )
        if not source:
            return
        self._import_cookie_path(Path(source))

    def _import_cookie_path(self, source: Path) -> bool:
        try:
            source_info = inspect_netscape_cookie_file(Path(source))
            detected_id = str(source_info.get("x_user_id") or "")
            if self._profile_user_id and detected_id and self._profile_user_id != detected_id:
                QMessageBox.warning(
                    self,
                    "別アカウントのCookieです",
                    "この設定に保存済みのXユーザーIDと、取り込もうとしたCookieのIDが一致しません。\n\n"
                    f"設定済み：{self._profile_user_id}\n取り込み：{detected_id}\n\n"
                    "Cookieを書き出したXアカウントを確認してください。",
                )
                return False
            result = import_netscape_cookie_file(
                Path(source), managed_x_cookie_path(self.data_dir, self.profile_id)
            )
            if detected_id:
                self._profile_user_id = detected_id
                self._identity_platform = "x"
            self._refresh_managed_status()
            identity = f"\nXユーザーID：{detected_id}" if detected_id else "\nXユーザーIDはCookieから判定できませんでした。"
            QMessageBox.information(
                self, "Cookie取り込み完了",
                f"このアカウント専用として{result['cookie_count']}件を保存しました。{identity}\n認証テストを続けます。",
            )
            self.test_x_auth()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "Cookieを取り込めません", str(exc))
            return False

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if (self.platform.currentData() == "x"
                and self.auth_mode.currentData() == "managed_x"
                and any(url.isLocalFile() and url.toLocalFile().lower().endswith(".txt") for url in urls)):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".txt"):
                self._import_cookie_path(Path(url.toLocalFile()))
                event.acceptProposedAction()
                return
        event.ignore()

    def open_pixiv_login(self):
        try:
            dialog = PixivLoginDialog(
                self.data_dir,
                self.profile_id,
                managed_pixiv_cache_path(self.data_dir, self.profile_id),
                self.engine,
                self,
            )
            accepted = dialog.exec() == QDialog.Accepted
            self._refresh_managed_status()
            if accepted:
                self.test_pixiv_auth()
        except Exception as exc:
            QMessageBox.warning(self, "アプリ内pixivログイン", str(exc))

    def test_x_auth(self):
        path = managed_x_cookie_path(self.data_dir, self.profile_id)
        info = inspect_netscape_cookie_file(path)
        if not info.get("x_auth"):
            QMessageBox.warning(self, "X認証テスト", "先にXログインまたはcookies.txtの取り込みを実行してください。")
            return
        if self._auth_process and self._auth_process.state() != QProcess.NotRunning:
            return
        command = list(self.engine) + [
            "--ignore-config", "--no-input", "--cookies", str(path),
            "--range", "1", "-N", "{tweet_id}", "https://x.com/i/bookmarks",
        ]
        self._auth_test_kind = "x"
        self._auth_process = QProcess(self)
        self._auth_process.setProcessChannelMode(QProcess.MergedChannels)
        self._auth_process.finished.connect(self._auth_test_finished)
        self._auth_process.errorOccurred.connect(self._auth_test_error)
        self.test_button.setEnabled(False)
        self.auth_status.setText("X認証を確認しています…")
        self._auth_process.start(command[0], command[1:])
        QTimer.singleShot(60000, self._auth_test_timeout)

    def test_pixiv_auth(self):
        path = managed_pixiv_cache_path(self.data_dir, self.profile_id)
        if not inspect_pixiv_cache(path).get("authenticated"):
            QMessageBox.warning(self, "pixiv認証テスト", "先にpixivログインを実行してください。")
            return
        if self._auth_process and self._auth_process.state() != QProcess.NotRunning:
            return
        command = list(self.engine) + [
            "--ignore-config", "--no-input", "--cache-file", str(path),
            "-o", "refresh-token=cache", "--range", "1", "-N", "{id}",
            "https://www.pixiv.net/bookmark.php",
        ]
        self._auth_test_kind = "pixiv"
        self._auth_process = QProcess(self)
        self._auth_process.setProcessChannelMode(QProcess.MergedChannels)
        self._auth_process.finished.connect(self._auth_test_finished)
        self._auth_process.errorOccurred.connect(self._auth_test_error)
        self.pixiv_test_button.setEnabled(False)
        self.auth_status.setText("pixiv認証を確認しています…")
        self._auth_process.start(command[0], command[1:])
        QTimer.singleShot(60000, self._auth_test_timeout)

    def _auth_test_finished(self, code, _status):
        if not self._auth_process:
            return
        output = bytes(self._auth_process.readAllStandardOutput()).decode("utf-8", "replace")
        kind = getattr(self, "_auth_test_kind", "x")
        self.test_button.setEnabled(self.auth_mode.currentData() == "managed_x")
        self.pixiv_test_button.setEnabled(self.auth_mode.currentData() == "managed_pixiv")
        ok, reason = (classify_pixiv_auth_test(int(code), output)
                      if kind == "pixiv" else classify_x_auth_test(int(code), output))
        if ok:
            self.auth_status.setText(f"✓ {reason}")
            QMessageBox.information(self, "認証テスト", reason)
        else:
            self.auth_status.setText(f"✗ {reason}")
            QMessageBox.warning(self, "認証テスト", reason)

    def _auth_test_error(self, _error):
        if _error != QProcess.FailedToStart:
            return
        self.test_button.setEnabled(self.auth_mode.currentData() == "managed_x")
        self.pixiv_test_button.setEnabled(self.auth_mode.currentData() == "managed_pixiv")
        self.auth_status.setText("認証テスト用エンジンを起動できませんでした。")

    def _auth_test_timeout(self):
        if self._auth_process and self._auth_process.state() != QProcess.NotRunning:
            self._auth_process.kill()
            self.test_button.setEnabled(self.auth_mode.currentData() == "managed_x")
            self.pixiv_test_button.setEnabled(self.auth_mode.currentData() == "managed_pixiv")
            self.auth_status.setText("認証テストがタイムアウトしました。")

    def pick_cookie(self):
        path, _ = QFileDialog.getOpenFileName(self, "cookies.txt を選択", "", "Cookie file (*.txt);;All files (*.*)")
        if path:
            self.auth_value.setText(path)
            self.auth_mode.setCurrentIndex(self.auth_mode.findData("cookies_file"))

    def accept(self):
        platform = self.platform.currentData()
        mode = self.auth_mode.currentData()
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "表示名が必要です", "この認証を見分ける表示名を入力してください。")
            return
        if ((platform == "x" and mode in {"pixiv_token", "managed_pixiv"}) or
                (platform == "pixiv" and mode == "managed_x")):
            QMessageBox.warning(self, "認証方式を確認", "サービスに対応した認証方式を選択してください。")
            return
        if platform == "x" and mode == "managed_x":
            info = inspect_netscape_cookie_file(managed_x_cookie_path(self.data_dir, self.profile_id))
            if not info.get("x_auth"):
                QMessageBox.warning(
                    self,
                    "Xログインが必要です",
                    "通常ブラウザでXへログインし、cookies.txtの取り込みを完了してから保存してください。",
                )
                return
        if platform == "pixiv" and mode == "managed_pixiv":
            if not inspect_pixiv_cache(managed_pixiv_cache_path(self.data_dir, self.profile_id)).get("authenticated"):
                QMessageBox.warning(
                    self,
                    "pixivログインが必要です",
                    "「pixivへログイン / 更新」を完了してから保存してください。",
                )
                return
        super().accept()

    def result_profile(self) -> AccountProfile:
        mode = self.auth_mode.currentData()
        value = self.auth_value.text().strip()
        if mode == "managed_x":
            value = str(managed_x_cookie_path(self.data_dir, self.profile_id))
        elif mode == "managed_pixiv":
            value = str(managed_pixiv_cache_path(self.data_dir, self.profile_id))
        platform = self.platform.currentData()
        same_platform = platform == self._identity_platform
        return AccountProfile(
            profile_id=self.profile_id,
            name=self.name_edit.text().strip() or "未設定",
            platform=platform,
            auth_mode=mode,
            auth_value=value,
            user_id=self._profile_user_id if same_platform else "",
            username=self._profile_username if same_platform else "",
        )


def scaled_media_pixmap(path: Path, target_size: QSize) -> QPixmap:
    """Decode an image near its display size instead of loading it full-size."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid() and target_size.isValid():
        reader.setScaledSize(source_size.scaled(target_size, Qt.KeepAspectRatio))
    image = reader.read()
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


class ElidedLabel(QLabel):
    """One-line label that keeps both filename start and extension visible."""
    def __init__(self, text: str = "", parent=None):
        super().__init__("", parent)
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._refresh_elision()

    def setFullText(self, text: str):
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._refresh_elision()

    def _refresh_elision(self):
        width = max(1, self.contentsRect().width())
        QLabel.setText(self, QFontMetrics(self.font()).elidedText(self._full_text, Qt.ElideMiddle, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_elision()


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
        self.name_label = ElidedLabel(name)
        self.name_label.setFixedWidth(150)
        self.name_label.setWordWrap(False)
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
            pix = scaled_media_pixmap(self.path, self.preview.size())
            if not pix.isNull():
                self.preview.setPixmap(pix)
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


class MediaSnapshotWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, root: Path, recursive: bool, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.recursive = bool(recursive)

    def run(self):
        try:
            self.ready.emit(snapshot_media_files(self.root, recursive=self.recursive))
        except Exception as exc:
            self.failed.emit(str(exc))


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
        self.snapshot_worker: Optional[MediaSnapshotWorker] = None
        self._pending_preview_path = ""
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self._flush_file_preview)

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
                self.status.setText("保存先を確認中…")
                self.snapshot_worker = MediaSnapshotWorker(
                    self.verification_root, self.verification_recursive, self
                )
                self.snapshot_worker.ready.connect(self._snapshot_ready)
                self.snapshot_worker.failed.connect(self._snapshot_failed)
                self.snapshot_worker.start()
                return
            except Exception as exc:
                self.verification_error = str(exc)
                self.output_lines.append(f"[SAVE CHECK ERROR] {exc}")
                self._finished(-2, QProcess.NormalExit)
                return
        self.process.start(self.command[0], self.command[1:])

    def _snapshot_ready(self, files):
        self.preexisting_media = dict(files or {})
        if self.cancelled:
            self._finished(-3, QProcess.NormalExit)
            return
        self.status.setText("実行中")
        self.process.start(self.command[0], self.command[1:])

    def _snapshot_failed(self, message: str):
        self.verification_error = str(message)
        self.output_lines.append(f"[SAVE CHECK ERROR] {message}")
        self._finished(-2, QProcess.NormalExit)

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
            pix = scaled_media_pixmap(path, self.job_preview.size())
            if not pix.isNull():
                self.job_preview.setPixmap(pix)
                self.job_preview.setToolTip(str(path))
                return
        self.job_preview.setPixmap(QPixmap())
        self.job_preview.setText("VIDEO" if suffix in {".mp4", ".webm", ".m4v", ".mov", ".mkv"} else "MEDIA")
        self.job_preview.setToolTip(str(path))

    def _queue_file_preview(self, path_text: str):
        self._pending_preview_path = str(path_text)
        self._preview_timer.start()

    def _flush_file_preview(self):
        path = self._pending_preview_path
        self._pending_preview_path = ""
        if path:
            self._show_file_preview(path)

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
            self._queue_file_preview(str(path))
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
            recoverable_x_job = (
                ctx.get("job_kind") == "likes_download"
                or (
                    ctx.get("job_kind") == "collection_download"
                    and ctx.get("platform") == "x"
                )
            )
            if (recoverable_x_job and
                    len(self.verified_file_paths) < len(set(self.reported_file_paths))):
                expected_ids = {
                    str(getattr(rec, "post_id", "") or "")
                    for rec in (ctx.get("probe_new_records") or [])
                }
                expected_ids.update(
                    str(post_id) for post_id in (ctx.get("collection_post_ids") or [])
                )
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
        if line.startswith(("SMC_META\t", "SMC_LIKE_SEEN\t", "SMC_FILE\t", "SMC_POST\t")):
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
            elif line.startswith("SMC_POST\t"):
                self.meta_media_count += 1
                ctx = getattr(self, "smc_context", {})
                label = "Likes" if ctx.get("job_kind") == "likes_probe" else "ブックマーク"
                self.status.setText(f"{label}確認中 / {self.meta_media_count:,}投稿")
                if self.stop_on_post_ids and not self.anchor_hit:
                    parts = line.split("\t", 2)
                    if len(parts) >= 2 and parts[1] in self.stop_on_post_ids:
                        self.anchor_hit = parts[1]
                        QTimer.singleShot(0, self._request_anchor_stop)
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
        if self.process.isOpen():
            self._read()
        self._stdout_buffer += self._stdout_decoder.decode(b"", final=True)
        # Flush a final line that did not end with a newline.
        if self._stdout_buffer:
            self._consume_output_line(self._stdout_buffer)
            self._stdout_buffer = ""
        self.progress.hide()
        self._rate_timer.stop()
        self._reconcile_actual_files()
        self._preview_timer.stop()
        self._flush_file_preview()
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
        test_data_dir = resolved_test_data_dir()
        self.settings = (QSettings(str(Path(test_data_dir) / "settings.ini"), QSettings.IniFormat)
                         if test_data_dir else QSettings("MasterTools", APP_NAME))
        configured_data_dir = str(self.settings.value("data_dir", str(Path.home() / "SNSMediaCollector")))
        self.data_dir, self._repaired_data_root = resolved_application_data_dir(
            test_data_dir, configured_data_dir,
        )
        if self._repaired_data_root:
            self.settings.setValue("data_dir", str(self.data_dir))
            self.settings.sync()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._recovery_report = None
        self._recovery_settings_error = ""
        if not test_data_dir:
            # A valid canonical profile takes priority over a stranded temporary
            # profile. This restores the user's original destinations instead of
            # overwriting them with the later smoke-folder defaults.
            candidate = None if has_user_profile_data(self.data_dir) else find_recovery_candidate(self.data_dir)
            if candidate is not None:
                answer = QMessageBox.question(
                    self,
                    "一時フォルダの利用データを復旧",
                    "前回の不具合で一時フォルダに残った利用データを検出しました。\n\n"
                    f"認証アカウント: {candidate.account_count}件\n"
                    f"取得設定: {candidate.collection_count}件\n"
                    f"復旧元: {candidate.path}\n\n"
                    "取得設定・認証・差分位置・archive・保存済みファイルを通常のデータフォルダへ復旧します。\n"
                    "現在の設定は先にバックアップし、復旧元は削除しません。",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if answer == QMessageBox.Yes:
                    try:
                        report = restore_candidate(candidate, self.data_dir)
                        self._recovery_report = report
                        if report.settings_path:
                            settings_backup = {
                                key: self.settings.value(key)
                                for key in ("account_sessions", "last_session", "data_dir")
                                if self.settings.contains(key)
                            }
                            try:
                                atomic_write_json(
                                    report.backup / "settings-before-recovery.json",
                                    settings_backup,
                                )
                                old_settings = QSettings(str(report.settings_path), QSettings.IniFormat)
                                for key in ("account_sessions", "last_session"):
                                    if old_settings.contains(key):
                                        self.settings.setValue(key, old_settings.value(key))
                                self.settings.sync()
                            except Exception as exc:
                                self._recovery_settings_error = str(exc)
                    except Exception as exc:
                        QMessageBox.warning(
                            self, "復旧できませんでした",
                            "一時フォルダには変更を加えていません。\n\n" + str(exc),
                        )
        self.accounts_file = self.data_dir / "accounts.json"
        self.accounts: list[AccountProfile] = self.load_accounts()
        self.target_store = TargetStore(self.data_dir / "targets.json")
        self.catalog = Catalog(self.data_dir / "catalog.sqlite3")
        self.jobs: list[DownloadJob] = []
        self.active_job: Optional[DownloadJob] = None
        self.last_saved_path: Optional[Path] = None
        self._recent_refresh_timer = QTimer(self)
        self._recent_refresh_timer.setSingleShot(True)
        self._recent_refresh_timer.setInterval(600)
        self._recent_refresh_timer.timeout.connect(self.refresh_recent_downloads)
        self.update_check_worker: Optional[UpdateCheckWorker] = None
        self.update_download_worker: Optional[UpdateDownloadWorker] = None
        self._update_check_silent = False
        self._account_workspace_ready = False
        self._active_account_id = ""
        try:
            saved_workspaces = json.loads(str(self.settings.value("account_sessions", "{}")))
            self.account_sessions = saved_workspaces if isinstance(saved_workspaces, dict) else {}
        except (ValueError, TypeError):
            self.account_sessions = {}
        self.collection_store = CollectionStore(self.data_dir / "collections.json")
        self.collection_store.migrate_account_sessions(self.accounts, self.account_sessions)
        self._settings_repair_count = 0 if test_data_dir else self.repair_ephemeral_test_paths()
        self._active_collection_id = ""

        self.build_ui()
        self.refresh_accounts()
        self.refresh_engine_status()
        self.refresh_recent_downloads()
        self.restore_session()
        if self._settings_repair_count:
            self.log.append(
                f"[SETTINGS REPAIR] 検証用の一時保存先を {self._settings_repair_count} 箇所修復しました。"
            )
            self.footer_status.setText("検証用の一時保存先を通常の保存先へ戻しました")
        if self._recovery_report is not None:
            report = self._recovery_report
            self.log.append(
                f"[RECOVERY] {report.source} から利用データを復旧しました。"
                f" 状態ファイル {report.state_files}件 / 保存済みファイル {report.media_files}件"
            )
            if self._recovery_settings_error:
                self.log.append(
                    "[RECOVERY WARNING] 最後に開いていた画面状態だけは復元できませんでした。"
                )
            self.footer_status.setText("一時フォルダから取得設定・認証・差分位置を復旧しました")
            QMessageBox.information(
                self,
                "復旧が完了しました",
                "取得設定・認証・差分位置を通常のデータフォルダへ復旧しました。\n\n"
                f"復旧元: {report.source}\n"
                f"復旧前バックアップ: {report.backup}\n\n"
                "復旧元とバックアップは自動削除していません。取得設定と保存先を確認してから取得を再開してください。",
            )
        elif self._repaired_data_root:
            self.log.append(
                f"[DATA ROOT REPAIR] {self._repaired_data_root} を解除し、{self.data_dir} へ戻しました。"
            )
            self.footer_status.setText("検証用データフォルダを解除し、通常の設定へ戻しました")
            QMessageBox.information(
                self,
                "通常のデータフォルダへ戻しました",
                "検証用の一時フォルダがデータ保存先として残っていたため解除しました。\n\n"
                f"現在のデータフォルダ: {self.data_dir}\n\n"
                "既存のアカウントと取得設定を優先して読み込んでいます。保存先を確認してから取得を再開してください。",
            )
        restored_account_id = ""
        try:
            restored_account_id = str(json.loads(str(self.settings.value("last_session", "{}"))).get("account_id") or "")
        except (ValueError, TypeError, AttributeError):
            pass
        initial_collection = next(
            (x.collection_id for x in self.collection_store.items if x.account_id == self._active_account_id), ""
        )
        self.refresh_collections(
            initial_collection,
            select_first=not bool(restored_account_id and not initial_collection),
        )
        if not self._active_account_id:
            idx = self.account_combo.currentData()
            if isinstance(idx, int) and 0 <= idx < len(self.accounts):
                self._active_account_id = self.accounts[idx].profile_id
        self._account_workspace_ready = True
        self._session_timer = QTimer(self)
        self._session_timer.setSingleShot(True)
        self._session_timer.setInterval(300)
        self._session_timer.timeout.connect(self.save_session)
        for widget in (
            self.url_edit, self.dest_edit, self.text_images_dest_edit,
            self.text_dest_edit, self.date_after_edit,
        ):
            widget.textChanged.connect(lambda *_: self._session_timer.start())
        if getattr(sys, "frozen", False) and not test_data_dir:
            QTimer.singleShot(3000, lambda: self.check_for_updates(silent=True))
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

    def repair_ephemeral_test_paths(self) -> int:
        """Remove leaked CI/smoke-test destinations while preserving real user paths."""
        repaired = 0
        default_media = str(self.data_dir / "Library")
        default_text_media = str(self.data_dir / "Library" / "text-media")
        default_text = str(self.data_dir / "Library" / "text")

        target_changed = False
        for profile in self.target_store.all():
            if is_ephemeral_test_path(profile.destination):
                profile.destination = default_media
                target_changed = True
                repaired += 1
            if is_ephemeral_test_path(profile.hitomi_source_folder):
                profile.hitomi_source_folder = ""
                target_changed = True
                repaired += 1
        if target_changed:
            self.target_store.save()

        collection_changed = False
        for collection in self.collection_store.items:
            if is_ephemeral_test_path(collection.destination):
                collection.destination = default_media
                collection_changed = True
                repaired += 1
            if is_ephemeral_test_path(collection.text_images_destination):
                collection.text_images_destination = default_text_media
                collection_changed = True
                repaired += 1
            if is_ephemeral_test_path(collection.text_destination):
                collection.text_destination = default_text
                collection_changed = True
                repaired += 1
        if collection_changed:
            self.collection_store.save()

        session_changed = False
        for state in self.account_sessions.values():
            if isinstance(state, dict) and is_ephemeral_test_path(state.get("destination", "")):
                state["destination"] = default_media
                session_changed = True
                repaired += 1
        if session_changed:
            self.settings.setValue("account_sessions", json.dumps(self.account_sessions, ensure_ascii=False))

        try:
            last_session = json.loads(str(self.settings.value("last_session", "{}")))
        except (ValueError, TypeError):
            last_session = {}
        if isinstance(last_session, dict) and is_ephemeral_test_path(last_session.get("destination", "")):
            last_session["destination"] = default_media
            self.settings.setValue("last_session", json.dumps(last_session, ensure_ascii=False))
            session_changed = True
            repaired += 1
        if session_changed:
            self.settings.sync()
        return repaired

    def save_session(self):
        idx = self.account_combo.currentData()
        account_id = self.accounts[idx].profile_id if isinstance(idx, int) and 0 <= idx < len(self.accounts) else ""
        if account_id:
            self.account_sessions[account_id] = self.account_workspace_state()
        checked_range = self.range_group.checkedButton()
        state = dict(account_id=account_id, platform=self.platform_combo.currentData(),
                     target=self.url_edit.text(), target_type=self.selected_target(),
                     destination=self.dest_edit.text(), range_mode=checked_range.property("key"),
                     date_after=self.date_after_edit.text(),
                     options={name: widget.isChecked() for name, widget in self.session_checkboxes().items()})
        self.settings.setValue("last_session", json.dumps(state, ensure_ascii=False))
        self.settings.setValue("account_sessions", json.dumps(self.account_sessions, ensure_ascii=False))
        self.settings.sync()

    def account_workspace_state(self) -> dict:
        checked_range = self.range_group.checkedButton()
        return dict(
            platform=self.platform_combo.currentData(),
            target=self.url_edit.text(), target_type=self.selected_target(),
            destination=self.dest_edit.text(),
            range_mode=checked_range.property("key") if checked_range else "incremental",
            date_after=self.date_after_edit.text(),
        )

    def apply_account_workspace(self, state: Optional[dict]):
        state = state if isinstance(state, dict) else {}
        if state.get("platform") not in (None, self.platform_combo.currentData()):
            state = {}
        self.url_edit.setText(str(state.get("target", "")))
        target_type = str(state.get("target_type", "media"))
        if self.platform_combo.currentData() == "pixiv" and target_type == "media":
            target_type = "posts"
        for button in self.target_buttons:
            if button.property("key") == target_type:
                button.setChecked(True)
                break
        range_mode = str(state.get("range_mode", "incremental"))
        self.range_buttons.get(range_mode, self.range_buttons["incremental"]).setChecked(True)
        self.date_after_edit.setText(str(state.get("date_after", "")))
        self.sync_target_ui()
        self.dest_edit.setText(str(state.get("destination", "")))

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
            restored_target = state.get("target_type")
            if self.platform_combo.currentData() == "pixiv" and restored_target == "media":
                restored_target = "posts"
            for button in self.target_buttons:
                if button.property("key") == restored_target:
                    button.setChecked(True)
            self.range_buttons.get(state.get("range_mode"), self.range_buttons["incremental"]).setChecked(True)
            self.date_after_edit.setText(str(state.get("date_after", "")))
            self.sync_target_ui()
            self.dest_edit.setText(str(state.get("destination") or self.dest_edit.text()))
            for name, widget in self.session_checkboxes().items():
                value = state.get("options", {}).get(name)
                if isinstance(value, bool):
                    widget.setChecked(value)
            current_idx = self.account_combo.currentData()
            account_id = (self.accounts[current_idx].profile_id
                          if isinstance(current_idx, int) and 0 <= current_idx < len(self.accounts) else "")
            if account_id:
                if account_id in self.account_sessions:
                    self.apply_account_workspace(self.account_sessions[account_id])
                else:
                    # Migrate the former single global workspace to the account
                    # that was selected when the previous version last closed.
                    self.account_sessions[account_id] = self.account_workspace_state()
            self._active_account_id = account_id
        except (ValueError, TypeError, AttributeError) as exc:
            self.log.append(f"[SESSION] 前回の画面設定を復元できませんでした: {exc}")

    def build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        layout = QVBoxLayout(root); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)

        header = QFrame(); header.setObjectName("appHeader"); header.setFixedHeight(66)
        hl = QHBoxLayout(header); hl.setContentsMargins(18, 8, 18, 8)
        titles = QVBoxLayout(); t = QLabel(APP_NAME); t.setObjectName("title")
        sub = QLabel(f"保存した取得設定から、X / Twitter と pixiv をすばやく収集 — v{APP_VERSION}"); sub.setObjectName("muted")
        titles.addWidget(t); titles.addWidget(sub); hl.addLayout(titles); hl.addStretch()
        self.engine_badge = QLabel("gallery-dl: 確認中…"); self.engine_badge.setObjectName("muted")
        hl.addWidget(self.engine_badge)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.build_sidebar())
        splitter.addWidget(self.build_center())
        splitter.addWidget(self.build_queue())
        splitter.setSizes([275, 875, 290])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        footer = QFrame(); footer.setObjectName("appFooter"); footer.setFixedHeight(36)
        fl = QHBoxLayout(footer); fl.setContentsMargins(16, 4, 16, 4)
        self.footer_status = QLabel("準備完了"); self.footer_status.setObjectName("muted")
        fl.addWidget(self.footer_status); fl.addStretch()
        self.update_button = QPushButton("更新を確認")
        self.update_button.clicked.connect(lambda: self.check_for_updates(silent=False))
        fl.addWidget(self.update_button)
        self.data_label = QLabel(str(self.data_dir)); self.data_label.setObjectName("muted")
        fl.addWidget(self.data_label)
        layout.addWidget(footer)

    def build_sidebar(self):
        w = QFrame(); w.setObjectName("sidebar"); w.setMinimumWidth(250)
        l = QVBoxLayout(w); l.setContentsMargins(12, 12, 12, 12); l.setSpacing(10)

        collection_section = QFrame(); collection_section.setObjectName("sideSection")
        collection_box = QVBoxLayout(collection_section); collection_box.setContentsMargins(11, 11, 11, 11); collection_box.setSpacing(7)
        top = QHBoxLayout(); sec = QLabel("取得メニュー"); sec.setObjectName("section")
        add_collection = QPushButton("＋ 新規"); add_collection.clicked.connect(self.add_collection)
        top.addWidget(sec); top.addStretch(); top.addWidget(add_collection); collection_box.addLayout(top)
        hint = QLabel("選択すると中央に設定内容を表示します"); hint.setObjectName("muted"); hint.setWordWrap(True); collection_box.addWidget(hint)
        self.collection_list = QListWidget()
        self.collection_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.collection_list.setDefaultDropAction(Qt.MoveAction)
        self.collection_list.currentRowChanged.connect(self.collection_selected)
        self.collection_list.model().rowsMoved.connect(lambda *_: self.collection_order_changed())
        collection_box.addWidget(self.collection_list, 1)
        collection_actions = QHBoxLayout()
        self.save_collection_btn = QPushButton("変更を保存"); self.save_collection_btn.setObjectName("primary")
        self.save_collection_btn.clicked.connect(self.save_current_collection)
        delete_collection = QPushButton("削除"); delete_collection.setObjectName("ghost"); delete_collection.clicked.connect(self.delete_collection)
        collection_actions.addWidget(self.save_collection_btn, 1); collection_actions.addWidget(delete_collection)
        collection_box.addLayout(collection_actions)
        self.inbox_btn = QPushButton("確認箱  0件")
        self.inbox_btn.setObjectName("inbox")
        self.inbox_btn.clicked.connect(self.open_review_inbox)
        collection_box.addWidget(self.inbox_btn)
        l.addWidget(collection_section, 1)

        self.auth_toggle = QToolButton()
        self.auth_toggle.setObjectName("disclosure")
        self.auth_toggle.setCheckable(True)
        self.auth_toggle.setChecked(not bool(self.accounts))
        self.auth_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.auth_toggle.toggled.connect(self.toggle_auth_panel)
        l.addWidget(self.auth_toggle)

        self.auth_panel = QFrame(); self.auth_panel.setObjectName("sideSection")
        account_box = QVBoxLayout(self.auth_panel); account_box.setContentsMargins(11, 10, 11, 11); account_box.setSpacing(7)
        account_top = QHBoxLayout(); account_sec = QLabel("ログインアカウント"); account_sec.setObjectName("section")
        add = QPushButton("＋ 追加"); add.clicked.connect(self.add_account)
        account_top.addWidget(account_sec); account_top.addStretch(); account_top.addWidget(add); account_box.addLayout(account_top)
        self.account_list = QListWidget(); self.account_list.currentRowChanged.connect(self.account_selected)
        self.account_list.setMaximumHeight(140)
        account_box.addWidget(self.account_list)
        self.quick_x_login_btn = QPushButton("XのCookieを更新")
        self.quick_x_login_btn.clicked.connect(self.quick_x_login)
        self.pixiv_login_btn = QPushButton("pixivの連携を更新")
        self.pixiv_login_btn.clicked.connect(self.start_pixiv_oauth)
        self.edit_account_btn = QPushButton("設定を編集"); self.edit_account_btn.clicked.connect(self.edit_account)
        self.delete_account_btn = QPushButton("削除"); self.delete_account_btn.setObjectName("ghost"); self.delete_account_btn.clicked.connect(self.delete_account)
        account_box.addWidget(self.quick_x_login_btn); account_box.addWidget(self.pixiv_login_btn)
        account_actions = QHBoxLayout(); account_actions.addWidget(self.edit_account_btn, 1); account_actions.addWidget(self.delete_account_btn)
        account_box.addLayout(account_actions)
        l.addWidget(self.auth_panel)
        self.toggle_auth_panel(self.auth_toggle.isChecked())

        open_data = QPushButton("データフォルダを開く"); open_data.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir))))
        open_data.setObjectName("ghost")
        l.addWidget(open_data)
        return w

    def toggle_auth_panel(self, expanded: bool):
        if hasattr(self, "auth_panel"):
            self.auth_panel.setVisible(expanded)
        count = len(self.accounts)
        arrow = "▼" if expanded else "▶"
        if hasattr(self, "auth_toggle"):
            self.auth_toggle.setText(f"{arrow}  ログイン管理  ·  {count}件")

    def build_center(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget(); l = QVBoxLayout(content); l.setContentsMargins(14, 12, 14, 12); l.setSpacing(12)

        card = QFrame(); card.setObjectName("card"); cl = QVBoxLayout(card); cl.setContentsMargins(18, 18, 18, 18); cl.setSpacing(12)
        heading = QHBoxLayout()
        heading_text = QVBoxLayout(); heading_text.setSpacing(2)
        self.editor_title = QLabel("取得内容を設定"); self.editor_title.setObjectName("editorTitle")
        self.editor_subtitle = QLabel("左の取得メニューを選ぶか、新しい内容を設定してください"); self.editor_subtitle.setObjectName("muted")
        heading_text.addWidget(self.editor_title); heading_text.addWidget(self.editor_subtitle)
        heading.addLayout(heading_text, 1)
        self.editor_state = QLabel("未保存"); self.editor_state.setObjectName("statusPill")
        heading.addWidget(self.editor_state, 0, Qt.AlignTop)
        cl.addLayout(heading)

        step_account = QLabel("使用するアカウント"); step_account.setObjectName("step"); cl.addWidget(step_account)

        platform_row = QHBoxLayout(); platform_row.addWidget(QLabel("取得元"))
        self.platform_combo = QComboBox(); self.platform_combo.addItem("X / Twitter", "x"); self.platform_combo.addItem("pixiv", "pixiv")
        self.platform_combo.currentIndexChanged.connect(self.platform_changed)
        platform_row.addWidget(self.platform_combo, 1); cl.addLayout(platform_row)

        account_row = QHBoxLayout(); account_row.addWidget(QLabel("選択"))
        self.account_combo = QComboBox(); self.account_combo.currentIndexChanged.connect(self.account_combo_changed)
        account_row.addWidget(self.account_combo, 1); cl.addLayout(account_row)

        step_target = QLabel("1. 何を取得する？"); step_target.setObjectName("step"); cl.addWidget(step_target)
        scope_row = QHBoxLayout(); scope_row.addWidget(QLabel("取得する人"))
        self.scope_combo = QComboBox(); self.scope_combo.addItem("ログイン中の自分", "self"); self.scope_combo.addItem("別のユーザー", "other")
        self.scope_combo.currentIndexChanged.connect(self.sync_target_ui)
        scope_row.addWidget(self.scope_combo, 1); cl.addLayout(scope_row)
        cl.addWidget(QLabel("対象のURL・ユーザー名・pixivユーザーID"))
        self.url_edit = QLineEdit(); self.url_edit.setPlaceholderText("例: https://x.com/username / @username / pixiv user ID")
        self.url_edit.editingFinished.connect(self.sync_target_ui)
        cl.addWidget(self.url_edit)

        target_row = QHBoxLayout(); target_row.addWidget(QLabel("取得対象"))
        self.target_group = QButtonGroup(self)
        self.target_buttons: list[QRadioButton] = []
        for text, key in [("投稿全体", "posts"), ("メディア欄", "media"), ("いいね", "likes"), ("ブックマーク", "bookmarks")]:
            b = QRadioButton(text); b.setProperty("key", key); self.target_group.addButton(b); self.target_buttons.append(b); target_row.addWidget(b)
        self.target_buttons[1].setChecked(True); target_row.addStretch(); cl.addLayout(target_row)
        self.target_group.buttonClicked.connect(lambda _b: self.sync_target_ui())

        step_range = QLabel("2. どこまで取得する？"); step_range.setObjectName("step"); cl.addWidget(step_range)
        range_row = QHBoxLayout()
        self.range_group = QButtonGroup(self)
        self.range_buttons = {}
        for text, key in [("前回 / 引継ぎ基準以降", "incremental"), ("すべて", "all"), ("日付以降", "date")]:
            b = QRadioButton(text); b.setProperty("key", key); self.range_group.addButton(b); self.range_buttons[key] = b; range_row.addWidget(b)
            if key == "incremental": b.setChecked(True)
        self.date_after_edit = QLineEdit(); self.date_after_edit.setPlaceholderText("例: 2026-08-01")
        self.date_after_edit.setMaximumWidth(170); self.date_after_edit.setVisible(False)
        range_row.addWidget(self.date_after_edit); range_row.addStretch(); cl.addLayout(range_row)
        self.range_note = QLabel(
            "Hitomi引継ぎ済みなら、最新の既存Tweetを初回基準にして10分前から再確認します。"
            "保存済みは自動で省くので、基本は続きだけ取得します。"
        )
        self.range_note.setObjectName("muted"); self.range_note.setWordWrap(True); cl.addWidget(self.range_note)
        self.range_status = QLabel("取得基準を確認中…")
        self.range_status.setObjectName("muted"); self.range_status.setWordWrap(True); cl.addWidget(self.range_status)
        self.range_group.buttonClicked.connect(lambda _b: self.sync_target_ui())
        self.date_after_edit.textChanged.connect(lambda _t: self.sync_target_ui())

        self.likes_controls = QWidget()
        likes_box = QVBoxLayout(self.likes_controls); likes_box.setContentsMargins(0, 0, 0, 0); likes_box.setSpacing(8)
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
        self.likes_probe_spin.setSuffix(" 投稿")
        likes_row.addWidget(self.likes_probe_spin)
        likes_row.addStretch(); likes_box.addLayout(likes_row)
        self.likes_note = QLabel(
            "Likesは先頭だけを軽く確認し、保存済みアンカーに到達したら終了します。アンカーより手前の新規投稿だけをDLします。"
            "アンカーが探索上限内で見つからない場合は、安全のため何もDLしません。"
        )
        self.likes_note.setObjectName("muted"); self.likes_note.setWordWrap(True); likes_box.addWidget(self.likes_note)
        self.likes_controls.setVisible(False)
        cl.addWidget(self.likes_controls)

        step_media = QLabel("3. 保存する種類と場所"); step_media.setObjectName("step"); cl.addWidget(step_media)
        media_row = QHBoxLayout(); media_row.addWidget(QLabel("メディア"))
        self.chk_img = QCheckBox("画像"); self.chk_img.setChecked(True)
        self.chk_video = QCheckBox("動画"); self.chk_video.setChecked(True)
        self.chk_gif = QCheckBox("GIF"); self.chk_gif.setChecked(True)
        media_row.addWidget(self.chk_img); media_row.addWidget(self.chk_video); media_row.addWidget(self.chk_gif); media_row.addStretch(); cl.addLayout(media_row)

        content_row = QHBoxLayout(); content_row.addWidget(QLabel("保存内容"))
        self.content_mode_combo = QComboBox()
        self.content_mode_combo.addItem("画像・動画のみ", "images")
        self.content_mode_combo.addItem("本文＋画像・動画", "text_images")
        self.content_mode_combo.addItem("本文のみ", "text")
        content_row.addWidget(self.content_mode_combo, 1)
        self.review_checkbox = QCheckBox("取得後に確認箱で振り分ける")
        content_row.addWidget(self.review_checkbox); cl.addLayout(content_row)

        self.images_dest_row = QWidget(); dest_row = QHBoxLayout(self.images_dest_row)
        dest_row.setContentsMargins(0, 0, 0, 0)
        self.images_dest_label = QLabel("保存先"); dest_row.addWidget(self.images_dest_label)
        self.dest_edit = QLineEdit(str(self.data_dir / "Library")); dest_row.addWidget(self.dest_edit, 1)
        browse = QPushButton("変更"); browse.clicked.connect(self.pick_destination); dest_row.addWidget(browse)
        cl.addWidget(self.images_dest_row)

        self.text_images_dest_row = QWidget(); text_images_dest_layout = QHBoxLayout(self.text_images_dest_row)
        text_images_dest_layout.setContentsMargins(0, 0, 0, 0)
        text_images_dest_layout.addWidget(QLabel("本文＋画像・動画の保存先"))
        self.text_images_dest_edit = QLineEdit(str(self.data_dir / "Library" / "text-media"))
        text_images_dest_layout.addWidget(self.text_images_dest_edit, 1)
        text_images_browse = QPushButton("変更")
        text_images_browse.clicked.connect(self.pick_text_images_destination)
        text_images_dest_layout.addWidget(text_images_browse)
        self.text_images_dest_row.setVisible(False); cl.addWidget(self.text_images_dest_row)

        self.text_dest_row = QWidget(); text_dest_layout = QHBoxLayout(self.text_dest_row)
        text_dest_layout.setContentsMargins(0, 0, 0, 0); text_dest_layout.addWidget(QLabel("本文のみの保存先"))
        self.text_dest_edit = QLineEdit(str(self.data_dir / "Library" / "text")); text_dest_layout.addWidget(self.text_dest_edit, 1)
        text_browse = QPushButton("変更"); text_browse.clicked.connect(self.pick_text_destination); text_dest_layout.addWidget(text_browse)
        self.text_dest_row.setVisible(False); cl.addWidget(self.text_dest_row)

        self.chk_remember_dest = QCheckBox("このアカウント・取得対象の保存先として記憶"); self.chk_remember_dest.setChecked(True)
        cl.addWidget(self.chk_remember_dest)

        target_row2 = QHBoxLayout()
        self.target_status = QLabel("この取得対象の保存先はまだ未登録です。"); self.target_status.setObjectName("muted"); self.target_status.setWordWrap(True)
        target_row2.addWidget(self.target_status, 1)
        self.import_btn = QPushButton("既存のHitomi保存フォルダを引き継ぐ")
        self.import_btn.clicked.connect(self.index_existing_hitomi_folder); target_row2.addWidget(self.import_btn); cl.addLayout(target_row2)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("▶ 詳細設定")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        cl.addWidget(self.advanced_toggle)
        self.advanced_panel = QWidget()
        advanced = QVBoxLayout(self.advanced_panel); advanced.setContentsMargins(12, 4, 0, 4); advanced.setSpacing(8)
        self.chk_archive = QCheckBox("保存済みファイルを重複取得しない（推奨）"); self.chk_archive.setChecked(True)
        self.chk_internal_meta = QCheckBox("投稿者IDなどをアプリ内に記録（画像フォルダにJSONは作りません）"); self.chk_internal_meta.setChecked(True)
        self.chk_hitomi_name = QCheckBox("Xのファイル名をHitomi Downloader形式にする"); self.chk_hitomi_name.setChecked(True)
        self.chk_direct_folder = QCheckBox("指定した保存先フォルダへ直接保存する"); self.chk_direct_folder.setChecked(True)
        advanced.addWidget(self.chk_archive); advanced.addWidget(self.chk_internal_meta)
        advanced.addWidget(self.chk_hitomi_name); advanced.addWidget(self.chk_direct_folder)
        self.advanced_panel.setVisible(False)
        self.advanced_toggle.toggled.connect(self.toggle_advanced_settings)
        cl.addWidget(self.advanced_panel)

        buttons = QHBoxLayout(); self.preview_btn = QPushButton("実行内容を確認"); self.preview_btn.clicked.connect(self.preview_command)
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
        w = QFrame(); w.setObjectName("queueSidebar"); w.setMinimumWidth(270)
        l = QVBoxLayout(w); l.setContentsMargins(12, 12, 12, 12)
        row = QHBoxLayout(); sec = QLabel("処理状況"); sec.setObjectName("section"); row.addWidget(sec); row.addStretch()
        stop = QPushButton("停止"); stop.setObjectName("danger"); stop.clicked.connect(self.stop_active); row.addWidget(stop); l.addLayout(row)
        self.queue_empty_label = QLabel("待機中の処理はありません\n\n取得を開始すると、進行状況がここに表示されます。")
        self.queue_empty_label.setObjectName("muted"); self.queue_empty_label.setWordWrap(True); self.queue_empty_label.setAlignment(Qt.AlignCenter)
        l.addWidget(self.queue_empty_label, 1)
        self.queue_layout = QVBoxLayout(); self.queue_layout.setAlignment(Qt.AlignTop)
        holder = QWidget(); holder.setLayout(self.queue_layout)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); scroll.setWidget(holder)
        self.queue_scroll = scroll; self.queue_scroll.setVisible(False)
        l.addWidget(scroll, 1)
        return w

    def toggle_advanced_settings(self, expanded: bool):
        self.advanced_panel.setVisible(bool(expanded))
        self.advanced_toggle.setText("▼ 詳細設定" if expanded else "▶ 詳細設定")

    def refresh_account_actions(self):
        idx = self.account_combo.currentData() if hasattr(self, "account_combo") else -1
        registered = isinstance(idx, int) and 0 <= idx < len(self.accounts)
        platform = self.accounts[idx].platform if registered else ""
        self.quick_x_login_btn.setVisible(registered and platform == "x")
        self.pixiv_login_btn.setVisible(registered and platform == "pixiv")
        self.edit_account_btn.setEnabled(registered)
        self.delete_account_btn.setEnabled(registered)

    def reveal_last_saved_file(self):
        if self.last_saved_path and self.last_saved_path.exists():
            reveal_path_in_file_manager(self.last_saved_path)
        else:
            QMessageBox.information(self, "実保存", "この起動中に実在確認できた新規ファイルはまだありません。")

    def open_current_destination(self):
        path = self.current_content_destination()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, "保存先", "保存先フォルダはまだ存在しません。")

    def seed_recent_from_current_destination(self):
        root = self.current_content_destination()
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

    def current_content_destination(self) -> Path:
        """Return the destination represented by the current content mode."""
        mode = str(self.content_mode_combo.currentData() or "images")
        if mode == "text_images" and not self.text_images_dest_row.isHidden():
            value = self.text_images_dest_edit.text().strip()
        elif mode == "text" and not self.text_dest_row.isHidden():
            value = self.text_dest_edit.text().strip()
        else:
            value = self.dest_edit.text().strip()
        return Path(value or self.data_dir / "Library").expanduser()

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
        self._recent_refresh_timer.stop()
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

    def schedule_recent_downloads_refresh(self):
        """Coalesce bursts of file-save events into one thumbnail rebuild."""
        self._recent_refresh_timer.start()

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
                self.last_saved_label.setText(f"今回の実保存: {path.name}")
                self.last_saved_label.setToolTip(str(path))
            self.footer_status.setText(f"実保存 {job.files_saved_count:,}件: {path.name}")
            self.footer_status.setToolTip(str(path))
            self.log.append(f"[SAVED OK] {path} / {size:,} bytes")
            self.schedule_recent_downloads_refresh()
        except Exception as exc:
            self.log.append(f"[RECENT ERROR] {path}: {exc}")

    def connect_job(self, job: DownloadJob):
        job.finished.connect(self.job_finished)
        job.file_saved.connect(self.job_file_saved)
        if hasattr(self, "queue_empty_label"):
            self.queue_empty_label.setVisible(False)
        if hasattr(self, "queue_scroll"):
            self.queue_scroll.setVisible(True)
        return job

    def load_accounts(self) -> list[AccountProfile]:
        try:
            data = json.loads(self.accounts_file.read_text(encoding="utf-8"))
            profiles = [AccountProfile(**x) for x in data]
            changed = False
            for account in profiles:
                if account.auth_mode == "managed_x":
                    expected = str(managed_x_cookie_path(self.data_dir, account.profile_id))
                    if account.auth_value != expected:
                        account.auth_value = expected
                        changed = True
                    detected_id = str(inspect_netscape_cookie_file(Path(expected)).get("x_user_id") or "")
                    if detected_id and account.user_id != detected_id:
                        account.user_id = detected_id
                        changed = True
                elif account.platform == "x" and account.auth_mode == "cookies_file" and account.auth_value:
                    detected_id = str(inspect_netscape_cookie_file(Path(account.auth_value)).get("x_user_id") or "")
                    if detected_id and account.user_id != detected_id:
                        account.user_id = detected_id
                        changed = True
                elif account.auth_mode == "managed_pixiv":
                    expected = str(managed_pixiv_cache_path(self.data_dir, account.profile_id))
                    if account.auth_value != expected:
                        account.auth_value = expected
                        changed = True
            if changed or any(not x.get("profile_id") for x in data):
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
                auth_mark = ""
                auth_hint = ""
                if a.platform == "x" and a.auth_mode == "managed_x":
                    ready = bool(inspect_netscape_cookie_file(Path(a.auth_value)).get("x_auth"))
                    auth_mark = "✓" if ready else "⚠"
                    auth_hint = "アカウント専用Cookie保存済み" if ready else "Xログインが必要"
                elif a.platform == "pixiv" and a.auth_mode == "managed_pixiv":
                    ready = bool(inspect_pixiv_cache(Path(a.auth_value)).get("authenticated"))
                    auth_mark = "✓" if ready else "⚠"
                    auth_hint = "アカウント専用pixiv連携保存済み" if ready else "pixivログインが必要"
                label = "  ".join(x for x in (prefix, auth_mark, a.name) if x)
                item = QListWidgetItem(label)
                if auth_hint:
                    item.setToolTip(auth_hint)
                self.account_list.addItem(item)
                self.account_combo.addItem(label, i)
                if auth_hint:
                    self.account_combo.setItemData(i, auth_hint, Qt.ToolTipRole)
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
        self.toggle_auth_panel(self.auth_toggle.isChecked())
        self.account_combo_changed()

    def refresh_collections(self, selected_id: str = "", *, select_first: bool = True):
        if not hasattr(self, "collection_list"):
            return
        selected_id = selected_id or self._active_collection_id
        self.collection_list.blockSignals(True)
        self.collection_list.clear()
        selected_row = -1
        for row, collection in enumerate(self.collection_store.items):
            source = {"posts": "投稿", "media": "メディア", "likes": "いいね", "bookmarks": "ブックマーク"}.get(collection.source, collection.source)
            item = QListWidgetItem(f"{collection.name}\n  {collection.platform.upper()} · {source}")
            item.setSizeHint(QSize(0, 52))
            item.setData(Qt.UserRole, collection.collection_id)
            item.setToolTip("ドラッグして並び替えできます")
            self.collection_list.addItem(item)
            if collection.collection_id == selected_id:
                selected_row = row
        if selected_row < 0 and self.collection_store.items and select_first:
            selected_row = 0
        self.collection_list.setCurrentRow(selected_row)
        self.collection_list.blockSignals(False)
        if selected_row >= 0:
            self.collection_selected(selected_row)
        else:
            self.refresh_editor_heading()
        self.refresh_inbox_count()

    def refresh_editor_heading(self):
        if not hasattr(self, "editor_title"):
            return
        collection = self.current_collection()
        if not collection:
            self.editor_title.setText("取得内容を設定")
            self.editor_subtitle.setText("左の取得メニューを選ぶか、新しい内容を設定してください")
            self.editor_state.setText("新規")
            return
        if collection.source == "likes":
            source = "ブックマーク" if collection.platform == "pixiv" else "いいね"
        else:
            source = {"posts": "投稿", "media": "メディア", "bookmarks": "ブックマーク"}.get(
                collection.source, collection.source
            )
        account = next((a for a in self.accounts if a.profile_id == collection.account_id), None)
        account_name = account.name if account else "認証アカウント未設定"
        mode = "確認箱へ" if collection.review_mode == "inbox" else "自動保存"
        service = "X" if collection.platform == "x" else "pixiv"
        self.editor_title.setText(collection.name)
        self.editor_subtitle.setText(
            f"{service} · {source}  /  {account_name}  /  {mode}"
        )
        self.editor_state.setText("選択中")

    def collection_order_changed(self):
        ids = [str(self.collection_list.item(i).data(Qt.UserRole)) for i in range(self.collection_list.count())]
        self.collection_store.reorder(ids)

    def current_collection(self) -> Optional[CollectionProfile]:
        item = self.collection_list.currentItem() if hasattr(self, "collection_list") else None
        return self.collection_store.get(str(item.data(Qt.UserRole))) if item else None

    def collection_selected(self, row: int):
        if row < 0:
            self.refresh_editor_heading()
            return
        collection = self.current_collection()
        if not collection:
            return
        self._active_collection_id = collection.collection_id
        account_idx = next((i for i, a in enumerate(self.accounts) if a.profile_id == collection.account_id), -1)
        combo_idx = self.account_combo.findData(account_idx)
        if combo_idx >= 0:
            self.account_combo.setCurrentIndex(combo_idx)
        pidx = self.platform_combo.findData(collection.platform)
        if pidx >= 0:
            self.platform_combo.setCurrentIndex(pidx)
        self.scope_combo.setCurrentIndex(max(0, self.scope_combo.findData(collection.target_scope)))
        self.url_edit.setText(collection.target_value)
        for button in self.target_buttons:
            if button.property("key") == collection.source:
                button.setChecked(True); break
        self.content_mode_combo.setCurrentIndex(max(0, self.content_mode_combo.findData(collection.content_mode)))
        self.review_checkbox.setChecked(collection.review_mode == "inbox")
        range_button = self.range_buttons.get(collection.range_mode, self.range_buttons["incremental"])
        range_button.setChecked(True)
        self.date_after_edit.setText(collection.date_after)
        self.sync_target_ui()
        # A collection's explicit destinations take precedence over legacy
        # target_store defaults restored by sync_target_ui().
        self.dest_edit.setText(collection.destination or str(self.data_dir / "Library"))
        self.text_images_dest_edit.setText(
            collection.text_images_destination or collection.destination or str(self.data_dir / "Library")
        )
        self.text_dest_edit.setText(collection.text_destination or str(Path(self.dest_edit.text()) / "text"))
        if collection.destination:
            self.target_status.setText("✓ この取得設定に保存先を登録済みです。")
        self.refresh_editor_heading()

    def collection_from_current(self, *, name: str, collection_id: str = "") -> CollectionProfile:
        idx = self.account_combo.currentData()
        if not isinstance(idx, int) or not (0 <= idx < len(self.accounts)):
            raise ValueError("先に認証アカウントを選択してください。")
        account = self.accounts[idx]
        scope = str(self.scope_combo.currentData() or "self")
        item = CollectionProfile(
            name=name.strip(), account_id=account.profile_id, platform=str(self.platform_combo.currentData()),
            source=self.selected_target(), target_scope=scope,
            target_value=self.url_edit.text().strip() if scope == "other" else "",
            content_mode=str(self.content_mode_combo.currentData() or "images"),
            review_mode="inbox" if self.review_checkbox.isChecked() else "auto",
            destination=self.dest_edit.text().strip(),
            text_images_destination=(
                self.text_images_dest_edit.text().strip() or self.dest_edit.text().strip()
            ),
            text_destination=self.text_dest_edit.text().strip() or str(Path(self.dest_edit.text().strip()) / "text"),
            range_mode=str(self.range_group.checkedButton().property("key")),
            date_after=self.date_after_edit.text().strip(),
            collection_id=collection_id or uuid.uuid4().hex,
        )
        item.validate()
        return item

    def add_collection(self):
        name, ok = QInputDialog.getText(self, "取得設定を追加", "分かりやすい名前（例: 資料用ブックマーク）")
        if not ok or not name.strip():
            return
        try:
            item = self.collection_store.upsert(self.collection_from_current(name=name))
            self.refresh_collections(item.collection_id)
        except Exception as exc:
            QMessageBox.warning(self, "取得設定", str(exc))

    def save_current_collection(self):
        current = self.current_collection()
        if not current:
            self.add_collection(); return
        try:
            item = self.collection_store.upsert(self.collection_from_current(
                name=current.name, collection_id=current.collection_id,
            ))
            self.refresh_collections(item.collection_id)
            self.footer_status.setText(f"取得設定を保存: {item.name}")
        except Exception as exc:
            QMessageBox.warning(self, "取得設定", str(exc))

    def delete_collection(self):
        current = self.current_collection()
        if not current:
            return
        if QMessageBox.question(self, "取得設定を削除", f"「{current.name}」を削除しますか？") == QMessageBox.Yes:
            self.collection_store.delete(current.collection_id)
            self._active_collection_id = ""
            self.refresh_collections()

    def add_account(self):
        d = AccountDialog(self, data_dir=self.data_dir, engine=self.engine_command())
        if d.exec():
            self.accounts.append(d.result_profile()); self.save_accounts(); self.refresh_accounts(self.accounts[-1].profile_id)

    def quick_x_login(self):
        idx = self.account_list.currentRow()
        if idx < 0 or idx >= len(self.accounts):
            QMessageBox.information(self, "Xログイン", "先にXアカウントを選択してください。")
            return
        account = self.accounts[idx]
        if account.platform != "x":
            QMessageBox.information(self, "Xログイン", "選択中のアカウントはXではありません。")
            return
        if account.auth_mode != "managed_x":
            QMessageBox.information(
                self,
                "Xログイン",
                "このアカウントは従来方式です。「選択アカウントを編集」で、"
                "認証方式を「アカウント別Cookie」へ変更してください。",
            )
            return
        dialog = AccountDialog(self, account, data_dir=self.data_dir, engine=self.engine_command())
        if dialog.exec():
            self.accounts[idx] = dialog.result_profile()
            self.save_accounts()
            self.refresh_accounts(account.profile_id)

    def edit_account(self):
        idx = self.account_list.currentRow()
        if idx < 0 or idx >= len(self.accounts): return
        d = AccountDialog(
            self, self.accounts[idx], data_dir=self.data_dir, engine=self.engine_command()
        )
        if d.exec():
            self.accounts[idx] = d.result_profile(); self.save_accounts(); self.refresh_accounts(self.accounts[idx].profile_id)

    def delete_account(self):
        idx = self.account_list.currentRow()
        if idx < 0 or idx >= len(self.accounts): return
        used_by = [x.name for x in self.collection_store.items if x.account_id == self.accounts[idx].profile_id]
        if used_by:
            QMessageBox.warning(
                self, "削除できません",
                "この認証アカウントは次の取得設定で使用中です。先に取得設定を削除または変更してください。\n\n"
                + "\n".join(used_by),
            )
            return
        if QMessageBox.question(self, "削除", f"「{self.accounts[idx].name}」を削除しますか？") == QMessageBox.Yes:
            self.accounts.pop(idx); self.save_accounts(); self.refresh_accounts()

    def account_selected(self, idx):
        if 0 <= idx < len(self.accounts):
            cidx = self.account_combo.findData(idx)
            if cidx >= 0: self.account_combo.setCurrentIndex(cidx)

    def account_combo_changed(self):
        idx = self.account_combo.currentData()
        registered = isinstance(idx, int) and 0 <= idx < len(self.accounts)
        account_id = self.accounts[idx].profile_id if registered else ""
        previous_id = self._active_account_id
        if self._account_workspace_ready and previous_id and previous_id != account_id:
            self.account_sessions[previous_id] = self.account_workspace_state()
        self.account_list.blockSignals(True)
        self.account_list.setCurrentRow(idx if isinstance(idx, int) else -1)
        self.account_list.blockSignals(False)
        if registered:
            a = self.accounts[idx]
            pidx = self.platform_combo.findData(a.platform)
            if pidx >= 0 and pidx != self.platform_combo.currentIndex():
                self.platform_combo.setCurrentIndex(pidx)
        self.platform_combo.setEnabled(not registered)
        self.platform_combo.setToolTip(
            "サービスは選択中のアカウントに固定されます。変更する場合は「認証なし」を選んでください。"
            if registered else ""
        )
        if self._account_workspace_ready and previous_id != account_id:
            self.apply_account_workspace(self.account_sessions.get(account_id))
        self._active_account_id = account_id
        self.refresh_account_actions()

    def platform_changed(self):
        p = self.platform_combo.currentData()
        labels = ([("投稿全体", "posts"), ("メディア欄", "media"), ("いいね", "likes"), ("ブックマーク", "bookmarks")]
                  if p == "x" else [("作品", "posts"), ("イラスト", "media"), ("ブックマーク", "likes"), ("", "bookmarks")])
        for b, (text, key) in zip(self.target_buttons, labels):
            b.setText(text); b.setProperty("key", key)
        self.target_buttons[1].setVisible(p == "x")
        self.target_buttons[3].setVisible(p == "x")
        if p == "pixiv" and self.selected_target() == "media":
            self.target_buttons[0].setChecked(True)
        self.url_edit.setPlaceholderText("例: https://x.com/username / @username" if p == "x" else "例: https://www.pixiv.net/users/123456 / 123456")
        if hasattr(self, "chk_hitomi_name"):
            self.chk_hitomi_name.setEnabled(p == "x")
            self.chk_hitomi_name.setVisible(p == "x")
        if hasattr(self, "content_mode_combo"):
            self.content_mode_combo.setEnabled(p == "x")
            if p == "pixiv": self.content_mode_combo.setCurrentIndex(0)
        self.sync_target_ui()

    def pick_destination(self):
        d = QFileDialog.getExistingDirectory(self, "保存先を選択", self.dest_edit.text())
        if d: self.dest_edit.setText(d)

    def pick_text_images_destination(self):
        d = QFileDialog.getExistingDirectory(
            self, "本文＋画像・動画の保存先を選択", self.text_images_dest_edit.text()
        )
        if d: self.text_images_dest_edit.setText(d)

    def pick_text_destination(self):
        d = QFileDialog.getExistingDirectory(self, "本文の保存先を選択", self.text_dest_edit.text())
        if d: self.text_dest_edit.setText(d)

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
        target = self.selected_target()
        if platform == "x" and target == "bookmarks":
            return normalize_target(platform, "self", target)
        if (
            platform == "pixiv" and target == "likes"
            and self.scope_combo.currentData() == "self"
        ):
            return (
                "https://www.pixiv.net/bookmark.php",
                "pixiv:self:bookmarks",
                "自分のブックマーク",
            )
        raw = self.url_edit.text()
        if self.scope_combo.currentData() == "self":
            idx = self.account_combo.currentData()
            if isinstance(idx, int) and 0 <= idx < len(self.accounts):
                account = self.accounts[idx]
                if account.user_id:
                    raw = f"id:{account.user_id}"
                elif account.username:
                    raw = account.username
            if not raw.strip():
                raise ValueError("ログインから自分のIDをまだ確認できません。Xログインを一度更新してください。")
        return normalize_target(platform, raw, target)

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
        is_pixiv_bookmarks = self.platform_combo.currentData() == "pixiv" and self.selected_target() == "likes"
        is_x_bookmarks = self.platform_combo.currentData() == "x" and self.selected_target() == "bookmarks"
        is_x_reviewable = is_x_bookmarks or is_x_likes
        if hasattr(self, "scope_combo"):
            self.scope_combo.setEnabled(not is_x_bookmarks)
            self.url_edit.setEnabled(self.scope_combo.currentData() == "other" and not is_x_bookmarks)
            self.url_edit.setVisible(self.scope_combo.currentData() == "other" and not is_x_bookmarks)
        if hasattr(self, "content_mode_combo"):
            self.content_mode_combo.setEnabled(is_x_reviewable)
            self.review_checkbox.setEnabled(is_x_reviewable)
            if not is_x_reviewable:
                self.content_mode_combo.setCurrentIndex(max(0, self.content_mode_combo.findData("images")))
                self.review_checkbox.setChecked(False)
            self.images_dest_label.setText("画像・動画のみの保存先" if is_x_reviewable else "保存先")
            self.text_images_dest_row.setVisible(is_x_reviewable)
            self.text_dest_row.setVisible(is_x_reviewable)
        range_button = self.range_group.checkedButton() if hasattr(self, "range_group") else None
        range_mode = str(range_button.property("key")) if range_button else "incremental"
        if is_pixiv_bookmarks and range_mode == "date":
            self.range_buttons["incremental"].setChecked(True)
            range_mode = "incremental"
        if hasattr(self, "likes_baseline_btn"):
            self.likes_controls.setVisible(is_x_likes)
            self.likes_baseline_btn.setEnabled(is_x_likes and not self.has_pending_likes_job())
            self.likes_anchor_spin.setEnabled(is_x_likes)
            self.likes_probe_spin.setEnabled(is_x_likes)
            self.likes_note.setEnabled(is_x_likes)
            if is_x_likes:
                incremental_label = "新しいいいねだけ"
            elif is_pixiv_bookmarks:
                incremental_label = "新しいブックマークだけ"
            else:
                incremental_label = "前回の続きから"
            self.range_buttons["incremental"].setText(incremental_label)
            self.range_buttons["all"].setText("すべて確認")
            self.range_buttons["date"].setText("投稿日を指定")
            self.range_buttons["date"].setEnabled(not is_pixiv_bookmarks)
            self.range_buttons["date"].setToolTip(
                "ブックマークした日付は取得できないため、この対象では使用できません。"
                if is_pixiv_bookmarks else "作品・投稿の公開日を基準に絞り込みます。"
            )
            self.date_after_edit.setVisible(range_mode == "date")
        if hasattr(self, "import_btn"):
            if self.platform_combo.currentData() == "x":
                self.import_btn.setText("既存のHitomi保存フォルダを引き継ぐ")
            else:
                self.import_btn.setText("既存の保存フォルダをこの対象に登録")
        if hasattr(self, "start_btn"):
            if is_x_likes:
                label = (
                    "⬇ 新しいいいねを確認"
                    if range_mode == "incremental"
                    else "⬇ いいねを確認"
                )
            else:
                label = {
                    ("x", "bookmarks"): "⬇ ブックマークを確認",
                    ("pixiv", "likes"): "⬇ ブックマークを取得",
                }.get((self.platform_combo.currentData(), self.selected_target()), "⬇ ダウンロード開始")
            self.start_btn.setText(label)
        if hasattr(self, "range_note"):
            if is_pixiv_bookmarks:
                self.range_note.setText(
                    "ブックマーク一覧を新しい順に確認します。作品の投稿日に関係なく、"
                    "最近ブックマークした作品を取得し、保存済みは自動で省きます。"
                )
            elif self.platform_combo.currentData() == "pixiv":
                self.range_note.setText(
                    "pixiv作品の投稿日を前回成功時刻から再確認し、保存済みは自動で省きます。"
                )
            else:
                self.range_note.setText(
                    "Hitomi引継ぎ済みなら、最新の既存Tweetを初回基準にして10分前から再確認します。"
                    "保存済みは自動で省くので、基本は続きだけ取得します。"
                )
        if profile and profile.destination:
            self.dest_edit.setText(profile.destination)
            self.text_images_dest_edit.setText(profile.destination)
            self.text_dest_edit.setText(str(Path(profile.destination) / "text"))
            self.chk_direct_folder.setChecked(profile.direct_folder)
            self.target_status.setText(self.target_status_text(profile))
        else:
            self.dest_edit.setText(str(self.data_dir / "Library"))
            self.text_images_dest_edit.setText(str(self.data_dir / "Library" / "text-media"))
            self.text_dest_edit.setText(str(self.data_dir / "Library" / "text"))
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
            parts.append(f"重複防止データ: {profile.imported_archive_entries:,}件")
        if profile.likes_anchor_at:
            parts.append(
                f"Likesアンカー: {profile.likes_anchor_posts:,}投稿 / 確認済み {profile.likes_seen_posts:,}投稿"
            )
        return "  |  ".join(parts) if parts else "登録済み（まだ取得履歴なし）"

    def range_status_text(self, profile: Optional[TargetProfile]) -> str:
        button = self.range_group.checkedButton() if hasattr(self, "range_group") else None
        mode = str(button.property("key")) if button else "incremental"
        if mode == "all":
            return "✓ 過去まで確認します。保存済みのメディアは自動でスキップします。"
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

        if self.platform_combo.currentData() == "pixiv" and self.selected_target() == "likes":
            return (
                "✓ ブックマーク一覧を新しい順に確認します。作品の投稿日では絞り込まず、"
                "保存履歴と既存ファイルで保存済みを省くため、古い作品を最近追加した場合も対象です。"
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
            if ctx.get("job_kind") not in {"likes_anchor", "likes_probe", "likes_download", "collection_download"}:
                continue
            if ctx.get("job_kind") == "collection_download" and ctx.get("target_type") != "likes":
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

    def ensure_auth_ready(self, auth_mode: str, auth_value: str):
        if auth_mode == "managed_x":
            info = inspect_netscape_cookie_file(Path(auth_value))
            if not info.get("x_auth"):
                raise ValueError(
                    "このXアカウントは未ログインです。左側の『Xログイン / Cookie更新』から"
                    "ログインしてください。"
                )
        elif auth_mode == "managed_pixiv":
            if not inspect_pixiv_cache(Path(auth_value)).get("authenticated"):
                raise ValueError(
                    "このpixivアカウントは未連携です。左側の『pixivログイン / 連携更新』から"
                    "ログインしてください。"
                )

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

    def is_x_likes_review_flow(self) -> bool:
        return bool(
            self.platform_combo.currentData() == "x"
            and self.selected_target() == "likes"
            and (
                self.review_checkbox.isChecked()
                or self.content_mode_combo.currentData() != "images"
            )
        )

    def build_likes_probe(self, *, scan_all: bool = False) -> tuple[list[str], str, dict]:
        url, target_key, target_name = self.current_target()
        account_name, auth_mode, auth_value = self.current_auth()
        self.ensure_auth_ready(auth_mode, auth_value)
        profile = self.target_store.ensure(target_key, "x", target_name)
        if not scan_all and not profile.likes_anchor_at:
            raise ValueError("Likesアンカーがありません。先に『Likesアンカーを作成（DLなし）』を実行してください。")
        if not scan_all and profile.likes_anchor_login and profile.likes_anchor_login != account_name:
            raise ValueError(
                f"このLikesアンカーは認証プロファイル『{profile.likes_anchor_login}』で作成されています。"
                f"現在は『{account_name}』です。同じ認証プロファイルを選ぶか、アンカーを作り直してください。"
            )
        anchor_ids = ([] if scan_all else self.catalog.likes_anchor_ids(
            target_key=target_key, login_profile=account_name
        ))
        if not scan_all and not anchor_ids:
            raise ValueError("LikesアンカーDBが空です。『Likesアンカーを作成（DLなし）』をやり直してください。")
        collection = self.current_collection()
        collection_id = ""
        if collection and collection.platform == "x" and collection.source == "likes":
            collection_id = collection.collection_id
        content_mode = str(self.content_mode_combo.currentData() or "images")
        review_mode = "inbox" if self.review_checkbox.isChecked() else "auto"
        range_button = self.range_group.checkedButton()
        range_mode = str(range_button.property("key")) if range_button else "incremental"
        date_after = self.date_after_edit.text().strip() if scan_all and range_mode == "date" else ""
        if date_after and not parse_iso(date_after):
            raise ValueError("投稿日は YYYY-MM-DD 形式で入力してください。")
        if (content_mode != "images" or review_mode == "inbox") and not collection_id:
            raise ValueError("本文保存または確認箱を使う場合は、先にこの内容を取得設定として保存してください。")
        cmd = build_x_likes_probe_command(
            self.engine_command(), url=url, auth_mode=auth_mode, auth_value=auth_value,
            max_media=self.likes_probe_spin.value(),
            include_posts=(content_mode != "images" or review_mode == "inbox"),
        )
        title = (
            f"X / {account_name} / {target_name} / "
            f"{'Likes一覧確認' if scan_all else 'Likes差分確認'}（DLなし）"
        )
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
            "scan_all": scan_all,
            "anchor_posts": max(1, int(profile.likes_anchor_posts or self.likes_anchor_spin.value())),
            "archive_scope": f"{target_key}:likes",
            "destination": self.dest_edit.text().strip(),
            "extensions": self.selected_extensions(),
            "capture_internal_metadata": self.chk_internal_meta.isChecked(),
            "use_archive": self.chk_archive.isChecked(),
            "direct_folder": self.chk_direct_folder.isChecked(),
            "hitomi_compat": self.chk_hitomi_name.isChecked(),
            "collection_id": collection_id,
            "content_mode": content_mode,
            "review_mode": review_mode,
            "text_images_destination": self.text_images_dest_edit.text().strip(),
            "text_destination": self.text_dest_edit.text().strip(),
            "date_after": date_after,
            "started_at": iso_utc(utc_now()),
        }
        return cmd, title, ctx

    def enqueue_likes_probe(self, *, scan_all: bool = False):
        try:
            cmd, title, ctx = self.build_likes_probe(scan_all=scan_all)
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
            f"[LIKES PROBE] 最大{self.likes_probe_spin.value():,}投稿だけ確認し、"
            + ("保存済み投稿を除いて確認箱／保存方針へ送ります。画像はまだDLしません。"
               if scan_all else "アンカーに到達した時点で停止します。画像はまだDLしません。")
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
        try:
            self.ensure_auth_ready(auth_mode, auth_value)
        except Exception as exc:
            QMessageBox.warning(self, "X認証が必要です", str(exc))
            return

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
        url, target_key, target_name = self.current_target()
        dest = Path(self.dest_edit.text().strip()).expanduser()
        dest.mkdir(parents=True, exist_ok=True)

        account_name, auth_mode, auth_value = self.current_auth()
        self.ensure_auth_ready(auth_mode, auth_value)
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
            if self.platform_combo.currentData() == "x" and self.selected_target() == "bookmarks":
                _name, mode, value = self.current_auth()
                cmd = build_x_bookmark_scan_command(self.engine_command(), auth_mode=mode, auth_value=value)
                safe = redact_command(cmd)
                self.log.append("[BOOKMARKS] 本文とメディア数だけ先に確認します。画像は振り分け後に取得します。")
            elif self.is_x_likes_incremental() or self.is_x_likes_review_flow():
                scan_all = not self.is_x_likes_incremental()
                cmd, _title, _ctx = self.build_likes_probe(scan_all=scan_all)
                safe = redact_command(cmd)
                self.log.append(
                    "[LIKES] 本文とメディア数を先に確認し、確認箱または指定した保存方針へ送ります。"
                    if self.is_x_likes_review_flow()
                    else "[LIKES] 1段目はアンカー探索（DLなし）。アンカー手前の投稿だけ2段目で直接DLします。"
                )
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
        if self.platform_combo.currentData() == "x" and self.selected_target() == "bookmarks":
            self.enqueue_bookmark_scan()
            return
        if self.is_x_likes_incremental() or self.is_x_likes_review_flow():
            self.enqueue_likes_probe(scan_all=not self.is_x_likes_incremental())
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

    def enqueue_bookmark_scan(self):
        collection = self.current_collection()
        if not collection:
            QMessageBox.warning(self, "取得設定が必要です", "Xブックマークは先に取得設定として保存してください。")
            return
        account = next((a for a in self.accounts if a.profile_id == collection.account_id), None)
        if not account:
            QMessageBox.warning(self, "認証アカウント", "この取得設定の認証アカウントが見つかりません。")
            return
        try:
            self.ensure_auth_ready(account.auth_mode, account.auth_value)
            command = build_x_bookmark_scan_command(
                self.engine_command(), auth_mode=account.auth_mode, auth_value=account.auth_value,
                max_posts=max(50, self.likes_probe_spin.value()),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Xブックマーク", str(exc)); return
        job = DownloadJob(f"X / {account.name} / {collection.name} / ブックマーク確認", command)
        job.smc_context = {
            "job_kind": "bookmark_scan", "collection_id": collection.collection_id,
            "login_name": account.name, "account_id": account.profile_id,
        }
        self.connect_job(job); self.jobs.append(job); self.queue_layout.addWidget(job)
        self.log.append("[BOOKMARKS] 最大件数まで本文・画像数を確認します。画像はまだダウンロードしません。")
        if self.active_job is None: self.start_next_job()

    def queue_collection_posts(
        self, collection: CollectionProfile, records: list[PostRecord], choices: dict[str, str],
    ):
        account = next((a for a in self.accounts if a.profile_id == collection.account_id), None)
        if not account:
            raise ValueError("認証アカウントが見つかりません。")
        destinations = {
            "images": Path(collection.destination or self.data_dir / "Library").expanduser(),
            "text_images": Path(
                collection.text_images_destination
                or collection.destination
                or self.data_dir / "Library"
            ).expanduser(),
            "text": Path(
                collection.text_destination
                or Path(collection.destination or self.data_dir / "Library") / "text"
            ).expanduser(),
        }
        media_groups = {
            mode: [
                rec for rec in records
                if choices.get(rec.post_id) == mode and rec.media_count > 0
            ]
            for mode in ("images", "text_images")
        }
        media_records = media_groups["images"] + media_groups["text_images"]
        immediate = [r for r in records if r not in media_records]
        markdown_paths: dict[str, str] = {}
        for rec in records:
            choice = choices.get(rec.post_id, "skip")
            markdown_path = ""
            if choice in {"text", "text_images"}:
                markdown_path = str(write_post_markdown(
                    rec,
                    destinations[choice],
                    activity_label="いいね取得日時" if collection.source == "likes" else "ブックマーク日時",
                ))
            markdown_paths[rec.post_id] = markdown_path
            if rec in immediate:
                self.catalog.set_collection_post_choice(
                    collection.collection_id, rec.post_id, choice,
                    state="processed", markdown_path=markdown_path,
                )
            else:
                self.catalog.set_collection_post_choice(
                    collection.collection_id, rec.post_id, choice,
                    state="queued", markdown_path=markdown_path,
                )
        if not media_records:
            self.refresh_inbox_count(); return
        try:
            if collection.source == "likes":
                raw = collection.target_value
                if collection.target_scope == "self":
                    raw = f"id:{account.user_id}" if account.user_id else account.username
                _url, target_key, _name = normalize_target("x", raw, "likes")
                archive_scope = f"{target_key}:likes"
            else:
                target_key = f"x:bookmarks:{collection.collection_id}"
                archive_scope = f"{target_key}:posts"
            profile = self.target_store.ensure(target_key, "x", collection.name)
            queued_jobs = []
            labels = {"images": "画像・動画のみ", "text_images": "本文＋画像・動画"}
            for mode, group in media_groups.items():
                if not group:
                    continue
                destination = destinations[mode]
                destination.mkdir(parents=True, exist_ok=True)
                base = core_build_command(
                    self.engine_command(), platform="x", url=group[0].source_url,
                    destination=destination, account_name=account.name,
                    auth_mode=account.auth_mode, auth_value=account.auth_value,
                    archive_scope=archive_scope, extensions=self.selected_extensions(),
                    capture_internal_metadata=True, use_archive=True,
                    archive_dir=self.data_dir / "archives", direct_folder=True,
                    range_mode="all", profile=profile, manual_date_after="", overlap_minutes=10,
                    hitomi_compat_x=True, target_type=collection.source,
                )
                command = append_urls(base, [r.source_url for r in group])
                job = DownloadJob(
                    f"X / {account.name} / {collection.name} / {labels[mode]}", command
                )
                job.smc_context = {
                    "job_kind": "collection_download", "collection_id": collection.collection_id,
                    "collection_post_ids": [r.post_id for r in group], "platform": "x",
                    "target_key": target_key, "target_name": collection.name,
                    "target_type": collection.source, "content_mode": mode,
                    "destination": str(destination), "login_name": account.name,
                    "started_at": iso_utc(utc_now()), "started_epoch": time.time(),
                    "hitomi_compat": True, "direct_folder": True,
                }
                queued_jobs.append(job)
            for job in queued_jobs:
                self.connect_job(job); self.jobs.append(job); self.queue_layout.addWidget(job)
            if self.active_job is None: self.start_next_job()
        except Exception:
            for rec in media_records:
                self.catalog.set_collection_post_choice(
                    collection.collection_id,
                    rec.post_id,
                    choices.get(rec.post_id, "images"),
                    state="pending",
                    markdown_path=markdown_paths.get(rec.post_id, ""),
                )
            self.refresh_inbox_count()
            raise

    def refresh_inbox_count(self):
        if hasattr(self, "inbox_btn"):
            count = self.catalog.pending_collection_post_count()
            self.inbox_btn.setText(f"確認箱  {count:,}件")
            self.inbox_btn.setEnabled(count > 0)

    def open_review_inbox(self):
        collection = self.current_collection()
        if not collection:
            return
        records = self.catalog.collection_posts(collection.collection_id, state="pending")
        if not records:
            QMessageBox.information(self, "確認箱", "この取得設定に未振り分けの投稿はありません。"); return
        dialog = QDialog(self); dialog.setWindowTitle(f"確認箱 — {collection.name}"); dialog.resize(820, 650)
        layout = QVBoxLayout(dialog)
        note = QLabel("投稿ごとに保存方法を選べます。上の一括指定も後から個別変更できます。")
        note.setWordWrap(True); layout.addWidget(note)
        bulk = QComboBox()
        for text, key in [("一括指定なし", ""), ("すべて本文＋画像", "text_images"), ("すべて画像のみ", "images"), ("すべて本文のみ", "text"), ("すべてスキップ", "skip")]:
            bulk.addItem(text, key)
        layout.addWidget(bulk)
        holder = QWidget(); rows = QVBoxLayout(holder); selectors = {}
        for rec in records:
            frame = QFrame(); frame.setObjectName("card"); row = QVBoxLayout(frame)
            title = QLabel(f"@{rec.author_name or rec.author_id}  ·  画像/動画 {rec.media_count}件")
            title.setObjectName("section"); row.addWidget(title)
            text = QLabel(rec.content or "（本文なし）"); text.setWordWrap(True); text.setTextInteractionFlags(Qt.TextSelectableByMouse); row.addWidget(text)
            select = QComboBox()
            for label, key in [("本文＋画像", "text_images"), ("画像のみ", "images"), ("本文のみ", "text"), ("スキップ", "skip")]:
                select.addItem(label, key)
            select.setCurrentIndex(max(0, select.findData(collection.content_mode)))
            row.addWidget(select); selectors[rec.post_id] = select; rows.addWidget(frame)
        rows.addStretch()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(holder); layout.addWidget(scroll, 1)
        bulk.currentIndexChanged.connect(lambda _i: [s.setCurrentIndex(s.findData(bulk.currentData())) for s in selectors.values() if bulk.currentData()])
        actions = QHBoxLayout(); cancel = QPushButton("閉じる"); apply_btn = QPushButton("振り分けを実行"); apply_btn.setObjectName("primary")
        cancel.clicked.connect(dialog.reject); apply_btn.clicked.connect(dialog.accept)
        actions.addStretch(); actions.addWidget(cancel); actions.addWidget(apply_btn); layout.addLayout(actions)
        if dialog.exec() == QDialog.Accepted:
            try:
                choices = {post_id: str(combo.currentData()) for post_id, combo in selectors.items()}
                self.queue_collection_posts(collection, records, choices)
                self.refresh_inbox_count()
            except Exception as exc:
                QMessageBox.warning(self, "振り分け", str(exc))

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

    def commit_likes_post_boundary(
        self, ctx: dict, posts: list[PostRecord], media_records: list[LikeSeenRecord],
    ) -> None:
        """Advance after post metadata is durable, including text-only Likes."""
        key, login = ctx["target_key"], ctx.get("login_name", "")
        old = self.catalog.likes_anchor_ids(target_key=key, login_profile=login)
        anchor_records = [
            LikeSeenRecord(
                rec.post_id, 1, rec.author_id, rec.author_name, rec.post_date, ""
            )
            for rec in posts
        ]
        rotated = self.catalog.rotate_likes_anchors(
            anchor_records,
            target_key=key,
            login_profile=login,
            max_posts=int(ctx.get("anchor_posts", 50)),
        )
        actual = self.catalog.likes_anchor_ids(target_key=key, login_profile=login)
        if actual != rotated["post_ids"]:
            raise RuntimeError("Likes投稿アンカーの保存後確認に失敗しました")
        seen = self.catalog.record_likes_seen(
            media_records, target_key=key, login_profile=login,
        )
        profile = self.target_store.ensure(key, "x", ctx.get("target_name", ""))
        profile.likes_anchor_at = iso_utc(utc_now())
        profile.likes_anchor_login = login
        profile.likes_anchor_posts = len(actual)
        profile.likes_seen_media = seen["media_total"]
        profile.likes_seen_posts = seen["posts_total"]
        self.target_store.save()
        self.log.append(
            f"[LIKES INBOX ANCHOR] {old[:1]} → {actual[:1]} / "
            f"本文込み{len(posts):,}投稿を永続化済み"
        )

    def job_finished(self, job: DownloadJob, code: int):
        ctx = getattr(job, "smc_context", {})
        logical_ok = not job.cancelled and (code == 0 or (ctx.get("job_kind") == "likes_probe" and bool(job.anchor_hit)))
        self.log.append(f"[{'OK' if logical_ok else 'ERROR'}] {job.title}")
        for line in job.output_lines[-80:]:
            self.log.append(line)

        if ctx.get("job_kind") == "bookmark_scan":
            collection = self.collection_store.get(ctx.get("collection_id", ""))
            try:
                if not logical_ok or not collection:
                    raise ValueError("ブックマーク確認が失敗・中断しました。")
                records = [rec for line in job.machine_lines if (rec := parse_smc_post_line(line))]
                known = self.catalog.collection_post_ids(collection.collection_id)
                boundary = find_bookmark_boundary(records, known)
                if not boundary["found"]:
                    raise ValueError(
                        "探索上限内で前回のブックマーク境界を検出できませんでした。"
                        "取りこぼし防止のため今回は追加せず、探索上限を増やして再確認してください。"
                    )
                new_records = list(boundary["records"])
                result = self.catalog.upsert_collection_posts(
                    collection.collection_id, new_records,
                    pending=collection.review_mode == "inbox",
                )
                self.log.append(
                    f"[BOOKMARKS OK] 確認 {len(records):,}投稿 / 新規 {result['added']:,}投稿"
                )
                if collection.review_mode == "inbox":
                    job.status.setText(f"確認箱へ {result['added']:,}投稿")
                    self.refresh_inbox_count()
                elif new_records:
                    choices = {r.post_id: collection.content_mode for r in new_records}
                    self.queue_collection_posts(collection, new_records, choices)
                    job.status.setText(f"自動振り分け {len(new_records):,}投稿")
                else:
                    job.status.setText("新規ブックマークなし")
            except Exception as exc:
                job.status.setText("ブックマーク確認エラー")
                self.log.append(f"[BOOKMARKS ERROR] {exc}")
            self.active_job = None; self.start_next_job(); return

        if ctx.get("job_kind") in {"bookmark_download", "collection_download"}:
            collection_download_ok = (
                logical_ok and not job.verification_error and not job.cancelled
                and safe_to_advance_download_state(
                    len(job.reported_file_paths), len(job.missing_reported_paths)
                )
            )
            state = "processed" if collection_download_ok else "pending"
            post_ids = ctx.get("collection_post_ids", ctx.get("bookmark_post_ids", []))
            for post_id in post_ids:
                row = self.catalog.conn.execute(
                    "SELECT choice,markdown_path FROM collection_posts WHERE collection_id=? AND post_id=?",
                    (ctx.get("collection_id", ""), post_id),
                ).fetchone()
                if row:
                    self.catalog.set_collection_post_choice(
                        ctx.get("collection_id", ""), post_id, row[0] or "images",
                        state=state, markdown_path=row[1] or "",
                    )
            self.refresh_inbox_count()

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
                post_records: list[PostRecord] = []
                for line in job.machine_lines:
                    rec = parse_smc_like_seen_line(line)
                    if rec and (
                        not rec.extension
                        or ("." + rec.extension.lower()) in MEDIA_EXTENSIONS
                    ):
                        records.append(rec)
                    post = parse_smc_post_line(line)
                    if post:
                        post_records.append(post)
                post_records = list({rec.post_id: rec for rec in post_records}.values())
                if post_records:
                    if ctx.get("scan_all"):
                        date_after = str(ctx.get("date_after") or "")
                        known_posts = self.catalog.collection_post_ids(
                            str(ctx.get("collection_id") or "")
                        )
                        new_post_records = [
                            rec for rec in post_records
                            if rec.post_id not in known_posts
                            and (not date_after or (rec.post_date or "")[:10] >= date_after)
                        ]
                        post_boundary = {
                            "found": True, "records": new_post_records,
                            "anchor_post_id": "",
                        }
                    else:
                        post_boundary = find_bookmark_boundary(post_records, ctx.get("anchor_ids", []))
                        new_post_records = list(post_boundary.get("records") or [])
                    new_ids = {rec.post_id for rec in new_post_records}
                    new_records = [rec for rec in records if rec.post_id in new_ids]
                    boundary = {
                        "found": post_boundary.get("found", False),
                        "anchor_post_id": post_boundary.get("anchor_post_id", ""),
                        "records_before_anchor": new_records,
                        "new_posts": len(new_post_records),
                        "new_media": len(new_records),
                    }
                else:
                    new_post_records = []
                    boundary = find_likes_anchor_boundary(records, ctx.get("anchor_ids", []))
                if not boundary.get("found"):
                    job.status.setText("確認保留 / アンカー未検出・ダウンロードなし")
                    self.log.append(
                        f"[LIKES SAFE STOP] 探索上限内でアンカーを検出できませんでした "
                        f"（確認 {len(post_records):,}投稿・{len(records):,}メディア）。昔削除した画像を復活させないため、"
                        "今回は1件もダウンロードしません。アンカーを作り直してください。"
                    )
                else:
                    new_records = list(boundary.get("records_before_anchor") or [])
                    new_posts = int(boundary.get("new_posts") or 0)
                    if ctx.get("scan_all"):
                        self.log.append(
                            f"[LIKES SCAN OK] 未登録候補 {new_posts:,}投稿 / {len(new_records):,}メディア。"
                        )
                    else:
                        self.log.append(
                            f"[LIKES PROBE OK] アンカー {boundary.get('anchor_post_id')} に到達。"
                            f"手前に候補 {new_posts:,}投稿 / {len(new_records):,}メディア。"
                        )
                    known, pending = self.classify_likes(ctx, new_records)
                    self.log.append(f"[LIKES CHECK] 取得済み {len(known):,} / 未取得候補 {len(pending):,}メディア")
                    collection = self.collection_store.get(str(ctx.get("collection_id") or ""))
                    collection_flow = bool(
                        collection and (
                            ctx.get("review_mode") == "inbox"
                            or ctx.get("content_mode") != "images"
                        )
                    )
                    if collection_flow and collection:
                        if not new_post_records and new_records:
                            grouped: dict[str, list[LikeSeenRecord]] = {}
                            for rec in new_records:
                                grouped.setdefault(rec.post_id, []).append(rec)
                            new_post_records = [
                                PostRecord(
                                    post_id=items[0].post_id,
                                    author_id=items[0].author_id,
                                    author_name=items[0].author_name,
                                    post_date=items[0].post_date,
                                    collected_date=iso_utc(utc_now()),
                                    content="",
                                    media_count=len(items),
                                )
                                for items in grouped.values()
                            ]
                        now = iso_utc(utc_now())
                        durable_posts = [
                            PostRecord(
                                rec.post_id, rec.author_id, rec.author_name, rec.post_date,
                                rec.collected_date or now, rec.content, rec.media_count,
                            )
                            for rec in new_post_records
                        ]
                        result = self.catalog.upsert_collection_posts(
                            collection.collection_id,
                            durable_posts,
                            pending=ctx.get("review_mode") == "inbox",
                        )
                        if durable_posts and not ctx.get("scan_all"):
                            self.commit_likes_post_boundary(ctx, durable_posts, new_records)
                        if ctx.get("review_mode") == "inbox":
                            job.status.setText(f"確認箱へ {result['added']:,}投稿")
                            self.refresh_inbox_count()
                            self.log.append(
                                f"[LIKES INBOX] 本文込み {result['added']:,}投稿を確認箱へ追加しました。"
                            )
                        elif durable_posts:
                            choices = {rec.post_id: str(ctx.get("content_mode") or "images") for rec in durable_posts}
                            self.queue_collection_posts(collection, durable_posts, choices)
                            job.status.setText(f"自動振り分け {len(durable_posts):,}投稿")
                        else:
                            job.status.setText("新規いいねなし")
                    elif not pending:
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
        idx = self.account_list.currentRow()
        if idx < 0 or idx >= len(self.accounts):
            QMessageBox.information(self, "pixivログイン", "先にpixivアカウントを選択してください。")
            return
        account = self.accounts[idx]
        if account.platform != "pixiv":
            QMessageBox.information(self, "pixivログイン", "選択中のアカウントはpixivではありません。")
            return
        if account.auth_mode != "managed_pixiv":
            QMessageBox.information(
                self,
                "pixivログイン",
                "このアカウントは従来方式です。「選択アカウントを編集」で、"
                "認証方式を「アプリ内pixiv連携」へ変更してください。",
            )
            return
        try:
            dialog = PixivLoginDialog(
                self.data_dir,
                account.profile_id,
                managed_pixiv_cache_path(self.data_dir, account.profile_id),
                self.engine_command(),
                self,
            )
            if dialog.exec() == QDialog.Accepted:
                self.refresh_accounts(account.profile_id)
                QMessageBox.information(
                    self,
                    "pixiv連携更新完了",
                    "このアカウント専用のpixiv連携を更新しました。そのまま取得できます。",
                )
        except Exception as exc:
            QMessageBox.warning(self, "アプリ内pixivログイン", str(exc))

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

    def check_for_updates(self, silent: bool = False):
        if self.update_check_worker and self.update_check_worker.isRunning():
            if not silent:
                self.footer_status.setText("更新を確認中です…")
            return
        self._update_check_silent = bool(silent)
        self.update_button.setEnabled(False)
        self.update_button.setText("確認中…")
        worker = UpdateCheckWorker(self)
        self.update_check_worker = worker
        worker.found.connect(self._update_found)
        worker.current.connect(self._update_current)
        worker.failed.connect(self._update_check_failed)
        worker.finished.connect(self._update_check_finished)
        worker.start()

    def _update_check_finished(self):
        self.update_button.setEnabled(True)
        self.update_button.setText("更新を確認")

    def _update_current(self):
        if not self._update_check_silent:
            QMessageBox.information(self, "更新確認", f"最新版です（v{APP_VERSION}）。")

    def _update_check_failed(self, message: str):
        if not self._update_check_silent:
            QMessageBox.warning(self, "更新確認", f"更新情報を取得できませんでした。\n\n{message}")

    def _update_found(self, info: dict):
        notes = str(info.get("notes") or "").strip()
        detail = f"\n\n{notes[:600]}" if notes else ""
        answer = QMessageBox.question(
            self,
            "アップデートがあります",
            f"SNS Media Collector v{info['version']} を利用できます。\n"
            f"ダウンロードして自動更新しますか？{detail}",
        )
        if answer != QMessageBox.Yes:
            return
        if self.active_job:
            QMessageBox.information(self, "更新待ち", "ダウンロード処理が終わってから、もう一度更新を実行してください。")
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("取得 0%")
        worker = UpdateDownloadWorker(info, self)
        self.update_download_worker = worker
        worker.progress_changed.connect(lambda value: self.update_button.setText(f"取得 {value}%"))
        worker.ready.connect(self._update_ready)
        worker.failed.connect(self._update_download_failed)
        worker.finished.connect(lambda: self.update_button.setEnabled(True))
        worker.start()

    def _update_download_failed(self, message: str):
        self.update_button.setText("更新を確認")
        QMessageBox.warning(self, "更新失敗", f"更新ファイルを取得できませんでした。\n\n{message}")

    def _update_ready(self, installer_text: str, version: str):
        installer = Path(installer_text)
        current_dir = Path(sys.executable).resolve().parent
        helper = current_dir / "bin" / "SNSMediaCollectorUpdater.exe"
        # The stable root launcher always resolves the newest installed version.
        # Existing releases still update safely because the installer also keeps
        # the versioned executable for rollback.
        target = current_dir.parent.parent / "SNSMediaCollector.exe"
        if not helper.is_file():
            self.update_button.setText("更新を確認")
            QMessageBox.warning(self, "更新準備", "自動更新プログラムが見つかりません。セットアップを手動で実行します。")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(installer)))
            return
        result = QProcess.startDetached(
            str(helper), [str(os.getpid()), str(installer), str(target)]
        )
        started = result[0] if isinstance(result, tuple) else bool(result)
        if not started:
            self.update_button.setText("更新を確認")
            QMessageBox.warning(self, "更新失敗", "自動更新プログラムを起動できませんでした。")
            return
        self.footer_status.setText(f"v{version}へ更新中… アプリを再起動します")
        QApplication.instance().quit()

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
