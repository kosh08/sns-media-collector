from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import urllib.request

RELEASE_API = "https://api.github.com/repos/kosh08/sns-media-collector/releases/latest"
USER_AGENT = "SNSMediaCollector-Updater"


def version_tuple(value: str) -> tuple[int, int, int]:
    text = str(value).strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise ValueError(f"invalid release version: {value}")
    return tuple(int(part) for part in text.split("."))


def select_update(release: dict, current_version: str) -> dict | None:
    if release.get("draft") or release.get("prerelease"):
        return None
    version = str(release.get("tag_name") or "").lstrip("v")
    if version_tuple(version) <= version_tuple(current_version):
        return None
    wanted = f"SNSMediaCollector-Setup-{version}.exe"
    for asset in release.get("assets") or []:
        if asset.get("name") != wanted:
            continue
        digest = str(asset.get("digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise ValueError("更新ファイルのSHA-256が公開情報にありません")
        url = str(asset.get("browser_download_url") or "")
        if not url.startswith("https://github.com/"):
            raise ValueError("更新ファイルのURLが不正です")
        return {
            "version": version,
            "name": wanted,
            "url": url,
            "sha256": digest.split(":", 1)[1].lower(),
            "notes": str(release.get("body") or "").strip(),
        }
    raise ValueError(f"更新ファイル {wanted} が見つかりません")


def fetch_latest_update(current_version: str) -> dict | None:
    request = urllib.request.Request(
        RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        release = json.load(response)
    return select_update(release, current_version)


def download_update(info: dict, destination: Path, progress=None) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(info["url"], headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                digest.update(block)
                received += len(block)
                if progress and total:
                    progress(min(100, int(received * 100 / total)))
        if digest.hexdigest().lower() != str(info["sha256"]).lower():
            raise ValueError("更新ファイルのSHA-256が一致しません")
        partial.replace(destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise
