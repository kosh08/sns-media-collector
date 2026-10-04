"""Small, public X image previews, independent of download/archive state."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class PostPreview:
    url: str
    kind: str = "image"


def preview_image_url(value: str, size: str = "small") -> str:
    """Only fetch X's public image CDN; never a video or an arbitrary URL."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "pbs.twimg.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not parsed.path.startswith(("/media/", "/ext_tw_video_thumb/",
                                                "/amplify_video_thumb/", "/tweet_video_thumb/"))):
            return ""
        if len(value) > 2048 or any(c in value for c in ("\r", "\n", "\t", "\\")):
            return ""
        fmt = parse_qs(parsed.query).get("format", [""])[0]
        path = parsed.path
        suffix = path.rsplit(".", 1)[-1].lower()
        if suffix in {"jpg", "jpeg", "png", "webp"}:
            path = path.rsplit(".", 1)[0]
            fmt = suffix
        if fmt not in {"jpg", "jpeg", "png", "webp"}:
            return ""
        query = urlencode({"format": fmt, "name": "medium" if size == "medium" else "small"})
        return urlunsplit(("https", "pbs.twimg.com", path, query, ""))
    except (ValueError, TypeError, AttributeError):
        return ""


def parse_post_previews(value) -> tuple[PostPreview, ...]:
    try:
        items = json.loads(value) if isinstance(value, str) else value
        if not isinstance(items, list):
            return ()
        result = []
        seen = set()
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            url = preview_image_url(item.get("url", ""))
            kind = item.get("kind", "image")
            if url and url not in seen and kind in {"image", "video", "gif"}:
                result.append(PostPreview(url, kind))
                seen.add(url)
            if len(result) == 4:
                break
        return tuple(result)
    except (ValueError, TypeError):
        return ()


def post_previews_json(items) -> str:
    return json.dumps([asdict(item) for item in items], ensure_ascii=False)


def tweet_preview_metadata(tweet: dict) -> list[dict]:
    legacy = tweet.get("legacy", tweet)
    media = legacy.get("extended_entities", {}).get("media", [])
    candidates = []
    for item in media[:8]:
        kind = {"photo": "image", "video": "video", "animated_gif": "gif"}.get(item.get("type"))
        if kind:
            candidates.append({"url": item.get("media_url_https", ""), "kind": kind})
    return [asdict(item) for item in parse_post_previews(candidates)]
