"""Local, per-account authentication storage helpers.

Cookie values are intentionally never returned by inspection helpers or written
to application logs.  The only plaintext copy is the gallery-dl compatible file
inside the user's SNS Media Collector data directory.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Iterable


@dataclass(frozen=True)
class BrowserCookie:
    domain: str
    path: str
    secure: bool
    expires: int
    name: str
    value: str


def managed_x_cookie_path(data_dir: Path, profile_id: str) -> Path:
    """Return a stable cookie path that cannot escape the authentication root."""
    safe_id = "".join(ch for ch in str(profile_id) if ch.isalnum())
    if not safe_id:
        raise ValueError("認証プロファイルIDが不正です。")
    return Path(data_dir) / "auth" / "cookies" / f"x-{safe_id}.txt"


def managed_x_web_profile_dir(data_dir: Path, profile_id: str) -> Path:
    safe_id = "".join(ch for ch in str(profile_id) if ch.isalnum())
    if not safe_id:
        raise ValueError("認証プロファイルIDが不正です。")
    return Path(data_dir) / "auth" / "web-profiles" / f"x-{safe_id}"


def inspect_netscape_cookie_file(path: Path) -> dict:
    """Inspect names/domains only.  Cookie secrets never leave this function."""
    path = Path(path)
    names: set[str] = set()
    domains: set[str] = set()
    lines = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"exists": False, "valid": False, "x_auth": False,
                "cookie_count": 0, "domains": [], "error": str(exc)}
    for raw in text.splitlines():
        if raw.startswith("#HttpOnly_"):
            raw = raw[len("#HttpOnly_"):]
        elif not raw or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 7:
            continue
        domain, _include, _cookie_path, _secure, _expires, name, _value = parts
        lines += 1
        names.add(name)
        domains.add(domain.lower())
    x_domains = {x for x in domains if x.lstrip(".").endswith(("x.com", "twitter.com"))}
    return {
        "exists": True,
        "valid": lines > 0,
        "x_auth": "auth_token" in names and bool(x_domains),
        "csrf": "ct0" in names,
        "cookie_count": lines,
        "domains": sorted(x_domains),
        "error": "",
    }


def _safe_cookie_field(value: str) -> str:
    return str(value).replace("\t", "").replace("\r", "").replace("\n", "")


def write_netscape_cookie_file(path: Path, cookies: Iterable[BrowserCookie]) -> dict:
    """Atomically write X cookies in Netscape format for gallery-dl."""
    path = Path(path)
    unique: dict[tuple[str, str, str], BrowserCookie] = {}
    for cookie in cookies:
        domain = _safe_cookie_field(cookie.domain).lower()
        if not domain.lstrip(".").endswith(("x.com", "twitter.com")):
            continue
        name = _safe_cookie_field(cookie.name)
        value = _safe_cookie_field(cookie.value)
        if not name or not value:
            continue
        normalized = BrowserCookie(
            domain=domain,
            path=_safe_cookie_field(cookie.path) or "/",
            secure=bool(cookie.secure),
            expires=max(0, int(cookie.expires or 0)),
            name=name,
            value=value,
        )
        unique[(normalized.domain, normalized.path, normalized.name)] = normalized
    if not any(cookie.name == "auth_token" for cookie in unique.values()):
        raise ValueError("Xのauth_tokenが見つかりません。Xへログインしてから保存してください。")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File", "# Managed by SNS Media Collector. Do not share this file."]
    for cookie in sorted(unique.values(), key=lambda x: (x.domain, x.path, x.name)):
        include_subdomains = "TRUE" if cookie.domain.startswith(".") else "FALSE"
        lines.append("\t".join((
            cookie.domain, include_subdomains, cookie.path,
            "TRUE" if cookie.secure else "FALSE", str(cookie.expires),
            cookie.name, cookie.value,
        )))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return inspect_netscape_cookie_file(path)


def import_netscape_cookie_file(source: Path, destination: Path) -> dict:
    """Validate and atomically copy an exported X cookie file into an account."""
    source = Path(source)
    result = inspect_netscape_cookie_file(source)
    if not result.get("valid"):
        raise ValueError("cookies.txtを読み取れませんでした。Netscape形式で書き出してください。")
    if not result.get("x_auth"):
        raise ValueError("このファイルにXのauth_tokenがありません。x.comで書き出してください。")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = source.read_bytes()
    handle, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return inspect_netscape_cookie_file(destination)
