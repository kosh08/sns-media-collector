from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif",
    ".mp4", ".webm", ".m4v", ".mov", ".mkv", ".zip",
}

# Hitomi Downloader style currently used by the user's existing X folders:
#   [26-08-08] 2085923968318386579_p0.jpg
HITOMI_X_RE = re.compile(
    r"^\[(?P<date>\d{2}-\d{2}-\d{2})\]\s+"
    r"(?P<tweet_id>\d{15,20})_p(?P<page>\d+)"
    r"(?P<suffix>.*?)\.(?P<ext>[A-Za-z0-9]+)$",
    re.IGNORECASE,
)
HITOMI_X_FILENAME = "[{date:%y-%m-%d}] {tweet_id}_p{num:A-1}.{extension}"
# Deliberately omit retweet_id. This makes the archive key reconstructable from
# Hitomi's TweetID + pN filename alone. gallery-dl prefixes archive keys with the
# extractor category by default, so X entries are twitter{tweet_id}_{num}.
X_ARCHIVE_FORMAT = "{tweet_id}_{num}"
SMC_META_PREFIX = "SMC_META\t"
SMC_LIKE_SEEN_PREFIX = "SMC_LIKE_SEEN\t"
SMC_FILE_PREFIX = "SMC_FILE\t"
SMC_POST_PREFIX = "SMC_POST\t"
SMC_X_META_FORMAT = (
    "prepare:SMC_META\t{tweet_id}\t{author[id]}\t{author[name]}\t"
    "{date:%Y-%m-%dT%H:%M:%S%z}\t{num}\t{extension}"
)
SMC_LIKE_SEEN_FORMAT = (
    "prepare:SMC_LIKE_SEEN\t{tweet_id}\t{num}\t{author[id]}\t{author[name]}\t"
    "{date:%Y-%m-%dT%H:%M:%S%z}\t{extension}"
)
# Emitted only after a file has been successfully handled by gallery-dl.
# This powers Hitomi-style live thumbnails without writing sidecar files.
SMC_FILE_FORMAT = "after:SMC_FILE\t{_path}"
SMC_POST_FORMAT = (
    "directory:SMC_POST\t{tweet_id}\t{author[id]}\t{author[name]!j}\t"
    "{date:%Y-%m-%dT%H:%M:%S%z}\t{date_bookmarked:%Y-%m-%dT%H:%M:%S%z}\t"
    "{content!j}\t{count}"
)


def atomic_write_json(path: Path, value) -> None:
    """Replace only a fully flushed file; an interrupted write preserves the old one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def normalized_local_path(path: Path | str) -> str:
    """Stable local path key for duplicate/save verification."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def snapshot_media_files(root: Path | str, recursive: bool = True) -> dict[str, int]:
    """Snapshot existing media paths without relying on mtime.

    gallery-dl normally applies HTTP Last-Modified timestamps, so mtime cannot be
    used to decide whether a file was created by the current job.  Path existence
    is the reliable baseline because gallery-dl skips existing files by default.
    """
    base = Path(root).expanduser()
    out: dict[str, int] = {}
    if not base.exists() or not base.is_dir():
        return out
    it = base.rglob("*") if recursive else base.iterdir()
    for item in it:
        try:
            if item.is_file() and item.suffix.lower() in MEDIA_EXTENSIONS:
                out[normalized_local_path(item)] = int(item.stat().st_size)
        except OSError:
            continue
    return out


def new_media_since_snapshot(
    before: dict[str, int] | set[str], root: Path | str, recursive: bool = True
) -> list[Path]:
    """Return media paths that did not exist in the pre-job snapshot."""
    old = set(before)
    base = Path(root).expanduser()
    if not base.exists() or not base.is_dir():
        return []
    out: list[Path] = []
    it = base.rglob("*") if recursive else base.iterdir()
    for item in it:
        try:
            if not item.is_file() or item.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            if normalized_local_path(item) not in old:
                out.append(item)
        except OSError:
            continue
    return out


def safe_to_advance_download_state(reported_count: int, missing_count: int) -> bool:
    """Whether a completed job is safe to use as the next incremental boundary.

    A gallery-dl file event that points to no actual new/existing file is a
    diagnostic failure.  Do not advance Likes anchors or incremental timestamps
    in that situation, otherwise the missing item could be skipped forever.
    """
    return not (int(reported_count) > 0 and int(missing_count) > 0)


@dataclass
class TargetProfile:
    key: str
    platform: str
    target_name: str
    destination: str = ""
    direct_folder: bool = True
    last_success_at: str = ""
    last_scan_started_at: str = ""
    author_id: str = ""
    author_name: str = ""
    indexed_at: str = ""
    indexed_files: int = 0
    imported_archive_entries: int = 0
    hitomi_matched_files: int = 0
    hitomi_baseline_at: str = ""
    hitomi_latest_tweet_id: str = ""
    hitomi_source_folder: str = ""
    likes_baseline_at: str = ""
    likes_baseline_login: str = ""
    likes_seen_media: int = 0
    likes_seen_posts: int = 0
    likes_anchor_at: str = ""
    likes_anchor_login: str = ""
    likes_anchor_posts: int = 0
    likes_anchor_media: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "TargetProfile":
        fields = cls.__dataclass_fields__
        item = cls(**{k: v for k, v in data.items() if k in fields})
        # v0.1.4.x used last_scan_started_at as the Hitomi migration baseline.
        # Preserve that state when upgrading, but only when there has not yet been
        # a successful SNS Media Collector scan.
        if (
            not item.hitomi_baseline_at
            and item.hitomi_matched_files
            and item.last_scan_started_at
            and not item.last_success_at
        ):
            item.hitomi_baseline_at = item.last_scan_started_at
            item.last_scan_started_at = ""
        return item


class TargetStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._items: dict[str, TargetProfile] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self._items = {
                    x["key"]: TargetProfile.from_dict(x)
                    for x in raw if isinstance(x, dict) and x.get("key")
                }
            elif isinstance(raw, dict):
                self._items = {
                    k: TargetProfile.from_dict(v)
                    for k, v in raw.items() if isinstance(v, dict)
                }
            else:
                self._items = {}
        except Exception:
            self._items = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        items = [asdict(self._items[k]) for k in sorted(self._items)]
        atomic_write_json(self.path, items)

    def get(self, key: str) -> Optional[TargetProfile]:
        return self._items.get(key)

    def ensure(self, key: str, platform: str, target_name: str) -> TargetProfile:
        item = self._items.get(key)
        if item is None:
            item = TargetProfile(key=key, platform=platform, target_name=target_name)
            self._items[key] = item
        else:
            item.platform = platform
            item.target_name = target_name or item.target_name
        return item

    def all(self) -> list[TargetProfile]:
        return list(self._items.values())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def incremental_baseline(
    profile: Optional[TargetProfile],
) -> tuple[Optional[datetime], str]:
    """Return the effective incremental baseline and its source.

    Successful SMC scans take priority.  Before the first successful scan, an
    imported Hitomi folder can supply the baseline from its newest Tweet ID.
    """
    if not profile:
        return None, ""
    for value, source in (
        (profile.last_scan_started_at, "last_scan"),
        (profile.last_success_at, "last_success"),
        (profile.hitomi_baseline_at, "hitomi"),
    ):
        dt = parse_iso(value)
        if dt:
            return dt, source
    return None, ""


def incremental_date_after(
    profile: Optional[TargetProfile], overlap_minutes: int = 10
) -> str:
    """Return gallery-dl compatible cutoff with a safety overlap."""
    dt, _source = incremental_baseline(profile)
    if not dt:
        return ""
    dt = dt - timedelta(minutes=max(0, overlap_minutes))
    return iso_utc(dt)


def x_tweet_id_to_datetime(tweet_id: int) -> Optional[datetime]:
    try:
        if tweet_id < 300_000_000_000_000:
            return None
        ms = (int(tweet_id) >> 22) + 1_288_834_974_657
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        if dt.year < 2006 or dt > utc_now() + timedelta(days=2):
            return None
        return dt
    except Exception:
        return None


def infer_latest_x_tweet_from_filenames(root: Path) -> tuple[str, str]:
    root = Path(root)
    best_id = 0
    best_dt: Optional[datetime] = None
    if not root.exists():
        return "", ""
    rx = re.compile(r"(?<!\d)(\d{15,20})(?!\d)")
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        for token in rx.findall(p.stem):
            try:
                tid = int(token)
            except ValueError:
                continue
            dt = x_tweet_id_to_datetime(tid)
            if dt and (best_dt is None or dt > best_dt):
                best_id, best_dt = tid, dt
    return (str(best_id), iso_utc(best_dt)) if best_dt else ("", "")


@dataclass(frozen=True)
class HitomiXRecord:
    path: Path
    date_text: str
    tweet_id: str
    page_index: int  # Hitomi p0 = 0
    gallery_num: int  # gallery-dl num starts at 1
    extension: str


def parse_hitomi_x_filename(path: Path) -> Optional[HitomiXRecord]:
    p = Path(path)
    m = HITOMI_X_RE.match(p.name)
    if not m:
        return None
    try:
        page = int(m.group("page"))
        tid = int(m.group("tweet_id"))
    except ValueError:
        return None
    if page < 0 or x_tweet_id_to_datetime(tid) is None:
        return None
    return HitomiXRecord(
        path=p,
        date_text=m.group("date"),
        tweet_id=str(tid),
        page_index=page,
        gallery_num=page + 1,
        extension=m.group("ext").lower(),
    )


def scan_hitomi_x_folder(root: Path) -> dict:
    """Parse an existing Hitomi X folder without touching its files."""
    root = Path(root)
    records: list[HitomiXRecord] = []
    media_count = 0
    unmatched = 0
    latest_id = ""
    latest_dt: Optional[datetime] = None
    if not root.exists():
        return {
            "media_count": 0,
            "matched": 0,
            "unmatched": 0,
            "records": records,
            "latest_tweet_id": "",
            "latest_tweet_time": "",
        }
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        media_count += 1
        rec = parse_hitomi_x_filename(p)
        if not rec:
            unmatched += 1
            continue
        records.append(rec)
        dt = x_tweet_id_to_datetime(int(rec.tweet_id))
        if dt and (latest_dt is None or dt > latest_dt):
            latest_dt = dt
            latest_id = rec.tweet_id
    return {
        "media_count": media_count,
        "matched": len(records),
        "unmatched": unmatched,
        "records": records,
        "latest_tweet_id": latest_id,
        "latest_tweet_time": iso_utc(latest_dt) if latest_dt else "",
    }


def safe_name(text: str, fallback: str = "default") -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")[:90]
    return value or fallback


def archive_path_for(
    archive_dir: Path,
    *,
    platform: str,
    account_name: str,
    archive_scope: str,
) -> Path:
    archive_dir = Path(archive_dir)
    scope = safe_name(archive_scope, "target")
    return archive_dir / f"{platform}_{safe_name(account_name)}_{scope}.sqlite3"


def x_archive_entry(tweet_id: str | int, gallery_num: int) -> str:
    if int(gallery_num) < 1:
        raise ValueError("gallery_num must be >= 1")
    return f"twitter{int(tweet_id)}_{int(gallery_num)}"


