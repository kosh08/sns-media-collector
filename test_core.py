from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core import (
    Catalog,
    LikeSeenRecord,
    HITOMI_X_FILENAME,
    SMC_FILE_FORMAT,
    SMC_LIKE_SEEN_FORMAT,
    SMC_X_META_FORMAT,
    TargetProfile,
    TargetStore,
    X_ARCHIVE_FORMAT,
    archive_path_for,
    append_urls,
    build_command,
    build_x_likes_anchor_command,
    build_x_likes_baseline_command,
    build_x_likes_probe_command,
    find_likes_anchor_boundary,
    import_hitomi_x_archive,
    import_x_likes_seen_archive,
    incremental_date_after,
    normalize_target,
    parse_hitomi_x_filename,
    parse_gallery_datetime,
    parse_smc_file_line,
    parse_smc_like_seen_line,
    parse_smc_x_meta_line,
    redact_command,
    scan_hitomi_x_folder,
    select_likes_anchor_records,
    snapshot_media_files,
    new_media_since_snapshot,
    safe_to_advance_download_state,
    split_likes_baseline_records,
    likes_post_urls,
    x_archive_entry,
    x_tweet_id_to_datetime,
)


def snowflake(dt: datetime) -> int:
    ms = int(dt.timestamp() * 1000)
    return ((ms - 1_288_834_974_657) << 22)


