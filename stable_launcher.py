from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import sys

from install_layout import latest_executable


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def show_error(message: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(0, message, "SNS Media Collector", 0x10)
    else:
        print(message, file=sys.stderr)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        target = latest_executable(application_root() / "versions")
        Path(sys.argv[2]).write_text(json.dumps({
            "success": True,
            "frozen": bool(getattr(sys, "frozen", False)),
            "target": str(target) if target else None,
        }), encoding="utf-8")
        return 0

    target = latest_executable(application_root() / "versions")
    if target is None:
        show_error("インストール済みのSNS Media Collectorが見つかりません。\nセットアップを実行してください。")
        return 2
    try:
        subprocess.Popen([str(target), *sys.argv[1:]], cwd=target.parent, close_fds=True)
    except OSError as error:
        show_error(f"SNS Media Collectorを起動できませんでした。\n\n{error}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
