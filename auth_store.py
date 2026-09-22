"""Local, per-account authentication storage helpers.

Cookie values are intentionally never returned by inspection helpers or written
to application logs.  The only plaintext copy is the gallery-dl compatible file
inside the user's SNS Media Collector data directory.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterable
from urllib.parse import unquote


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


def managed_pixiv_cache_path(data_dir: Path, profile_id: str) -> Path:
    """Return the account-specific gallery-dl cache containing pixiv OAuth."""
    safe_id = "".join(ch for ch in str(profile_id) if ch.isalnum())
    if not safe_id:
        raise ValueError("認証プロファイルIDが不正です。")
    return Path(data_dir) / "auth" / "pixiv" / f"pixiv-{safe_id}.sqlite3"


def managed_pixiv_web_profile_dir(data_dir: Path, profile_id: str) -> Path:
    safe_id = "".join(ch for ch in str(profile_id) if ch.isalnum())
    if not safe_id:
        raise ValueError("認証プロファイルIDが不正です。")
    return Path(data_dir) / "auth" / "web-profiles" / f"pixiv-{safe_id}"


def inspect_pixiv_cache(path: Path) -> dict:
    """Check for a cached pixiv refresh token without reading its value."""
    path = Path(path)
    if not path.is_file():
        return {"exists": False, "authenticated": False, "error": ""}
    try:
        # The existence check above prevents accidental creation.  A plain
        # path is more reliable than SQLite URI parsing for Windows user names
        # containing spaces, '#' or non-ASCII characters.
        con = sqlite3.connect(str(path))
        try:
            row = con.execute(
                "SELECT 1 FROM data WHERE key LIKE ? LIMIT 1",
                ("gallery_dl.extractor.pixiv._refresh_token_cache-%",),
            ).fetchone()
        finally:
            con.close()
        return {"exists": True, "authenticated": bool(row), "error": ""}
    except (OSError, sqlite3.Error) as exc:
        return {"exists": True, "authenticated": False, "error": str(exc)}


def inspect_netscape_cookie_file(path: Path) -> dict:
    """Inspect names/domains only.  Cookie secrets never leave this function."""
    path = Path(path)
    names: set[str] = set()
    domains: set[str] = set()
    lines = 0
    x_user_id = ""
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
        domain, _include, _cookie_path, _secure, _expires, name, value = parts
        lines += 1
        names.add(name)
        domains.add(domain.lower())
        if name == "twid" and domain.lower().lstrip(".").endswith(("x.com", "twitter.com")):
            match = re.fullmatch(r"u=(\d+)", unquote(value))
            if match:
                x_user_id = match.group(1)
    x_domains = {x for x in domains if x.lstrip(".").endswith(("x.com", "twitter.com"))}
    return {
        "exists": True,
        "valid": lines > 0,
        "x_auth": "auth_token" in names and bool(x_domains),
        "csrf": "ct0" in names,
        "x_user_id": x_user_id,
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
    """Validate an export and write only X cookies into an account's store."""
    source = Path(source)
    result = inspect_netscape_cookie_file(source)
    if not result.get("valid"):
        raise ValueError("cookies.txtを読み取れませんでした。Netscape形式で書き出してください。")
    if not result.get("x_auth"):
        raise ValueError("このファイルにXのauth_tokenがありません。x.comで書き出してください。")
    records: list[BrowserCookie] = []
    for original in source.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = original
        if raw.startswith("#HttpOnly_"):
            raw = raw[len("#HttpOnly_"):]
        elif not raw or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 7:
            continue
        domain, _include, cookie_path, secure, expires, name, value = parts
        if not domain.lower().lstrip(".").endswith(("x.com", "twitter.com")):
            continue
        try:
            expires_at = int(expires or 0)
        except ValueError:
            expires_at = 0
        records.append(BrowserCookie(
            domain=domain,
            path=cookie_path or "/",
            secure=secure.upper() == "TRUE",
            expires=expires_at,
            name=name,
            value=value,
        ))
    return write_netscape_cookie_file(Path(destination), records)


def classify_x_auth_test(returncode: int, output: str) -> tuple[bool, str]:
    """Turn gallery-dl's auth probe result into a safe, actionable message.

    The raw output can contain local paths and extractor details, so callers
    should show this summary instead of echoing the complete process output.
    """
    text = str(output or "").lower()
    if "failed to decrypt cookie" in text or "dpapi" in text:
        return False, (
            "ブラウザCookieをWindowsで復号できませんでした。"
            "Chromeからcookies.txtを書き出し、アカウント別Cookieへ取り込んでください。"
        )
    if any(marker in text for marker in (
        "could not authenticate you", "authrequired", "authenticated cookies needed",
        "login required", "authentication required",
    )):
        return False, "XがCookieを拒否しました。Xログインを更新してください。"
    if "rate" in text and "limit" in text:
        return False, "Xのアクセス制限中です。時間を置いて再確認してください。"
    if returncode != 0 or "[twitter][error]" in text:
        return False, "X認証を確認できませんでした。ログインを更新してから再試行してください。"
    return True, "X認証OK。このアカウントで取得できます。"


def classify_pixiv_auth_test(returncode: int, output: str) -> tuple[bool, str]:
    """Summarize a pixiv gallery-dl probe without exposing its token."""
    text = str(output or "").lower()
    if "invalid refresh token" in text:
        return False, "pixivの連携情報が無効です。pixivログインを更新してください。"
    if "'refresh-token' required" in text or "authenticationerror" in text:
        return False, "pixivの連携情報がありません。pixivへログインしてください。"
    if "rate" in text and "limit" in text:
        return False, "pixivのアクセス制限中です。時間を置いて再確認してください。"
    if returncode != 0 or "[pixiv][error]" in text:
        return False, "pixiv認証を確認できませんでした。ログインを更新してから再試行してください。"
    return True, "pixiv認証OK。このアカウントで取得できます。"
