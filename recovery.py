from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


STATE_FILES = (
    "accounts.json",
    "collections.json",
    "targets.json",
    "catalog.sqlite3",
    "catalog.sqlite3-wal",
    "catalog.sqlite3-shm",
)
STATE_DIRS = ("auth", "archives")
HISTORY_FILE = "recovery-history.json"


@dataclass(frozen=True)
class RecoveryCandidate:
    path: Path
    candidate_id: str
    latest_mtime: float
    account_count: int
    collection_count: int


@dataclass(frozen=True)
class RecoveryReport:
    source: Path
    backup: Path
    state_files: int
    state_directories: int
    media_files: int
    media_conflicts: int
    settings_path: Path | None


def _json_value(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _candidate_details(path: Path) -> RecoveryCandidate | None:
    accounts = _json_value(path / "accounts.json")
    if not isinstance(accounts, list):
        return None
    valid_accounts = [
        item for item in accounts
        if isinstance(item, dict) and item.get("profile_id") and item.get("name")
    ]
    if not valid_accounts:
        return None

    raw_collections = _json_value(path / "collections.json")
    if isinstance(raw_collections, dict):
        collections = raw_collections.get("items", [])
    elif isinstance(raw_collections, list):
        collections = raw_collections
    else:
        collections = []
    valid_collections = [x for x in collections if isinstance(x, dict) and x.get("collection_id")]

    tracked = [path / name for name in STATE_FILES]
    tracked.extend(path / name for name in STATE_DIRS)
    tracked.append(path / "settings.ini")
    stats = [(item.stat().st_mtime_ns, item.stat().st_size if item.is_file() else 0)
             for item in tracked if item.exists()]
    if not stats:
        return None
    latest_ns = max(value[0] for value in stats)
    fingerprint = json.dumps(
        [str(path.resolve()), len(valid_accounts), len(valid_collections), sorted(stats)],
        ensure_ascii=False,
    ).encode("utf-8")
    return RecoveryCandidate(
        path=path,
        candidate_id=hashlib.sha256(fingerprint).hexdigest(),
        latest_mtime=latest_ns / 1_000_000_000,
        account_count=len(valid_accounts),
        collection_count=len(valid_collections),
    )


def _recovered_ids(data_dir: Path) -> set[str]:
    raw = _json_value(data_dir / HISTORY_FILE)
    values = raw.get("recoveries", []) if isinstance(raw, dict) else []
    return {str(x.get("candidate_id")) for x in values if isinstance(x, dict)}


def find_recovery_candidate(data_dir: Path, temp_root: Path | None = None) -> RecoveryCandidate | None:
    """Find the newest real user profile stranded in one of our smoke folders."""
    data_dir = Path(data_dir)
    temp_root = Path(temp_root or tempfile.gettempdir())
    recovered = _recovered_ids(data_dir)
    candidates: list[RecoveryCandidate] = []
    try:
        paths = temp_root.glob("smc-smoke-*")
        for path in paths:
            if not path.is_dir() or path.resolve() == data_dir.resolve():
                continue
            item = _candidate_details(path)
            if item and item.candidate_id not in recovered:
                candidates.append(item)
    except OSError:
        return None
    return max(candidates, key=lambda x: x.latest_mtime, default=None)


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _copy_tree(source: Path, destination: Path, *, overwrite: bool) -> tuple[int, int]:
    copied = conflicts = 0
    if not source.is_dir():
        return copied, conflicts
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        target = destination / relative
        if target.exists() and not overwrite:
            conflicts += 1
            continue
        _copy_file_atomic(item, target)
        copied += 1
    return copied, conflicts


def restore_candidate(candidate: RecoveryCandidate, data_dir: Path) -> RecoveryReport:
    """Restore one coherent profile without deleting the source or existing media."""
    source = candidate.path
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    backup = data_dir / "recovery-backups" / (
        datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    backup.mkdir(parents=True, exist_ok=False)

    for name in STATE_FILES:
        current = data_dir / name
        if current.is_file():
            _copy_file_atomic(current, backup / name)
    for name in STATE_DIRS:
        current = data_dir / name
        if current.is_dir():
            _copy_tree(current, backup / name, overwrite=True)

    try:
        state_files = 0
        for name in STATE_FILES:
            stranded = source / name
            if name == "catalog.sqlite3-shm":
                # SQLite safely recreates shared-memory state from the database/WAL.
                (data_dir / name).unlink(missing_ok=True)
            elif stranded.is_file():
                _copy_file_atomic(stranded, data_dir / name)
                state_files += 1
            elif name == "catalog.sqlite3-wal":
                # A journal from the previously active catalog must never be paired
                # with the recovered main database.
                (data_dir / name).unlink(missing_ok=True)

        state_directories = 0
        for name in STATE_DIRS:
            stranded = source / name
            if stranded.is_dir():
                _copy_tree(stranded, data_dir / name, overwrite=True)
                state_directories += 1

        media_files, media_conflicts = _copy_tree(
            source / "Library", data_dir / "Library", overwrite=False,
        )

        history_path = data_dir / HISTORY_FILE
        raw_history = _json_value(history_path)
        history = raw_history if isinstance(raw_history, dict) else {"version": 1, "recoveries": []}
        values = history.setdefault("recoveries", [])
        values.append({
            "candidate_id": candidate.candidate_id,
            "source": str(source),
            "backup": str(backup),
            "recovered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        _copy_file_atomic_from_text(history_path, json.dumps(history, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        # Return state/auth/archive to the exact pre-recovery snapshot. Media is
        # never overwritten, so successfully copied media may safely remain.
        for name in STATE_FILES:
            current = data_dir / name
            current.unlink(missing_ok=True)
            saved = backup / name
            if saved.is_file():
                _copy_file_atomic(saved, current)
        for name in STATE_DIRS:
            current = data_dir / name
            if current.is_symlink() or current.is_file():
                current.unlink(missing_ok=True)
            elif current.is_dir():
                shutil.rmtree(current)
            saved = backup / name
            if saved.is_dir():
                _copy_tree(saved, current, overwrite=True)
        raise
    settings_path = source / "settings.ini"
    return RecoveryReport(
        source=source,
        backup=backup,
        state_files=state_files,
        state_directories=state_directories,
        media_files=media_files,
        media_conflicts=media_conflicts,
        settings_path=settings_path if settings_path.is_file() else None,
    )


def _copy_file_atomic_from_text(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