def import_hitomi_x_archive(root: Path, archive_path: Path) -> dict:
    """Import Hitomi filenames directly into a gallery-dl compatible archive.

    SNS Media Collector forces X's archive-format to ``{tweet_id}_{num}``, while
    gallery-dl's default archive-prefix remains ``twitter``. Therefore a Hitomi
    ``..._p0`` file maps exactly to ``twitter<TweetID>_1``.
    """
    result = scan_hitomi_x_folder(root)
    records: list[HitomiXRecord] = result["records"]
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(archive_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS archive "
            "(entry TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        before = con.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        with con:
            con.executemany(
                "INSERT OR IGNORE INTO archive(entry) VALUES(?)",
                ((x_archive_entry(r.tweet_id, r.gallery_num),) for r in records),
            )
        after = con.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
    finally:
        con.close()
    result = dict(result)
    result.pop("records", None)
    result["archive_path"] = str(archive_path)
    result["archive_total"] = after
    result["archive_added"] = after - before
    return result



@dataclass(frozen=True)
class LikeSeenRecord:
    post_id: str
    media_num: int
    author_id: str = ""
    author_name: str = ""
    post_date: str = ""
    extension: str = ""


@dataclass(frozen=True)
class PostRecord:
    post_id: str
    author_id: str = ""
    author_name: str = ""
    post_date: str = ""
    collected_date: str = ""
    content: str = ""
    media_count: int = 0

    @property
    def source_url(self) -> str:
        name = self.author_name.strip()
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
            return f"https://x.com/{name}/status/{self.post_id}"
        return f"https://x.com/i/status/{self.post_id}"


def parse_smc_post_line(line: str) -> Optional[PostRecord]:
    if not line.startswith(SMC_POST_PREFIX):
        return None
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) != 8 or parts[0] != "SMC_POST":
        return None
    _, post_id, author_id, author_json, post_date, collected_date, content_json, media_count = parts
    try:
        int(post_id)
        author_name = json.loads(author_json)
        content = json.loads(content_json)
        count = max(0, int(media_count or 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(author_name, str) or not isinstance(content, str):
        return None
    return PostRecord(post_id, author_id, author_name, post_date, collected_date, content, count)


def build_x_bookmark_scan_command(
    engine: list[str], *, auth_mode: str, auth_value: str, max_posts: int = 300,
) -> list[str]:
    """Collect bookmark metadata, including text-only posts, without media downloads."""
    limit = max(10, int(max_posts))
    cmd = list(engine) + ["--windows-filenames", "--no-input"]
    _append_auth(cmd, "x", auth_mode, auth_value)
    cmd += [
        "-o", "extractor.twitter.text-tweets=true",
        "--post-range", f"1-{limit}",
        "--print", SMC_POST_FORMAT,
        "--no-download", "https://x.com/i/bookmarks",
    ]
    return cmd


def find_bookmark_boundary(records: Iterable[PostRecord], known_post_ids: Iterable[str]) -> dict:
    """Return only records before the first known bookmark; never leap over a missing boundary."""
    items = list(records)
    known = {str(x) for x in known_post_ids if str(x)}
    if not known:
        return {"found": True, "first_run": True, "records": items, "anchor_post_id": ""}
    for index, record in enumerate(items):
        if record.post_id in known:
            return {
                "found": True, "first_run": False,
                "records": items[:index], "anchor_post_id": record.post_id,
            }
    return {"found": False, "first_run": False, "records": [], "anchor_post_id": ""}


def write_post_markdown(record: PostRecord, destination: Path) -> Path:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = (record.collected_date or record.post_date or "undated")[:10]
    safe_stamp = re.sub(r"[^0-9-]", "", stamp) or "undated"
    path = destination / f"[{safe_stamp}] {record.post_id}.md"
    body = (
        f"# @{record.author_name or record.author_id or 'unknown'}\n\n"
        f"- 元投稿: {record.source_url}\n"
        f"- 投稿日時: {record.post_date or '不明'}\n"
        f"- ブックマーク日時: {record.collected_date or '不明'}\n\n"
        f"{record.content.strip()}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def partition_likes_records(records: Iterable[LikeSeenRecord], archive_path: Optional[Path],
                            *, destination: Optional[Path] = None,
                            hitomi_compat: bool = False) -> tuple[list[LikeSeenRecord], list[LikeSeenRecord]]:
    """Classify each media, never assume one archived image completes its post.

    Read the archive without creating or changing it. Missing archives mean no
    known entries; unreadable/corrupt archives raise so callers hold the boundary.
    Existing nonempty Hitomi files are also evidence. Archive entries intentionally
    remain authoritative after the user deletes old downloads.
    """
    records = list(dict.fromkeys(records))
    entries = set()
    if archive_path is not None and Path(archive_path).exists():
        con = sqlite3.connect(Path(archive_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for r in records:
                entry = x_archive_entry(r.post_id, r.media_num)
                if con.execute("SELECT 1 FROM archive WHERE entry=?", (entry,)).fetchone():
                    entries.add(entry)
        finally:
            con.close()
    known, pending = [], []
    for r in records:
        present = x_archive_entry(r.post_id, r.media_num) in entries
        if not present and destination is not None and hitomi_compat:
            dt = parse_gallery_datetime(r.post_date)
            if dt and re.fullmatch(r"[a-zA-Z0-9]+", r.extension):
                path = Path(destination) / f"[{dt:%y-%m-%d}] {r.post_id}_p{r.media_num - 1}.{r.extension}"
                present = path.is_file() and path.stat().st_size > 0
        (known if present else pending).append(r)
    return known, pending


def parse_smc_like_seen_line(line: str) -> Optional[LikeSeenRecord]:
    if not line.startswith(SMC_LIKE_SEEN_PREFIX):
        return None
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) != 7 or parts[0] != "SMC_LIKE_SEEN":
        return None
    _, post_id, num, author_id, author_name, post_date, extension = parts
    try:
        int(post_id)
        media_num = int(num)
    except ValueError:
        return None
    if media_num < 1:
        return None
    return LikeSeenRecord(
        post_id=post_id,
        media_num=media_num,
        author_id=author_id,
        author_name=author_name,
        post_date=post_date,
        extension=extension.lower(),
    )


def split_likes_baseline_records(
    records: Iterable[LikeSeenRecord], reserve_latest_posts: int = 0
) -> tuple[list[LikeSeenRecord], list[LikeSeenRecord]]:
    """Split a Likes scan into baseline records and deliberately-unseen records.

    gallery-dl emits Likes in timeline order.  ``reserve_latest_posts`` leaves the
    first N unique media posts out of the baseline so a migration can immediately
    download a handful of likes made after the old Hitomi collection stopped.
    """
    items = list(records)
    reserve = max(0, int(reserve_latest_posts))
    if not reserve:
        return items, []
    latest_ids: list[str] = []
    latest_set: set[str] = set()
    for rec in items:
        if rec.post_id in latest_set:
            continue
        latest_set.add(rec.post_id)
        latest_ids.append(rec.post_id)
        if len(latest_ids) >= reserve:
            break
    reserved_ids = set(latest_ids)
    baseline = [r for r in items if r.post_id not in reserved_ids]
    kept_new = [r for r in items if r.post_id in reserved_ids]
    return baseline, kept_new


def select_likes_anchor_records(
    records: Iterable[LikeSeenRecord],
    *,
    reserve_latest_posts: int = 0,
    anchor_posts: int = 50,
) -> tuple[list[LikeSeenRecord], list[LikeSeenRecord]]:
    """Return (anchors, reserved_new) from a short Likes scan.

    Records arrive in Likes timeline order. The newest ``reserve_latest_posts``
    unique media posts are deliberately left outside the anchor so they can be
    downloaded immediately after migrating from Hitomi. The following
    ``anchor_posts`` unique media posts become the moving boundary.
    """
    items = list(records)
    reserve = max(0, int(reserve_latest_posts))
    keep = max(1, int(anchor_posts))
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for rec in items:
        if rec.post_id in seen:
            continue
        seen.add(rec.post_id)
        ordered_ids.append(rec.post_id)
    reserved_ids = set(ordered_ids[:reserve])
    anchor_ids = set(ordered_ids[reserve: reserve + keep])
    reserved = [r for r in items if r.post_id in reserved_ids]
    anchors = [r for r in items if r.post_id in anchor_ids]
    return anchors, reserved


def find_likes_anchor_boundary(
    records: Iterable[LikeSeenRecord], anchor_post_ids: Iterable[str]
) -> dict:
    """Find the first previously-known anchor in a Likes probe.

    Nothing after the first anchor is considered new. This is intentionally
    conservative: when no anchor is found, callers must *not* download anything,
    which prevents old manually-deleted Hitomi files from being resurrected.
    """
    items = list(records)
    anchors = {str(x) for x in anchor_post_ids if str(x)}
    for idx, rec in enumerate(items):
        if rec.post_id in anchors:
            new = items[:idx]
            return {
                "found": True,
                "anchor_post_id": rec.post_id,
                "media_before_anchor": idx,
                "records_before_anchor": new,
                "new_posts": len({r.post_id for r in new}),
                "new_media": len(new),
            }
    return {
        "found": False,
        "anchor_post_id": "",
        "media_before_anchor": 0,
        "records_before_anchor": [],
        "new_posts": 0,
        "new_media": 0,
    }


def likes_post_urls(records: Iterable[LikeSeenRecord]) -> list[str]:
    """Build one stable X status URL per unique media post, preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for rec in records:
        if rec.post_id in seen:
            continue
        seen.add(rec.post_id)
        name = rec.author_name.strip()
        if re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
            out.append(f"https://x.com/{name}/status/{rec.post_id}")
        else:
            out.append(f"https://x.com/i/status/{rec.post_id}")
    return out


def build_x_likes_anchor_command(
    engine: list[str], *, url: str, auth_mode: str, auth_value: str,
    reserve_latest_posts: int = 0, anchor_posts: int = 50,
) -> list[str]:
    """Scan only the top of Likes and emit metadata without downloading."""
    reserve = max(0, int(reserve_latest_posts))
    anchors = max(1, int(anchor_posts))
    # A small cushion accounts for text-only Likes which emit no file record.
    scan_posts = reserve + anchors + 20
    cmd = list(engine) + ["--windows-filenames", "--no-input"]
    _append_auth(cmd, "x", auth_mode, auth_value)
    cmd += ["--post-range", f"1-{scan_posts}", "-N", SMC_LIKE_SEEN_FORMAT, url]
    return cmd


def build_x_likes_probe_command(
    engine: list[str], *, url: str, auth_mode: str, auth_value: str,
    max_media: int = 500,
) -> list[str]:
    """No-download probe used to find a saved anchor near the top of Likes."""
    limit = max(10, int(max_media))
    cmd = list(engine) + ["--windows-filenames", "--no-input"]
    _append_auth(cmd, "x", auth_mode, auth_value)
    cmd += ["--range", f"1-{limit}", "-N", SMC_LIKE_SEEN_FORMAT, url]
    return cmd


def append_urls(command: list[str], urls: Iterable[str]) -> list[str]:
    """Replace the final input URL of a gallery-dl command with URL list."""
    values = [str(x) for x in urls if str(x)]
    if not values:
        raise ValueError("download URL list is empty")
    if not command:
        raise ValueError("command is empty")
    return list(command[:-1]) + values


def import_x_likes_seen_archive(
    records: Iterable[LikeSeenRecord], archive_path: Path
) -> dict:
    """Mark Likes media as already seen in the same archive used for downloads."""
    unique: dict[tuple[str, int], LikeSeenRecord] = {}
    for rec in records:
        unique[(rec.post_id, rec.media_num)] = rec
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(archive_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS archive "
            "(entry TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        before = con.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        with con:
            con.executemany(
                "INSERT OR IGNORE INTO archive(entry) VALUES(?)",
                ((x_archive_entry(r.post_id, r.media_num),) for r in unique.values()),
            )
        after = con.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
    finally:
        con.close()
    return {
        "scanned_media": len(unique),
        "scanned_posts": len({r.post_id for r in unique.values()}),
        "archive_added": after - before,
        "archive_total": after,
        "archive_path": str(archive_path),
    }


def _append_auth(cmd: list[str], platform: str, auth_mode: str, auth_value: str) -> None:
    if auth_mode == "managed_x" and auth_value:
        cmd += ["--config-ignore", "--cookies", auth_value]
    elif auth_mode == "cookies_file" and auth_value:
        cmd += ["--cookies", auth_value]
    elif auth_mode == "browser" and auth_value:
        cmd += ["--cookies-from-browser", auth_value]
    elif platform == "pixiv" and auth_mode == "pixiv_token" and auth_value:
        cmd += ["-o", f"refresh-token={auth_value}"]
    elif platform == "pixiv" and auth_mode == "managed_pixiv" and auth_value:
        cmd += ["--config-ignore", "--cache-file", auth_value, "-o", "refresh-token=cache"]


def build_x_likes_baseline_command(
    engine: list[str], *, url: str, auth_mode: str, auth_value: str
) -> list[str]:
    """Backward-compatible alias for the bounded anchor scan."""
    return build_x_likes_anchor_command(
        engine, url=url, auth_mode=auth_mode, auth_value=auth_value,
        reserve_latest_posts=0, anchor_posts=50,
    )

def normalize_target(platform: str, raw: str, target: str) -> tuple[str, str, str]:
    """Return (normalized_url, target_key, target_name)."""
    s = raw.strip()
    if not s:
        raise ValueError("URL / ユーザー名 / IDを入力してください。")

    if platform == "x":
        if target == "bookmarks":
            return "https://x.com/i/bookmarks", "x:self:bookmarks", "自分のブックマーク"
        m_id = re.fullmatch(r"id:(\d+)", s, re.I)
        if m_id:
            uid = m_id.group(1)
            base = f"https://x.com/id:{uid}"
            url = base + ({"media": "/media", "likes": "/likes"}.get(target, ""))
            return url, f"x:id:{uid}", f"id:{uid}"
        m_status = re.search(
            r"(?:x\.com|twitter\.com)/([^/?#]+)/status/(\d+)", s, re.I
        )
        if m_status:
            username = m_status.group(1)
            return s, f"x:{username.lower()}", username

        if s.startswith("@"):
            username = s[1:].strip()
        elif re.fullmatch(r"[A-Za-z0-9_]{1,15}", s):
            username = s
        else:
            m = re.search(r"(?:x\.com|twitter\.com)/([^/?#]+)", s, re.I)
            if not m:
                return (
                    s,
                    f"x:url:{hashlib.sha1(s.encode('utf-8')).hexdigest()[:16]}",
                    s,
                )
            username = m.group(1)
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
            raise ValueError("Xのユーザー名を判定できませんでした。")
        base = f"https://x.com/{username}"
        url = base + ({"media": "/media", "likes": "/likes"}.get(target, ""))
        return url, f"x:{username.lower()}", username

    if s.isdigit():
        uid = s
    else:
        m = re.search(r"pixiv\.net/(?:[a-z]{2}/)?users/(\d+)", s, re.I)
        if not m:
            return (
                s,
                f"pixiv:url:{hashlib.sha1(s.encode('utf-8')).hexdigest()[:16]}",
                s,
            )
        uid = m.group(1)
    url = (
        f"https://www.pixiv.net/users/{uid}/bookmarks/artworks"
        if target == "likes"
        else f"https://www.pixiv.net/users/{uid}"
    )
    return url, f"pixiv:{uid}", uid


def build_command(
    engine: list[str],
    *,
    platform: str,
    url: str,
    destination: Path,
    account_name: str,
    auth_mode: str,
    auth_value: str,
    archive_scope: str,
    extensions: Iterable[str],
    capture_internal_metadata: bool,
    use_archive: bool,
    archive_dir: Path,
    direct_folder: bool,
    range_mode: str,
    profile: Optional[TargetProfile],
    manual_date_after: str = "",
    overlap_minutes: int = 10,
    hitomi_compat_x: bool = True,
    target_type: str = "",
    likes_abort_after: int = 100,
    emit_file_events: bool = True,
) -> list[str]:
    destination = Path(destination)
    cmd = list(engine) + [
        "--windows-filenames",
        # Keep Explorer's modification time at the actual download time.
        # gallery-dl otherwise applies HTTP Last-Modified timestamps, which can
        # make a newly downloaded file look old when sorting by 更新日時.
        "--no-mtime",
        "-D" if direct_folder else "-d",
        str(destination),
    ]

    if auth_mode == "managed_x" and auth_value:
        cmd += ["--config-ignore", "--cookies", auth_value]
    elif auth_mode == "cookies_file" and auth_value:
        cmd += ["--cookies", auth_value]
    elif auth_mode == "browser" and auth_value:
        cmd += ["--cookies-from-browser", auth_value]
    elif platform == "pixiv" and auth_mode == "pixiv_token" and auth_value:
        cmd += ["-o", f"refresh-token={auth_value}"]
    elif platform == "pixiv" and auth_mode == "managed_pixiv" and auth_value:
        cmd += ["--config-ignore", "--cache-file", auth_value, "-o", "refresh-token=cache"]

    ext = [x.lower().lstrip(".") for x in extensions if x]
    if ext:
        expr = ",".join(repr(x) for x in ext)
        cmd += ["--filter", f"extension.lower() in ({expr},)"]

    # Hitomi-style live preview: report the exact file path after each
    # successful download. --Print keeps downloads enabled.
    if emit_file_events:
        cmd += ["--Print", SMC_FILE_FORMAT]

    # X compatibility layer:
    # - archive key reconstructable from TweetID + pN
    # - Hitomi-style p0 filenames for new downloads
    # - metadata emitted to stdout and consumed by our SQLite catalog, never as
    #   sidecar JSON files in the image folder.
    if platform == "x":
        cmd += ["-o", f"archive-format={X_ARCHIVE_FORMAT}"]
        if hitomi_compat_x:
            cmd += ["-o", f"filename={HITOMI_X_FILENAME}"]
        if capture_internal_metadata:
            cmd += ["--Print", SMC_X_META_FORMAT]

    if use_archive:
        if platform == "x":
            # If a matching Hitomi-style file already exists on disk, let gallery-dl
            # add the skipped item to the archive as an extra safety net.
            cmd += ["-o", "archive-event=file,skip"]
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_path_for(
            archive_dir,
            platform=platform,
            account_name=account_name,
            archive_scope=archive_scope,
        )
        cmd += ["--download-archive", str(archive)]

    if range_mode == "incremental":
        if platform == "x" and target_type == "likes":
            raise ValueError(
                "X Likesの差分取得はアンカー確認パイプラインを使います。"
                "GUIの『新しいいいねだけ』から実行してください。"
            )
        elif platform == "pixiv" and target_type == "likes":
            # pixiv's bookmark feed is ordered by when an item was bookmarked,
            # but the extractor's ``date`` value is the artwork creation date.
            # Applying --date-after here loses older artworks that were only
            # bookmarked recently.  Walk the feed and let the target-scoped
            # download archive (plus gallery-dl's existing-file checks) skip
            # media we have already saved.
            pass
        else:
            cutoff = incremental_date_after(profile, overlap_minutes=overlap_minutes)
            if cutoff:
                cmd += ["--date-after", cutoff]
            elif platform == "x":
                raise ValueError(
                    "初回取得の基準がありません。既存Hitomiフォルダを引き継ぐか、"
                    "取得範囲で『すべて』または『日付以降』を選んでください。"
                )
    elif range_mode == "date" and manual_date_after.strip():
        cmd += ["--date-after", manual_date_after.strip()]

    cmd.append(url)
    return cmd


def redact_command(cmd: Iterable[str]) -> list[str]:
    out: list[str] = []
    hide_next = False
    for part in cmd:
        if hide_next:
            out.append("<hidden>")
            hide_next = False
            continue
        if part == "--cookies":
            out.append(part)
            hide_next = True
            continue
        if "refresh-token=" in part:
            prefix = part.split("refresh-token=", 1)[0]
            out.append(prefix + "refresh-token=<hidden>")
            continue
        out.append(part)
    return out


def parse_smc_file_line(line: str) -> Optional[str]:
    if not line.startswith(SMC_FILE_PREFIX):
        return None
    # Keep the full remainder intact; Windows paths may contain spaces.
    value = line.rstrip("\r\n").split("\t", 1)
    if len(value) != 2:
        return None
    path = value[1].strip()
    return path or None


def parse_smc_x_meta_line(line: str) -> Optional[dict]:
    if not line.startswith(SMC_META_PREFIX):
        return None
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) != 7 or parts[0] != "SMC_META":
        return None
    _, post_id, author_id, author_name, post_date, num, extension = parts
    try:
        media_num = int(num)
        int(post_id)
    except ValueError:
        return None
    if media_num < 1:
        return None
    return {
        "post_id": post_id,
        "author_id": author_id,
        "author_name": author_name,
        "post_date": post_date,
        "media_num": media_num,
        "extension": extension.lower(),
    }


def parse_gallery_datetime(value: str) -> Optional[datetime]:
    """Parse gallery-dl style timestamps on Python 3.10+.

    gallery-dl's ``%z`` output is commonly ``+0900`` while
    ``datetime.fromisoformat()`` on Python 3.10 only accepts ``+09:00``.
    Python 3.11 broadened ISO-8601 parsing, so normalize the offset ourselves
    to keep behavior identical on 3.10 and newer.
    """
    text = (value or "").strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    # +0900 / -0430 -> +09:00 / -04:30
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Conservative fallbacks for timestamps without timezone / with space.
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime((value or "").strip(), fmt)
            except ValueError:
                continue
    return None


class Catalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS media (
              metadata_path TEXT PRIMARY KEY,
              media_path TEXT,
              platform TEXT,
              target_key TEXT,
              login_profile TEXT,
              author_id TEXT,
              author_name TEXT,
              post_id TEXT,
              source_url TEXT,
              post_date TEXT,
              indexed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_media_author ON media(platform, author_id, author_name);
            CREATE INDEX IF NOT EXISTS idx_media_post ON media(platform, post_id);

            CREATE TABLE IF NOT EXISTS existing_files (
              path TEXT PRIMARY KEY,
              size INTEGER NOT NULL,
              mtime_ns INTEGER NOT NULL,
              sha256 TEXT,
              indexed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_existing_hash ON existing_files(sha256);

            CREATE TABLE IF NOT EXISTS likes_seen (
              target_key TEXT NOT NULL,
              login_profile TEXT NOT NULL,
              post_id TEXT NOT NULL,
              media_num INTEGER NOT NULL,
              author_id TEXT,
              author_name TEXT,
              post_date TEXT,
              extension TEXT,
              state TEXT NOT NULL DEFAULT 'seen',
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              PRIMARY KEY(target_key, login_profile, post_id, media_num)
            );
            CREATE INDEX IF NOT EXISTS idx_likes_seen_post
              ON likes_seen(target_key, login_profile, post_id);

            CREATE TABLE IF NOT EXISTS likes_anchors (
              target_key TEXT NOT NULL,
              login_profile TEXT NOT NULL,
              post_id TEXT NOT NULL,
              rank INTEGER NOT NULL,
              author_id TEXT,
              author_name TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY(target_key, login_profile, post_id)
            );
            CREATE INDEX IF NOT EXISTS idx_likes_anchor_rank
              ON likes_anchors(target_key, login_profile, rank);

            CREATE TABLE IF NOT EXISTS recent_files (
              path TEXT PRIMARY KEY,
              platform TEXT,
              target_key TEXT,
              login_profile TEXT,
              target_name TEXT,
              detected_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_recent_files_detected
              ON recent_files(detected_at DESC);

            CREATE TABLE IF NOT EXISTS collection_posts (
              collection_id TEXT NOT NULL,
              post_id TEXT NOT NULL,
              author_id TEXT,
              author_name TEXT,
              post_date TEXT,
              collected_date TEXT,
              content TEXT,
              media_count INTEGER NOT NULL DEFAULT 0,
              source_url TEXT,
              state TEXT NOT NULL DEFAULT 'pending',
              choice TEXT NOT NULL DEFAULT '',
              markdown_path TEXT NOT NULL DEFAULT '',
              first_seen_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(collection_id, post_id)
            );
            CREATE INDEX IF NOT EXISTS idx_collection_posts_state
              ON collection_posts(collection_id, state, collected_date DESC, post_id DESC);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert_collection_posts(
        self, collection_id: str, records: Iterable[PostRecord], *, pending: bool = True,
    ) -> dict:
        now = iso_utc(utc_now())
        unique = {r.post_id: r for r in records}
        added = 0
        with self.conn:
            for rec in unique.values():
                exists = self.conn.execute(
                    "SELECT 1 FROM collection_posts WHERE collection_id=? AND post_id=?",
                    (collection_id, rec.post_id),
                ).fetchone()
                if not exists:
                    added += 1
                self.conn.execute(
                    """INSERT INTO collection_posts(
                         collection_id,post_id,author_id,author_name,post_date,collected_date,
                         content,media_count,source_url,state,first_seen_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(collection_id,post_id) DO UPDATE SET
                         author_id=excluded.author_id,author_name=excluded.author_name,
                         post_date=excluded.post_date,collected_date=excluded.collected_date,
                         content=excluded.content,media_count=excluded.media_count,
                         source_url=excluded.source_url,updated_at=excluded.updated_at""",
                    (
                        collection_id, rec.post_id, rec.author_id, rec.author_name,
                        rec.post_date, rec.collected_date, rec.content, rec.media_count,
                        rec.source_url, "pending" if pending else "ready", now, now,
                    ),
                )
        return {"scanned": len(unique), "added": added}

    def collection_posts(self, collection_id: str, *, state: str = "pending", limit: int = 500) -> list[PostRecord]:
        rows = self.conn.execute(
            """SELECT post_id,author_id,author_name,post_date,collected_date,content,media_count
               FROM collection_posts WHERE collection_id=? AND state=?
               ORDER BY collected_date DESC, post_id DESC LIMIT ?""",
            (collection_id, state, max(1, int(limit))),
        ).fetchall()
        return [PostRecord(*row) for row in rows]

    def collection_post_ids(self, collection_id: str) -> set[str]:
        return {str(row[0]) for row in self.conn.execute(
            "SELECT post_id FROM collection_posts WHERE collection_id=?", (collection_id,)
        )}

    def set_collection_post_choice(
        self, collection_id: str, post_id: str, choice: str, *,
        state: str = "processed", markdown_path: str = "",
    ) -> None:
        if choice not in {"images", "text_images", "text", "skip"}:
            raise ValueError("invalid post choice")
        with self.conn:
            self.conn.execute(
                """UPDATE collection_posts SET choice=?,state=?,markdown_path=?,updated_at=?
                   WHERE collection_id=? AND post_id=?""",
                (choice, state, markdown_path, iso_utc(utc_now()), collection_id, post_id),
            )

    def pending_collection_post_count(self, collection_id: str = "") -> int:
        if collection_id:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM collection_posts WHERE collection_id=? AND state='pending'",
                (collection_id,),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM collection_posts WHERE state='pending'").fetchone()
        return int(row[0] or 0)

    def upsert_x_event(
        self,
        meta: dict,
        *,
        target_key: str,
        login_profile: str,
        destination: Path,
        hitomi_compat: bool,
    ) -> dict:
        post_id = str(meta.get("post_id") or "")
        author_id = str(meta.get("author_id") or "")
        author_name = str(meta.get("author_name") or "")
        post_date = str(meta.get("post_date") or "")
        media_num = int(meta.get("media_num") or 0)
        extension = str(meta.get("extension") or "")
        if not post_id or media_num < 1:
            raise ValueError("invalid X metadata event")

        source_url = (
            f"https://x.com/{author_name}/status/{post_id}"
            if author_name
            else f"https://x.com/i/status/{post_id}"
        )
        media_path = ""
        if hitomi_compat and extension:
            try:
                dt = parse_gallery_datetime(post_date)
                if dt is None:
                    raise ValueError("invalid gallery-dl post date")
                date_text = dt.strftime("%y-%m-%d")
                media_path = str(
                    Path(destination)
                    / f"[{date_text}] {post_id}_p{media_num - 1}.{extension}"
                )
            except Exception:
                pass
        key = f"internal:x:{target_key}:{post_id}:{media_num}"
        now = iso_utc(utc_now())
        self.conn.execute(
            """INSERT INTO media(metadata_path, media_path, platform, target_key, login_profile,
                   author_id, author_name, post_id, source_url, post_date, indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(metadata_path) DO UPDATE SET
                 media_path=excluded.media_path, platform=excluded.platform,
                 target_key=excluded.target_key, login_profile=excluded.login_profile,
                 author_id=excluded.author_id, author_name=excluded.author_name,
                 post_id=excluded.post_id, source_url=excluded.source_url,
                 post_date=excluded.post_date, indexed_at=excluded.indexed_at""",
            (
                key,
                media_path,
                "x",
                target_key,
                login_profile,
                author_id,
                author_name,
                post_id,
                source_url,
                post_date,
                now,
            ),
        )
        self.conn.commit()
        return {
            "author_id": author_id,
            "author_name": author_name,
            "post_id": post_id,
            "source_url": source_url,
            "media_path": media_path,
        }

    def record_likes_seen(
        self,
        records: Iterable[LikeSeenRecord],
        *,
        target_key: str,
        login_profile: str,
        state: str = "seen",
    ) -> dict:
        now = iso_utc(utc_now())
        unique: dict[tuple[str, int], LikeSeenRecord] = {}
        for rec in records:
            unique[(rec.post_id, rec.media_num)] = rec
        before = self.conn.execute(
            "SELECT COUNT(*) FROM likes_seen WHERE target_key=? AND login_profile=?",
            (target_key, login_profile),
        ).fetchone()[0]
        with self.conn:
            for rec in unique.values():
                self.conn.execute(
                    """INSERT INTO likes_seen(
                         target_key,login_profile,post_id,media_num,author_id,author_name,
                         post_date,extension,state,first_seen_at,last_seen_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(target_key,login_profile,post_id,media_num) DO UPDATE SET
                         author_id=excluded.author_id,
                         author_name=excluded.author_name,
                         post_date=excluded.post_date,
                         extension=excluded.extension,
                         state=CASE
                           WHEN likes_seen.state='ignored' THEN 'ignored'
                           ELSE excluded.state
                         END,
                         last_seen_at=excluded.last_seen_at""",
                    (
                        target_key, login_profile, rec.post_id, rec.media_num,
                        rec.author_id, rec.author_name, rec.post_date, rec.extension,
                        state, now, now,
                    ),
                )
        after = self.conn.execute(
            "SELECT COUNT(*) FROM likes_seen WHERE target_key=? AND login_profile=?",
            (target_key, login_profile),
        ).fetchone()[0]
        posts = self.conn.execute(
            "SELECT COUNT(DISTINCT post_id) FROM likes_seen WHERE target_key=? AND login_profile=?",
            (target_key, login_profile),
        ).fetchone()[0]
        return {"added": after - before, "media_total": after, "posts_total": posts}

    def replace_likes_anchors(
        self,
        records: Iterable[LikeSeenRecord],
        *,
        target_key: str,
        login_profile: str,
        max_posts: int = 50,
    ) -> dict:
        """Atomically replace the moving Likes anchor window."""
        max_posts = max(1, int(max_posts))
        ordered: list[LikeSeenRecord] = []
        seen: set[str] = set()
        for rec in records:
            if rec.post_id in seen:
                continue
            seen.add(rec.post_id)
            ordered.append(rec)
            if len(ordered) >= max_posts:
                break
        now = iso_utc(utc_now())
        with self.conn:
            self.conn.execute(
                "DELETE FROM likes_anchors WHERE target_key=? AND login_profile=?",
                (target_key, login_profile),
            )
            for rank, rec in enumerate(ordered, 1):
                self.conn.execute(
                    """INSERT INTO likes_anchors(
                         target_key,login_profile,post_id,rank,author_id,author_name,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (target_key, login_profile, rec.post_id, rank,
                     rec.author_id, rec.author_name, now),
                )
        return {"posts": len(ordered), "post_ids": [r.post_id for r in ordered]}

    def likes_anchor_records(
        self, *, target_key: str, login_profile: str
    ) -> list[LikeSeenRecord]:
        rows = self.conn.execute(
            """SELECT post_id, author_id, author_name
               FROM likes_anchors
               WHERE target_key=? AND login_profile=?
               ORDER BY rank""",
            (target_key, login_profile),
        ).fetchall()
        return [
            LikeSeenRecord(str(post_id), 1, str(author_id or ""), str(author_name or ""))
            for post_id, author_id, author_name in rows
        ]

    def likes_anchor_ids(self, *, target_key: str, login_profile: str) -> list[str]:
        return [
            rec.post_id for rec in self.likes_anchor_records(
                target_key=target_key, login_profile=login_profile
            )
        ]

    def likes_anchor_count(self, *, target_key: str, login_profile: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM likes_anchors WHERE target_key=? AND login_profile=?",
            (target_key, login_profile),
        ).fetchone()
        return int(row[0] or 0)

    def rotate_likes_anchors(
        self,
        new_records: Iterable[LikeSeenRecord],
        *,
        target_key: str,
        login_profile: str,
        max_posts: int = 50,
    ) -> dict:
        """Move the boundary forward after a successful incremental download."""
        existing = self.likes_anchor_records(
            target_key=target_key, login_profile=login_profile
        )
        combined = list(new_records) + existing
        return self.replace_likes_anchors(
            combined,
            target_key=target_key,
            login_profile=login_profile,
            max_posts=max_posts,
        )

    def likes_seen_counts(self, *, target_key: str, login_profile: str) -> tuple[int, int]:
        row = self.conn.execute(
            """SELECT COUNT(*), COUNT(DISTINCT post_id)
               FROM likes_seen WHERE target_key=? AND login_profile=?""",
            (target_key, login_profile),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    # Kept for backward compatibility with v0.1.3's existing catalog. New
    # downloads no longer create sidecar JSON files.
    def upsert_metadata(
        self,
        metadata_path: Path,
        *,
        platform: str,
        target_key: str,
        login_profile: str,
    ) -> Optional[dict]:
        try:
            data = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        except Exception:
            return None
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        author_id = str(author.get("id") or "")
        author_name = str(author.get("name") or "")
        post_id = str(data.get("tweet_id") or data.get("id") or data.get("illust_id") or "")
        post_date = str(data.get("date") or "")
        if platform == "x" and author_name and post_id:
            source_url = f"https://x.com/{author_name}/status/{post_id}"
        else:
            source_url = str(data.get("url") or data.get("webpage_url") or "")
        p = Path(metadata_path)
        media_path = str(p.with_suffix("")) if p.name.lower().endswith(".json") else ""
        now = iso_utc(utc_now())
        self.conn.execute(
            """INSERT INTO media(metadata_path, media_path, platform, target_key, login_profile,
                   author_id, author_name, post_id, source_url, post_date, indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(metadata_path) DO UPDATE SET
                 media_path=excluded.media_path, platform=excluded.platform,
                 target_key=excluded.target_key, login_profile=excluded.login_profile,
                 author_id=excluded.author_id, author_name=excluded.author_name,
                 post_id=excluded.post_id, source_url=excluded.source_url,
                 post_date=excluded.post_date, indexed_at=excluded.indexed_at""",
            (
                str(p), media_path, platform, target_key, login_profile,
                author_id, author_name, post_id, source_url, post_date, now,
            ),
        )
        self.conn.commit()
        return {
            "author_id": author_id,
            "author_name": author_name,
            "post_id": post_id,
            "source_url": source_url,
            "media_path": media_path,
        }

    def record_recent_file(
        self, path: Path | str, *, platform: str = "", target_key: str = "",
        login_profile: str = "", target_name: str = "",
    ) -> None:
        p = str(Path(path))
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.conn.execute(
            """INSERT INTO recent_files(path,platform,target_key,login_profile,target_name,detected_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 platform=excluded.platform, target_key=excluded.target_key,
                 login_profile=excluded.login_profile, target_name=excluded.target_name,
                 detected_at=excluded.detected_at""",
            (p, platform, target_key, login_profile, target_name, now),
        )
        self.conn.commit()

    def recent_files(self, limit: int = 24) -> list[dict]:
        rows = self.conn.execute(
            """SELECT r.path,r.platform,r.target_key,r.login_profile,r.target_name,r.detected_at,
                      COALESCE(m.author_name,''),COALESCE(m.author_id,''),
                      COALESCE(m.post_id,''),COALESCE(m.source_url,'')
               FROM recent_files r
               LEFT JOIN media m ON m.media_path=r.path
               ORDER BY r.detected_at DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for row in rows:
            item = {
                "path": row[0], "platform": row[1] or "", "target_key": row[2] or "",
                "login_profile": row[3] or "", "target_name": row[4] or "",
                "detected_at": row[5] or "", "author_name": row[6] or "",
                "author_id": row[7] or "", "post_id": row[8] or "",
                "source_url": row[9] or "",
            }
            # Files imported from Hitomi may have been added to recent_files before
            # internal metadata was known. The filename still contains Tweet ID,
            # so use it as a fallback to enrich the card with author/source URL.
            if item["platform"] == "x" and not item["source_url"]:
                rec = parse_hitomi_x_filename(Path(item["path"]))
                if rec:
                    meta = self.conn.execute(
                        """SELECT author_name,author_id,post_id,source_url
                           FROM media WHERE platform='x' AND post_id=?
                           ORDER BY indexed_at DESC LIMIT 1""",
                        (rec.tweet_id,),
                    ).fetchone()
                    if meta:
                        item["author_name"] = meta[0] or item["author_name"]
                        item["author_id"] = meta[1] or item["author_id"]
                        item["post_id"] = meta[2] or item["post_id"]
                        item["source_url"] = meta[3] or item["source_url"]
            out.append(item)
        return out

    def recent_file_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM recent_files").fetchone()
        return int(row[0] or 0)

    def export_authors_csv(self, path: Path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.conn.execute(
            """SELECT platform, author_name, author_id, COUNT(*) AS media_count, MAX(indexed_at)
               FROM media
               WHERE author_name<>'' OR author_id<>''
               GROUP BY platform, author_name, author_id
               ORDER BY platform, lower(author_name), author_id"""
        ).fetchall()
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "platform", "account_name", "account_id", "media_records", "last_seen"
            ])
            w.writerows(rows)
        return len(rows)

    def index_existing_folder(self, root: Path, *, hash_files: bool = False) -> int:
        root = Path(root)
        count = 0
        now = iso_utc(utc_now())
        if not root.exists():
            return 0
        with self.conn:
            for p in root.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                digest = ""
                if hash_files:
                    h = hashlib.sha256()
                    try:
                        with p.open("rb") as f:
                            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                                h.update(chunk)
                        digest = h.hexdigest()
                    except OSError:
                        digest = ""
                self.conn.execute(
                    """INSERT INTO existing_files(path,size,mtime_ns,sha256,indexed_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
                         sha256=CASE WHEN excluded.sha256<>'' THEN excluded.sha256 ELSE existing_files.sha256 END,
                         indexed_at=excluded.indexed_at""",
                    (str(p), st.st_size, st.st_mtime_ns, digest, now),
                )
                count += 1
        return count
