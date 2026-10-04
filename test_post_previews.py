import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core import (Catalog, PostRecord, SMC_LIKE_POST_FORMAT, parse_smc_post_line,
                  build_x_post_preview_command, build_x_likes_probe_command)
from post_previews import (PostPreview, parse_post_previews, preview_image_url,
                           tweet_preview_metadata)

URL = "https://pbs.twimg.com/media/sample.jpg"
PREVIEW = PostPreview(preview_image_url(URL))


class PreviewTests(unittest.TestCase):
    def test_only_public_image_cdn_is_allowed(self):
        for value in ("http://pbs.twimg.com/media/a.jpg", "https://example.com/media/a.jpg",
                      "https://pbs.twimg.com.evil.test/media/a.jpg", "https://user@pbs.twimg.com/media/a.jpg",
                      "https://pbs.twimg.com:444/media/a.jpg", "https://video.twimg.com/a.mp4",
                      "https://pbs.twimg.com/not-media/a.jpg", "https://pbs.twimg.com/media/a.svg",
                      "https://pbs.twimg.com/media/a.jpg\n", None):
            self.assertEqual(preview_image_url(value), "", value)

    def test_original_url_is_reduced_and_medium_is_explicit(self):
        self.assertEqual(preview_image_url(URL), "https://pbs.twimg.com/media/sample?format=jpg&name=small")
        self.assertIn("name=medium", preview_image_url(URL, "medium"))
        self.assertIn("name=small", preview_image_url(PREVIEW.url.replace("small", "orig")))

    def test_deduplicated_and_bounded(self):
        items = [{"url": URL}] * 2 + [{"url": f"https://pbs.twimg.com/media/{i}.png"} for i in range(8)]
        parsed = parse_post_previews(items)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(len({p.url for p in parsed}), 4)

    def test_invalid_metadata_does_not_discard_post(self):
        line = 'SMC_POST\t123\t42\t"name"\t2026-01-01\t\t"body\\nnext"\t2'
        self.assertEqual(parse_smc_post_line(line + '\tbroken').content, "body\nnext")
        self.assertEqual(parse_smc_post_line(line).previews, ())
        enriched = parse_smc_post_line(line + '\t' + json.dumps([{"url": URL, "kind": "video"}]))
        self.assertEqual(enriched.previews[0].kind, "video")
        self.assertEqual(enriched.media_count, 2)

    def test_photo_video_and_gif_cover_metadata(self):
        raw = {"legacy": {"extended_entities": {"media": [
            {"type": kind, "media_url_https": URL.replace("sample", str(i)),
             "video_info": {"variants": [{"url": "https://video.twimg.com/private.mp4"}]}}
            for i, kind in enumerate(("photo", "video", "animated_gif"))]}}}
        result = tweet_preview_metadata(raw)
        self.assertEqual([p["kind"] for p in result], ["image", "video", "gif"])
        self.assertTrue(all("video.twimg.com" not in p["url"] for p in result))

    def test_real_gallery_formatter_launcher_and_parser_roundtrip(self):
        from gallery_dl import formatter
        from gallery_dl.extractor.twitter import TwitterExtractor
        from gallery_dl_launcher import enable_smc_post_previews
        metadata = {"tweet_id": 123, "author": {"id": 42, "name": "name"},
                    "date": datetime(2026, 1, 1, tzinfo=timezone.utc), "content": "本文\nnext", "count": 1}
        raw = {"extended_entities": {"media": [{"type": "photo", "media_url_https": URL}]}}
        with patch.object(TwitterExtractor, "_transform_tweet", lambda _self, _tweet: dict(metadata)), \
                patch("sys.argv", ["launcher", "--smc-post-previews"]):
            enable_smc_post_previews()
            enriched = TwitterExtractor._transform_tweet(None, raw)
        template = SMC_LIKE_POST_FORMAT.removeprefix("post:")
        record = parse_smc_post_line(formatter.StringFormatter(template).format_map(enriched))
        self.assertEqual(record.content, metadata["content"])
        self.assertEqual(record.media_count, 1)
        self.assertEqual(record.previews, (PREVIEW,))

    def test_preview_command_has_no_archive_or_media_write(self):
        record = PostRecord("123", author_name="name")
        command = build_x_post_preview_command(["launcher"], record, auth_mode="none", auth_value="")
        self.assertIn("--no-download", command)
        self.assertIn("--smc-post-previews", command)
        self.assertNotIn("--download-archive", command)
        self.assertEqual(command[-1], record.source_url)

    def test_real_extractor_preserves_video_count_urls_and_numbering(self):
        import copy
        from gallery_dl import extractor, formatter
        from gallery_dl.extractor.twitter import TwitterExtractor
        from gallery_dl_launcher import enable_smc_post_previews
        media = [{"type": kind, "media_url_https": URL.replace("sample", str(i)),
                  "original_info": {"width": 640, "height": 480}}
                 for i, kind in enumerate(("photo", "video", "animated_gif"))]
        for item in media[1:]:
            item["video_info"] = {"variants": [{"url": "https://video.twimg.com/" + item["type"] + ".mp4", "bitrate": 1000}]}
        tweet = {"legacy": {"id_str": "2086000000000000000", "lang": "ja", "entities": {},
                            "full_text": "投稿本文", "extended_entities": {"media": media}}, "user": {}}
        def messages():
            instance = extractor.find("https://x.com/name/status/2086000000000000000")
            instance._init_cookies = lambda: None
            instance.initialize()
            instance.login = lambda: None
            instance.metadata = lambda: {}
            instance.tweets = lambda: [copy.deepcopy(tweet)]
            instance._transform_user = lambda _user: {"id": 42, "name": "name"}
            return list(instance.items())
        original = TwitterExtractor._transform_tweet
        baseline = messages()
        try:
            with patch("sys.argv", ["launcher", "--smc-post-previews"]):
                enable_smc_post_previews()
            enhanced = messages()
        finally:
            TwitterExtractor._transform_tweet = original
        self.assertEqual([row[1] for row in enhanced], [row[1] for row in baseline])
        self.assertEqual([row[2].get("num") for row in enhanced], [row[2].get("num") for row in baseline])
        self.assertEqual(enhanced[0][2]["count"], 3)
        record = parse_smc_post_line(formatter.StringFormatter(SMC_LIKE_POST_FORMAT.removeprefix("post:")).format_map(enhanced[0][2]))
        self.assertEqual([p.kind for p in record.previews], ["image", "video", "gif"])
        self.assertEqual(record.media_count, 3)

    def test_existing_catalog_migrates_without_changing_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "catalog.db"
            catalog = Catalog(path)
            old = PostRecord("123", content="old body", media_count=1)
            catalog.upsert_collection_posts("c", [old])
            catalog.set_collection_post_choice("c", "123", "text", state="pending", markdown_path="keep.md")
            catalog.close()
            connection = sqlite3.connect(path)
            connection.execute("ALTER TABLE collection_posts DROP COLUMN previews_json")
            connection.commit()
            before = connection.execute("SELECT * FROM collection_posts").fetchone()
            connection.close()
            catalog = Catalog(path)
            catalog.update_collection_post_previews("c", replace(old, previews=(PREVIEW,)))
            after = catalog.conn.execute("SELECT * FROM collection_posts").fetchone()
            self.assertEqual(after[:-1], before)
            catalog.close()
            catalog = Catalog(path)
            self.assertEqual(catalog.collection_posts("c")[0].previews, (PREVIEW,))
            catalog.upsert_collection_posts("c", [old])
            self.assertEqual(catalog.collection_posts("c")[0].previews, (PREVIEW,))
            self.assertEqual(catalog.pending_collection_post_count("c"), 1)
            catalog.close()
