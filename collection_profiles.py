from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from core import atomic_write_json


@dataclass
class CollectionProfile:
    """A reusable acquisition recipe, deliberately separate from authentication."""

    name: str
    account_id: str
    platform: str
    source: str = "media"  # posts | media | likes | bookmarks
    target_scope: str = "self"  # self | other
    target_value: str = ""
    content_mode: str = "images"  # images | text_images | text
    review_mode: str = "auto"  # auto | inbox
    destination: str = ""
    text_images_destination: str = ""
    text_destination: str = ""
    range_mode: str = "incremental"
    date_after: str = ""
    collection_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_dict(cls, value: dict) -> "CollectionProfile":
        fields = cls.__dataclass_fields__
        payload = {k: v for k, v in value.items() if k in fields}
        # v1 stored one media destination plus one text destination. Preserve
        # the media location for the new combined-content mode on upgrade.
        if "text_images_destination" not in value:
            payload["text_images_destination"] = str(value.get("destination") or "")
        return cls(**payload)

    def validate(self) -> None:
        if self.platform not in {"x", "pixiv"}:
            raise ValueError("取得元はXまたはpixivを選択してください。")
        if self.source not in {"posts", "media", "likes", "bookmarks"}:
            raise ValueError("取得用途が不正です。")
        if self.platform == "pixiv" and self.source == "bookmarks":
            self.source = "likes"
        if self.target_scope not in {"self", "other"}:
            raise ValueError("取得対象の指定が不正です。")
        if self.target_scope == "other" and not self.target_value.strip():
            raise ValueError("他ユーザーを取得する場合はURL・ユーザー名・IDが必要です。")
        if self.content_mode not in {"images", "text_images", "text"}:
            raise ValueError("保存内容の指定が不正です。")
        if self.platform == "pixiv" and self.content_mode != "images":
            self.content_mode = "images"
        if self.review_mode not in {"auto", "inbox"}:
            raise ValueError("振り分け方法の指定が不正です。")


class CollectionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: list[CollectionProfile] = []
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            values = raw.get("items", []) if isinstance(raw, dict) else raw
            self.items = [CollectionProfile.from_dict(x) for x in values if isinstance(x, dict)]
        except Exception:
            self.items = []

    def save(self) -> None:
        atomic_write_json(self.path, {"version": 2, "items": [asdict(x) for x in self.items]})

    def get(self, collection_id: str) -> Optional[CollectionProfile]:
        return next((x for x in self.items if x.collection_id == collection_id), None)

    def upsert(self, item: CollectionProfile) -> CollectionProfile:
        item.validate()
        existing = next((i for i, x in enumerate(self.items) if x.collection_id == item.collection_id), -1)
        if existing >= 0:
            self.items[existing] = item
        else:
            self.items.append(item)
        self.save()
        return item

    def delete(self, collection_id: str) -> bool:
        before = len(self.items)
        self.items = [x for x in self.items if x.collection_id != collection_id]
        if len(self.items) != before:
            self.save()
            return True
        return False

    def reorder(self, ordered_ids: Iterable[str]) -> None:
        ids = [str(x) for x in ordered_ids]
        by_id = {x.collection_id: x for x in self.items}
        ordered = [by_id.pop(x) for x in ids if x in by_id]
        ordered.extend(x for x in self.items if x.collection_id in by_id)
        self.items = ordered
        self.save()

    def migrate_account_sessions(self, accounts: Iterable, sessions: dict) -> int:
        """Create one recipe per legacy account once; never overwrite user recipes."""
        if self.items:
            return 0
        for account in accounts:
            state = sessions.get(getattr(account, "profile_id", ""), {})
            platform = getattr(account, "platform", "x")
            source = str(state.get("target_type") or ("posts" if platform == "pixiv" else "media"))
            target_value = str(state.get("target") or "")
            scope = "other" if target_value else "self"
            if platform == "x" and target_value:
                match = re.search(r"(?:x\.com|twitter\.com)/([^/?#]+)", target_value, re.I)
                handle = (match.group(1) if match else target_value.lstrip("@")).strip()
                known_names = {
                    str(getattr(account, "name", "")).strip().lower(),
                    str(getattr(account, "username", "")).strip().lstrip("@").lower(),
                }
                if handle.lower() in known_names:
                    scope, target_value = "self", ""
            label = {"posts": "投稿", "media": "メディア", "likes": "いいね・ブックマーク"}.get(source, source)
            self.items.append(CollectionProfile(
                name=f"{getattr(account, 'name', platform)} / {label}",
                account_id=getattr(account, "profile_id", ""), platform=platform,
                source=source, target_scope=scope, target_value=target_value,
                destination=str(state.get("destination") or ""),
                text_images_destination=str(state.get("destination") or ""),
                range_mode=str(state.get("range_mode") or "incremental"),
                date_after=str(state.get("date_after") or ""),
            ))
        if self.items:
            self.save()
        return len(self.items)