class CoreTests(unittest.TestCase):
    def test_hitomi_filename_parser_p0_and_p1(self):
        tid = snowflake(datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc))
        a = parse_hitomi_x_filename(Path(f"[26-08-08] {tid}_p0.jpg"))
        b = parse_hitomi_x_filename(Path(f"[26-08-08] {tid}_p1.png"))
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a.page_index, 0)
        self.assertEqual(a.gallery_num, 1)
        self.assertEqual(b.page_index, 1)
        self.assertEqual(b.gallery_num, 2)

    def test_hitomi_scan_counts_unmatched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tid = snowflake(datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc))
            (root / f"[26-08-08] {tid}_p0.jpg").write_bytes(b"x")
            (root / f"[26-08-08] {tid}_p1.jpg").write_bytes(b"y")
            (root / "legacy-name.jpg").write_bytes(b"z")
            result = scan_hitomi_x_folder(root)
            self.assertEqual(result["media_count"], 3)
            self.assertEqual(result["matched"], 2)
            self.assertEqual(result["unmatched"], 1)
            self.assertEqual(result["latest_tweet_id"], str(tid))

    def test_hitomi_import_writes_gallery_dl_compatible_archive_keys(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "old"
            root.mkdir()
            tid1 = snowflake(datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc))
            tid2 = snowflake(datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc))
            (root / f"[26-08-08] {tid1}_p0.jpg").write_bytes(b"a")
            (root / f"[26-08-08] {tid1}_p1.jpg").write_bytes(b"b")
            (root / f"[26-08-09] {tid2}_p0.jpg").write_bytes(b"c")
            archive = Path(td) / "a.sqlite3"
            result = import_hitomi_x_archive(root, archive)
            self.assertEqual(result["matched"], 3)
            self.assertEqual(result["archive_added"], 3)
            self.assertEqual(result["latest_tweet_id"], str(tid2))
            con = sqlite3.connect(archive)
            try:
                entries = {r[0] for r in con.execute("SELECT entry FROM archive")}
            finally:
                con.close()
            self.assertEqual(entries, {
                x_archive_entry(tid1, 1),
                x_archive_entry(tid1, 2),
                x_archive_entry(tid2, 1),
            })

    def test_hitomi_import_drives_first_incremental_command_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "old"
            root.mkdir()
            latest_dt = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
            tid = snowflake(latest_dt)
            (root / f"[26-08-09] {tid}_p0.jpg").write_bytes(b"a")
            archive = Path(td) / "archive.sqlite3"
            imported = import_hitomi_x_archive(root, archive)
            p = TargetProfile(
                key="x:foo", platform="x", target_name="foo",
                hitomi_baseline_at=imported["latest_tweet_time"],
                hitomi_latest_tweet_id=imported["latest_tweet_id"],
            )
            cmd = build_command(
                ["gallery-dl"], platform="x", url="https://x.com/foo/media",
                destination=root, account_name="main", auth_mode="none", auth_value="",
                archive_scope="x:foo:media", extensions=["jpg"],
                capture_internal_metadata=False, use_archive=True, archive_dir=Path(td) / "archives",
                direct_folder=True, range_mode="incremental", profile=p, hitomi_compat_x=True,
            )
            self.assertEqual(imported["latest_tweet_id"], str(tid))
            self.assertEqual(cmd[cmd.index("--date-after") + 1], "2026-08-09T07:50:00Z")

    def test_hitomi_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "old"
            root.mkdir()
            tid = snowflake(datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc))
            (root / f"[26-08-08] {tid}_p0.jpg").write_bytes(b"a")
            archive = Path(td) / "a.sqlite3"
            first = import_hitomi_x_archive(root, archive)
            second = import_hitomi_x_archive(root, archive)
            self.assertEqual(first["archive_added"], 1)
            self.assertEqual(second["archive_added"], 0)
            self.assertEqual(second["archive_total"], 1)

    def test_archive_path_scopes_target_type(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = archive_path_for(root, platform="x", account_name="main", archive_scope="x:foo:media")
            b = archive_path_for(root, platform="x", account_name="main", archive_scope="x:foo:likes")
            self.assertNotEqual(a, b)

    def test_normalize_x_media(self):
        url, key, name = normalize_target("x", "@Foo_Bar", "media")
        self.assertEqual(url, "https://x.com/Foo_Bar/media")
        self.assertEqual(key, "x:foo_bar")
        self.assertEqual(name, "Foo_Bar")

    def test_incremental_overlap(self):
        p = TargetProfile(key="x:a", platform="x", target_name="a", last_scan_started_at="2026-08-09T08:00:00Z")
        self.assertEqual(incremental_date_after(p, 10), "2026-08-09T07:50:00Z")

    def test_hitomi_baseline_is_used_before_first_success(self):
        p = TargetProfile(
            key="x:a", platform="x", target_name="a",
            hitomi_baseline_at="2026-08-09T08:00:00Z",
            hitomi_latest_tweet_id="2086000000000000000",
        )
        self.assertEqual(incremental_date_after(p, 10), "2026-08-09T07:50:00Z")

    def test_real_scan_takes_priority_over_hitomi_baseline(self):
        p = TargetProfile(
            key="x:a", platform="x", target_name="a",
            last_scan_started_at="2026-08-09T10:00:00Z",
            hitomi_baseline_at="2026-08-09T08:00:00Z",
        )
        self.assertEqual(incremental_date_after(p, 10), "2026-08-09T09:50:00Z")

    def test_upgrade_migrates_v014_hitomi_baseline(self):
        old = {
            "key": "x:foo", "platform": "x", "target_name": "foo",
            "hitomi_matched_files": 42,
            "last_scan_started_at": "2026-08-09T08:00:00Z",
            "last_success_at": "",
        }
        p = TargetProfile.from_dict(old)
        self.assertEqual(p.hitomi_baseline_at, "2026-08-09T08:00:00Z")
        self.assertEqual(p.last_scan_started_at, "")

    def test_store_roundtrip_new_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "targets.json"
            s = TargetStore(path)
            p = s.ensure("x:foo", "x", "foo")
            p.destination = r"D:\\hitomi\\foo"
            p.imported_archive_entries = 99
            s.save()
            s2 = TargetStore(path)
            self.assertEqual(s2.get("x:foo").destination, r"D:\\hitomi\\foo")
            self.assertEqual(s2.get("x:foo").imported_archive_entries, 99)

    def test_x_incremental_without_baseline_refuses_silent_full_download(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = TargetProfile(key="x:foo", platform="x", target_name="foo")
            with self.assertRaisesRegex(ValueError, "初回取得の基準"):
                build_command(
                    ["gallery-dl"], platform="x", url="https://x.com/foo/media",
                    destination=root / "out", account_name="main", auth_mode="none", auth_value="",
                    archive_scope="x:foo:media", extensions=["jpg"],
                    capture_internal_metadata=False, use_archive=True, archive_dir=root / "archives",
                    direct_folder=True, range_mode="incremental", profile=p, hitomi_compat_x=True,
                )

    def test_x_command_uses_hitomi_baseline_on_first_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = TargetProfile(
                key="x:foo", platform="x", target_name="foo",
                hitomi_baseline_at="2026-08-09T08:00:00Z",
            )
            cmd = build_command(
                ["gallery-dl"], platform="x", url="https://x.com/foo/media",
                destination=root / "out", account_name="main", auth_mode="none", auth_value="",
                archive_scope="x:foo:media", extensions=["jpg"],
                capture_internal_metadata=False, use_archive=True, archive_dir=root / "archives",
                direct_folder=True, range_mode="incremental", profile=p, hitomi_compat_x=True,
            )
            self.assertIn("--date-after", cmd)
            self.assertEqual(cmd[cmd.index("--date-after") + 1], "2026-08-09T07:50:00Z")

    def test_x_command_is_hitomi_compatible_without_json_sidecars(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = TargetProfile(key="x:foo", platform="x", target_name="foo", last_scan_started_at="2026-08-09T08:00:00Z")
            cmd = build_command(
                ["gallery-dl"], platform="x", url="https://x.com/foo/media",
                destination=root / "out", account_name="main", auth_mode="none", auth_value="",
                archive_scope="x:foo:media", extensions=["jpg", "png"],
                capture_internal_metadata=True, use_archive=True, archive_dir=root / "archives",
                direct_folder=True, range_mode="incremental", profile=p, hitomi_compat_x=True,
            )
            joined = "\n".join(cmd)
            self.assertIn("-D", cmd)
            self.assertIn(f"filename={HITOMI_X_FILENAME}", cmd)
            self.assertIn(f"archive-format={X_ARCHIVE_FORMAT}", cmd)
            self.assertIn("--Print", cmd)
            self.assertIn(SMC_X_META_FORMAT, cmd)
            self.assertIn("--download-archive", cmd)
            self.assertIn("archive-event=file,skip", cmd)
            self.assertIn("--date-after", cmd)
            self.assertNotIn("--write-metadata", cmd)
            self.assertNotIn(".json", joined.lower())

    def test_x_command_archive_path_matches_import_path_helper(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = archive_path_for(root / "archives", platform="x", account_name="main", archive_scope="x:foo:media")
            cmd = build_command(
                ["gallery-dl"], platform="x", url="https://x.com/foo/media",
                destination=root / "out", account_name="main", auth_mode="none", auth_value="",
                archive_scope="x:foo:media", extensions=["jpg"], capture_internal_metadata=False,
                use_archive=True, archive_dir=root / "archives", direct_folder=True,
                range_mode="all", profile=None,
            )
            actual = Path(cmd[cmd.index("--download-archive") + 1])
            self.assertEqual(actual, expected)

    def test_pixiv_command_does_not_receive_x_compat_options(self):
        with tempfile.TemporaryDirectory() as td:
            cmd = build_command(
                ["gallery-dl"], platform="pixiv", url="https://www.pixiv.net/users/123",
                destination=Path(td), account_name="pix", auth_mode="pixiv_token", auth_value="SUPERSECRET",
                archive_scope="pixiv:123:media", extensions=["jpg"], capture_internal_metadata=True,
                use_archive=False, archive_dir=Path(td) / "a", direct_folder=False,
                range_mode="all", profile=None,
            )
            joined = "\n".join(cmd)
            self.assertNotIn("archive-format={tweet_id}_{num}", joined)
            self.assertNotIn("SMC_META", joined)
            redacted = " ".join(redact_command(cmd))
            self.assertNotIn("SUPERSECRET", redacted)
            self.assertIn("refresh-token=<hidden>", redacted)

    def test_managed_pixiv_uses_isolated_cache_instead_of_plaintext_token(self):
        with tempfile.TemporaryDirectory() as td:
            cmd = build_command(
                ["gallery-dl"], platform="pixiv", url="https://www.pixiv.net/users/123",
                destination=Path(td), account_name="pix", auth_mode="managed_pixiv",
                auth_value=str(Path(td) / "pixiv.sqlite3"), archive_scope="pixiv:123:media",
                extensions=["jpg"], capture_internal_metadata=False, use_archive=False,
                archive_dir=Path(td) / "archives", direct_folder=True, range_mode="all",
                profile=None,
            )
            self.assertIn("--cache-file", cmd)
            self.assertIn("--config-ignore", cmd)
            self.assertEqual(cmd[cmd.index("--cache-file") + 1], str(Path(td) / "pixiv.sqlite3"))
            self.assertIn("refresh-token=cache", cmd)

    def test_pixiv_bookmark_incremental_does_not_filter_by_artwork_date(self):
        """A newly bookmarked old artwork must remain eligible for download."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = TargetProfile(
                key="pixiv:123", platform="pixiv", target_name="123",
                last_scan_started_at="2026-09-18T10:00:00Z",
                last_success_at="2026-09-18T10:05:00Z",
            )
            cmd = build_command(
                ["gallery-dl"], platform="pixiv",
                url="https://www.pixiv.net/users/123/bookmarks/artworks",
                destination=root / "out", account_name="pix",
                auth_mode="managed_pixiv", auth_value=str(root / "pixiv.sqlite3"),
                archive_scope="pixiv:123:likes", extensions=["jpg", "png"],
                capture_internal_metadata=False, use_archive=True,
                archive_dir=root / "archives", direct_folder=True,
                range_mode="incremental", profile=profile, target_type="likes",
            )
            self.assertNotIn("--date-after", cmd)
            self.assertIn("--download-archive", cmd)
            self.assertEqual(cmd[-1], "https://www.pixiv.net/users/123/bookmarks/artworks")

    def test_pixiv_posts_incremental_still_uses_artwork_date(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = TargetProfile(
                key="pixiv:123", platform="pixiv", target_name="123",
                last_success_at="2026-09-18T10:05:00Z",
            )
            cmd = build_command(
                ["gallery-dl"], platform="pixiv",
                url="https://www.pixiv.net/users/123",
                destination=root / "out", account_name="pix",
                auth_mode="managed_pixiv", auth_value=str(root / "pixiv.sqlite3"),
                archive_scope="pixiv:123:posts", extensions=["jpg"],
                capture_internal_metadata=False, use_archive=True,
                archive_dir=root / "archives", direct_folder=True,
                range_mode="incremental", profile=profile, target_type="posts",
            )
            self.assertIn("--date-after", cmd)


    def test_gallery_datetime_python310_compatible_offset(self):
        dt = parse_gallery_datetime("2026-08-09T12:00:00+0900")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.strftime("%y-%m-%d"), "26-08-09")
        self.assertEqual(dt.utcoffset().total_seconds(), 9 * 3600)

    def test_parse_internal_metadata_and_catalog(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tid = snowflake(datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc))
            line = f"SMC_META\t{tid}\t99887766\tartist_name\t2026-08-09T12:00:00+0900\t2\tjpg"
            meta = parse_smc_x_meta_line(line)
            self.assertEqual(meta["post_id"], str(tid))
            self.assertEqual(meta["media_num"], 2)
            c = Catalog(root / "catalog.sqlite3")
            row = c.upsert_x_event(
                meta, target_key="x:artist_name", login_profile="main",
                destination=root / "images", hitomi_compat=True,
            )
            self.assertEqual(row["author_id"], "99887766")
            self.assertTrue(row["media_path"].endswith(f"[26-08-09] {tid}_p1.jpg"))
            dbrow = c.conn.execute("SELECT author_id,author_name,post_id FROM media").fetchone()
            self.assertEqual(dbrow, ("99887766", "artist_name", str(tid)))
            csv_path = root / "authors.csv"
            self.assertEqual(c.export_authors_csv(csv_path), 1)
            self.assertIn("99887766", csv_path.read_text(encoding="utf-8-sig"))
            c.close()

    def test_existing_folder_index_does_not_require_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.jpg").write_bytes(b"abc")
            (root / "b.mp4").write_bytes(b"def")
            (root / "ignore.txt").write_text("x")
            c = Catalog(root / "catalog.sqlite3")
            n = c.index_existing_folder(root)
            self.assertEqual(n, 2)
            rows = c.conn.execute("SELECT count(*) FROM existing_files").fetchone()[0]
            self.assertEqual(rows, 2)
            c.close()

    def test_tweet_snowflake_roundtrip_date(self):
        dt = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
        got = x_tweet_id_to_datetime(snowflake(dt))
        self.assertEqual(got.replace(microsecond=0), dt)


    def test_parse_likes_seen_line(self):
        tid = snowflake(datetime(2026, 8, 9, 9, 0, tzinfo=timezone.utc))
        line = f"SMC_LIKE_SEEN\t{tid}\t2\t9988\tartist\t2026-08-09T18:00:00+0900\tjpg"
        rec = parse_smc_like_seen_line(line)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.post_id, str(tid))
        self.assertEqual(rec.media_num, 2)
        self.assertEqual(rec.author_name, "artist")

    def test_likes_baseline_reserves_latest_posts_in_timeline_order(self):
        records = [
            LikeSeenRecord("3000000000000000001", 1),
            LikeSeenRecord("3000000000000000001", 2),
            LikeSeenRecord("3000000000000000002", 1),
            LikeSeenRecord("3000000000000000003", 1),
        ]
        baseline, reserved = split_likes_baseline_records(records, 2)
        self.assertEqual({r.post_id for r in reserved}, {
            "3000000000000000001", "3000000000000000002"
        })
        self.assertEqual({r.post_id for r in baseline}, {"3000000000000000003"})

    def test_likes_baseline_archive_prevents_deleted_items_from_returning(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / "likes.sqlite3"
            records = [
                LikeSeenRecord("3000000000000000001", 1),
                LikeSeenRecord("3000000000000000002", 1),
            ]
            result = import_x_likes_seen_archive(records, archive)
            self.assertEqual(result["archive_added"], 2)
            con = sqlite3.connect(archive)
            try:
                entries = {r[0] for r in con.execute("SELECT entry FROM archive")}
            finally:
                con.close()
            self.assertIn("twitter3000000000000000001_1", entries)
            self.assertIn("twitter3000000000000000002_1", entries)

    def test_likes_seen_catalog_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            c = Catalog(Path(td) / "catalog.sqlite3")
            records = [
                LikeSeenRecord("3000000000000000001", 1, "9", "artist"),
                LikeSeenRecord("3000000000000000001", 2, "9", "artist"),
            ]
            result = c.record_likes_seen(
                records, target_key="x:me", login_profile="main"
            )
            self.assertEqual(result["media_total"], 2)
            self.assertEqual(result["posts_total"], 1)
            self.assertEqual(c.likes_seen_counts(target_key="x:me", login_profile="main"), (2, 1))
            c.close()

    def test_likes_baseline_command_is_no_download_print_scan(self):
        cmd = build_x_likes_baseline_command(
            ["gallery-dl"], url="https://x.com/me/likes",
            auth_mode="browser", auth_value="firefox",
        )
        self.assertIn("-N", cmd)
        self.assertIn(SMC_LIKE_SEEN_FORMAT, cmd)
        self.assertIn("--cookies-from-browser", cmd)
        self.assertNotIn("--download-archive", cmd)
        self.assertNotIn("--date-after", cmd)

    def test_likes_anchor_selection_reserves_latest_and_caps_window(self):
        records = [
            LikeSeenRecord("3000000000000000001", 1),
            LikeSeenRecord("3000000000000000001", 2),
            LikeSeenRecord("3000000000000000002", 1),
            LikeSeenRecord("3000000000000000003", 1),
            LikeSeenRecord("3000000000000000004", 1),
        ]
        anchors, reserved = select_likes_anchor_records(
            records, reserve_latest_posts=1, anchor_posts=2
        )
        self.assertEqual({r.post_id for r in reserved}, {"3000000000000000001"})
        self.assertEqual(
            {r.post_id for r in anchors},
            {"3000000000000000002", "3000000000000000003"},
        )

    def test_anchor_command_is_bounded_no_download_scan(self):
        cmd = build_x_likes_anchor_command(
            ["gallery-dl"], url="https://x.com/me/likes",
            auth_mode="browser", auth_value="firefox",
            reserve_latest_posts=5, anchor_posts=50,
        )
        self.assertIn("--post-range", cmd)
        self.assertEqual(cmd[cmd.index("--post-range") + 1], "1-75")
        self.assertIn("-N", cmd)
        self.assertNotIn("--download-archive", cmd)

    def test_probe_command_is_bounded_no_download_scan(self):
        cmd = build_x_likes_probe_command(
            ["gallery-dl"], url="https://x.com/me/likes",
            auth_mode="none", auth_value="", max_media=321,
        )
        self.assertIn("--range", cmd)
        self.assertEqual(cmd[cmd.index("--range") + 1], "1-321")
        self.assertIn("-N", cmd)
        self.assertNotIn("--download-archive", cmd)

    def test_anchor_boundary_only_returns_records_before_first_anchor(self):
        records = [
            LikeSeenRecord("4000000000000000001", 1, author_name="new1"),
            LikeSeenRecord("4000000000000000001", 2, author_name="new1"),
            LikeSeenRecord("4000000000000000002", 1, author_name="new2"),
            LikeSeenRecord("3000000000000000005", 1, author_name="old"),
            LikeSeenRecord("2000000000000000001", 1, author_name="deleted_old"),
        ]
        got = find_likes_anchor_boundary(records, {"3000000000000000005"})
        self.assertTrue(got["found"])
        self.assertEqual(got["new_posts"], 2)
        self.assertEqual(got["new_media"], 3)
        self.assertNotIn(
            "2000000000000000001",
            {r.post_id for r in got["records_before_anchor"]},
        )

    def test_anchor_not_found_is_safe_zero_download_boundary(self):
        records = [
            LikeSeenRecord("4000000000000000001", 1),
            LikeSeenRecord("2000000000000000001", 1),  # old deleted candidate
        ]
        got = find_likes_anchor_boundary(records, {"3000000000000000005"})
        self.assertFalse(got["found"])
        self.assertEqual(got["new_posts"], 0)
        self.assertEqual(got["records_before_anchor"], [])

    def test_likes_post_urls_deduplicate_posts(self):
        records = [
            LikeSeenRecord("4000000000000000001", 1, author_name="artist"),
            LikeSeenRecord("4000000000000000001", 2, author_name="artist"),
            LikeSeenRecord("4000000000000000002", 1, author_name="bad name"),
        ]
        urls = likes_post_urls(records)
        self.assertEqual(urls[0], "https://x.com/artist/status/4000000000000000001")
        self.assertEqual(urls[1], "https://x.com/i/status/4000000000000000002")
        self.assertEqual(len(urls), 2)

    def test_append_urls_replaces_single_input_url(self):
        cmd = append_urls(
            ["gallery-dl", "--windows-filenames", "https://x.com/me/likes"],
            ["https://x.com/a/status/1", "https://x.com/b/status/2"],
        )
        self.assertEqual(
            cmd[-2:],
            ["https://x.com/a/status/1", "https://x.com/b/status/2"],
        )

    def test_anchor_catalog_replace_and_rotate(self):
        with tempfile.TemporaryDirectory() as td:
            c = Catalog(Path(td) / "catalog.sqlite3")
            old = [
                LikeSeenRecord("3000000000000000001", 1),
                LikeSeenRecord("3000000000000000002", 1),
                LikeSeenRecord("3000000000000000003", 1),
            ]
            got = c.replace_likes_anchors(
                old, target_key="x:me", login_profile="main", max_posts=3
            )
            self.assertEqual(got["posts"], 3)
            self.assertEqual(c.likes_anchor_ids(target_key="x:me", login_profile="main"), [
                "3000000000000000001", "3000000000000000002", "3000000000000000003"
            ])
            new = [LikeSeenRecord("4000000000000000001", 1)]
            c.rotate_likes_anchors(
                new, target_key="x:me", login_profile="main", max_posts=3
            )
            self.assertEqual(c.likes_anchor_ids(target_key="x:me", login_profile="main"), [
                "4000000000000000001", "3000000000000000001", "3000000000000000002"
            ])
            c.close()

    def test_core_rejects_old_abort_style_likes_incremental(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "アンカー確認パイプライン"):
                build_command(
                    ["gallery-dl"], platform="x", url="https://x.com/me/likes",
                    destination=Path(td) / "out", account_name="main", auth_mode="none", auth_value="",
                    archive_scope="x:me:likes", extensions=["jpg"], capture_internal_metadata=False,
                    use_archive=True, archive_dir=Path(td) / "archives", direct_folder=True,
                    range_mode="incremental", profile=TargetProfile(key="x:me", platform="x", target_name="me"),
                    target_type="likes",
                )

    def test_file_event_parser_keeps_windows_path(self):
        line = "SMC_FILE\t" + r"D:\hitomi\artist\[26-08-09] 2086000000000000000_p0.jpg"
        self.assertEqual(
            parse_smc_file_line(line),
            r"D:\hitomi\artist\[26-08-09] 2086000000000000000_p0.jpg",
        )

    def test_download_command_emits_after_file_event_for_live_preview(self):
        with tempfile.TemporaryDirectory() as td:
            cmd = build_command(
                ["gallery-dl"], platform="x", url="https://x.com/foo/media",
                destination=Path(td) / "out", account_name="main", auth_mode="none", auth_value="",
                archive_scope="x:foo:media", extensions=["jpg"], capture_internal_metadata=False,
                use_archive=False, archive_dir=Path(td) / "archives", direct_folder=True,
                range_mode="all", profile=None,
            )
            pairs = list(zip(cmd, cmd[1:]))
            self.assertIn(("--Print", SMC_FILE_FORMAT), pairs)
            self.assertNotIn("--write-metadata", cmd)

    def test_pixiv_download_also_emits_live_preview_file_event(self):
        with tempfile.TemporaryDirectory() as td:
            cmd = build_command(
                ["gallery-dl"], platform="pixiv", url="https://www.pixiv.net/users/123",
                destination=Path(td) / "out", account_name="main", auth_mode="none", auth_value="",
                archive_scope="pixiv:123:media", extensions=["jpg"], capture_internal_metadata=False,
                use_archive=False, archive_dir=Path(td) / "archives", direct_folder=True,
                range_mode="all", profile=None,
            )
            self.assertIn(("--Print", SMC_FILE_FORMAT), list(zip(cmd, cmd[1:])))


    def test_download_command_keeps_explorer_mtime_current(self):
        with tempfile.TemporaryDirectory() as td:
            cmd = build_command(
                ["gallery-dl"], platform="x", url="https://x.com/foo/media",
                destination=Path(td) / "out", account_name="main", auth_mode="none", auth_value="",
                archive_scope="x:foo:media", extensions=["jpg"], capture_internal_metadata=False,
                use_archive=False, archive_dir=Path(td) / "archives", direct_folder=True,
                range_mode="all", profile=None,
            )
            self.assertIn("--no-mtime", cmd)

    def test_save_snapshot_detects_only_new_files_without_using_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / "old.jpg"
            old.write_bytes(b"old")
            before = snapshot_media_files(root, recursive=False)
            # Changing content/mtime of an existing filename must not make it a new save.
            old.write_bytes(b"changed-existing")
            new = root / "new.jpg"
            new.write_bytes(b"new")
            (root / "ignore.txt").write_text("x", encoding="utf-8")
            found = new_media_since_snapshot(before, root, recursive=False)
            self.assertEqual(found, [new])

    def test_save_snapshot_recursive_mode_finds_nested_download(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            before = snapshot_media_files(root, recursive=True)
            nested = root / "artist" / "new.png"
            nested.parent.mkdir()
            nested.write_bytes(b"png")
            self.assertEqual(new_media_since_snapshot(before, root, recursive=True), [nested])


    def test_save_integrity_blocks_state_advance_on_missing_reported_file(self):
        self.assertFalse(safe_to_advance_download_state(1, 1))
        self.assertFalse(safe_to_advance_download_state(2, 1))
        self.assertTrue(safe_to_advance_download_state(1, 0))
        self.assertTrue(safe_to_advance_download_state(0, 0))

    def test_recent_files_catalog_roundtrip_and_metadata_join(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            c = Catalog(root / "catalog.sqlite3")
            media = root / "[26-08-09] 2086000000000000000_p0.jpg"
            media.write_bytes(b"x")
            c.record_recent_file(
                media, platform="x", target_key="x:foo", login_profile="main", target_name="foo"
            )
            meta = {
                "post_id": "2086000000000000000",
                "author_id": "12345",
                "author_name": "artist",
                "post_date": "2026-08-09T10:00:00+0900",
                "media_num": 1,
                "extension": "jpg",
            }
            c.upsert_x_event(
                meta, target_key="x:foo", login_profile="main",
                destination=root, hitomi_compat=True,
            )
            rows = c.recent_files(10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(Path(rows[0]["path"]), media)
            self.assertEqual(rows[0]["author_name"], "artist")
            self.assertEqual(rows[0]["source_url"], "https://x.com/artist/status/2086000000000000000")
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
