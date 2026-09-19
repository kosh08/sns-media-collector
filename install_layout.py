from __future__ import annotations

from pathlib import Path
import re
import shutil


APP_EXE = "SNSMediaCollector.exe"
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def version_key(name: str) -> tuple[int, int, int] | None:
    match = VERSION_PATTERN.fullmatch(name.strip())
    return tuple(map(int, match.groups())) if match else None


def installed_versions(versions_root: Path) -> list[Path]:
    root = Path(versions_root)
    if not root.is_dir():
        return []
    candidates = []
    for child in root.iterdir():
        key = version_key(child.name)
        if key is None or child.is_symlink() or not child.is_dir():
            continue
        if not (child / APP_EXE).is_file():
            continue
        candidates.append((key, child))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates]


def latest_executable(versions_root: Path) -> Path | None:
    versions = installed_versions(versions_root)
    return versions[0] / APP_EXE if versions else None


def cleanup_old_versions(
    versions_root: Path, current_version: str, keep_previous: int = 1
) -> dict[str, list[str]]:
    """Keep the current version and a small rollback window.

    Only immediate, numeric version directories containing the application EXE
    are eligible. This deliberately cannot touch user data or arbitrary folders.
    """
    root = Path(versions_root).resolve()
    if root.name.casefold() != "versions" or version_key(current_version) is None:
        raise ValueError("Refusing to clean an invalid versions directory")
    current = root / current_version
    if current.parent != root or not (current / APP_EXE).is_file():
        raise ValueError("The installed current version was not found")

    versions = installed_versions(root)
    previous = [path for path in versions if path != current][:max(0, keep_previous)]
    keep = {current, *previous}
    result: dict[str, list[str]] = {
        "kept": [path.name for path in versions if path in keep],
        "removed": [],
        "failed": [],
    }
    for path in versions:
        if path in keep:
            continue
        try:
            shutil.rmtree(path)
            result["removed"].append(path.name)
        except OSError:
            result["failed"].append(path.name)
    return result
