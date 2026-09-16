from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def snowflake(dt: datetime) -> int:
    ms = int(dt.timestamp() * 1000)
    return ((ms - 1_288_834_974_657) << 22)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    print("SNS Media Collector startup smoke test")
    print("Python:", sys.version)

    # 1) Actual installed dependencies
    try:
        import PySide6
        import gallery_dl
        print("PySide6:", getattr(PySide6, "__version__", "unknown"))
        print("gallery-dl:", getattr(gallery_dl, "__version__", "unknown"))
    except Exception:
        traceback.print_exc()
        return 12

    # 2) Validate our Hitomi -> gallery-dl archive migration against the actual
    # installed gallery-dl archive implementation. No network access involved.
    try:
        from core import LikeSeenRecord, import_hitomi_x_archive, import_x_likes_seen_archive
        from gallery_dl import archive as gallery_archive

        with tempfile.TemporaryDirectory(prefix="smc-archive-smoke-") as td:
            root = Path(td) / "old"
            root.mkdir()
            tid = snowflake(datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc))
            (root / f"[26-08-08] {tid}_p0.jpg").write_bytes(b"smoke")
            apath = Path(td) / "archive.sqlite3"
            imported = import_hitomi_x_archive(root, apath)
            assert imported["archive_added"] == 1, imported

            arc = gallery_archive.connect(
                str(apath), "twitter", "{tweet_id}_{num}"
            )
            try:
                assert arc.check({"tweet_id": tid, "num": 1}), (
                    "Imported Hitomi entry is not recognized by gallery-dl"
                )
            finally:
                arc.close()

            likes_path = Path(td) / "likes.sqlite3"
            import_x_likes_seen_archive([LikeSeenRecord(str(tid), 1)], likes_path)
            larc = gallery_archive.connect(
                str(likes_path), "twitter", "{tweet_id}_{num}"
            )
            try:
                assert larc.check({"tweet_id": tid, "num": 1}), (
                    "Likes seen entry is not recognized by gallery-dl"
                )
            finally:
                larc.close()
        print("Hitomi + Likes archive compatibility: OK")
    except Exception:
        traceback.print_exc()
        return 15

    # 3) Create the real Qt MainWindow in offscreen mode and exercise command generation.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    with tempfile.TemporaryDirectory(prefix="smc-smoke-") as td:
        os.environ["SMC_TEST_DATA_DIR"] = td
        w = None
        try:
            from PySide6.QtWidgets import QApplication
            from app import MainWindow
            app = QApplication.instance() or QApplication([])
            w = MainWindow()
            w.url_edit.setText("@smc_test_user")
            w.dest_edit.setText(str(Path(td) / "hitomi-existing"))
            w.chk_direct_folder.setChecked(True)
            # Simulate a completed Hitomi migration: the first SMC run must use
            # the imported Tweet timestamp instead of silently requesting all history.
            profile = w.current_target_profile(create=True)
            profile.hitomi_baseline_at = "2026-08-09T08:00:00Z"
            profile.hitomi_latest_tweet_id = "2086000000000000000"
            w.target_store.save()
            w.sync_target_ui()
            assert "初回基準" in w.range_status.text(), w.range_status.text()
            cmd, title = w.build_command()
            joined = " ".join(cmd)
            assert "https://x.com/smc_test_user/media" in joined, joined
            assert "-D" in cmd, cmd
            assert "--download-archive" in cmd, cmd
            assert "--date-after" in cmd, cmd
            assert cmd[cmd.index("--date-after") + 1] == "2026-08-09T07:50:00Z", cmd
            assert "--write-metadata" not in cmd, cmd
            assert any("filename=[{date:%y-%m-%d}]" in x for x in cmd), cmd
            assert any("archive-format={tweet_id}_{num}" in x for x in cmd), cmd
            assert "--Print" in cmd, cmd
            assert "--no-mtime" in cmd, cmd
            from core import SMC_FILE_FORMAT, snapshot_media_files
            assert ("--Print", SMC_FILE_FORMAT) in list(zip(cmd, cmd[1:])), cmd

            # Strict save verification: an output event is not counted unless a new
            # file really exists on disk. Existing Hitomi files must never count.
            from app import DownloadJob
            verify_dir = Path(td) / "verify-save"
            verify_dir.mkdir()
            old_file = verify_dir / "old.jpg"
            old_file.write_bytes(b"old")
            vjob = DownloadJob("verify", [sys.executable, "-c", "pass"])
            vjob.verification_root = verify_dir
            vjob.verification_recursive = False
            vjob.preexisting_media = snapshot_media_files(verify_dir, recursive=False)
            assert not vjob._verify_file_path(str(old_file))
            assert vjob.files_saved_count == 0
            new_file = verify_dir / "new.jpg"
            new_file.write_bytes(b"new")
            assert vjob._verify_file_path(str(new_file))
            assert vjob.files_saved_count == 1
            assert Path(vjob.verified_file_paths[0]) == new_file
            print("Strict save verification gate: OK")

            # Likes incremental mode uses a bounded, no-download anchor probe.
            for b in w.target_buttons:
                if b.property("key") == "likes":
                    b.setChecked(True)
                    break
            w.sync_target_ui()
            likes_profile = w.current_target_profile(create=True)
            likes_profile.likes_anchor_at = "2026-08-09T09:00:00Z"
            likes_profile.likes_anchor_login = "認証なし"
            likes_profile.likes_anchor_posts = 2
            from core import LikeSeenRecord
            w.catalog.replace_likes_anchors(
                [LikeSeenRecord("3000000000000000001", 1), LikeSeenRecord("3000000000000000002", 1)],
                target_key=likes_profile.key, login_profile="認証なし", max_posts=2,
            )
            w.target_store.save()
            w.sync_target_ui()
            likes_cmd, _likes_title, likes_ctx = w.build_likes_probe()
            assert "--range" in likes_cmd, likes_cmd
            assert "-N" in likes_cmd, likes_cmd
            assert "--abort" not in likes_cmd, likes_cmd
            assert "--date-after" not in likes_cmd, likes_cmd
            assert "https://x.com/smc_test_user/likes" in likes_cmd, likes_cmd
            assert len(likes_ctx["anchor_ids"]) == 2, likes_ctx
            assert "Likesアンカー" in w.range_status.text(), w.range_status.text()

            # Recent-download gallery: persist a file and render one thumbnail card.
            sample = Path(td) / "hitomi-existing" / "[26-08-09] 2086000000000000000_p0.jpg"
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.write_bytes(b"not-a-real-jpeg-but-valid-path-for-fallback")
            w.catalog.record_recent_file(
                sample, platform="x", target_key="x:smc_test_user",
                login_profile="認証なし", target_name="smc_test_user",
            )
            w.refresh_recent_downloads()
            assert w.recent_grid.count() == 1, w.recent_grid.count()
            print("Recent-download gallery: OK")

            print("Qt MainWindow: OK")
            print("Media + Likes anchor command generation: OK -", title)
            w.close()
            app.processEvents()
        except Exception:
            traceback.print_exc()
            return 13
        finally:
            if w is not None:
                w.close()
            os.environ.pop("SMC_TEST_DATA_DIR", None)

    # 4) Verify gallery-dl CLI entry point itself.
    rc = subprocess.call([sys.executable, "-m", "gallery_dl", "--version"], cwd=str(ROOT))
    if rc:
        print("gallery-dl CLI failed:", rc)
        return 14
    print("gallery-dl CLI: OK")
    print("STARTUP SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
