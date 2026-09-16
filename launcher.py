from __future__ import annotations

import ctypes
import sys
import traceback
from datetime import datetime
from pathlib import Path

# Important for PyInstaller: import app normally so PyInstaller can discover and
# bundle app.py/core.py. The older runpy.run_path() launcher worked from source
# but could produce a frozen EXE without app.py inside it.


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_root()
CRASH_LOG = ROOT / "crash.log"


def show_windows_message(title: str, message: str) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
            from portable_self_test import run
            return run(sys.argv[2])
        from windows_instance import acquire
        if not acquire():
            show_windows_message("SNS Media Collector", "すでに起動しています。開いているアプリをご利用ください。")
            return 0
        from app import main as app_main
        app_main()
        return 0
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 0
    except Exception:
        text = (
            "SNS Media Collector startup crash\n"
            f"Time: {datetime.now().isoformat(timespec='seconds')}\n"
            f"Python: {sys.version}\n"
            f"Executable: {sys.executable}\n\n"
            + traceback.format_exc()
        )
        try:
            CRASH_LOG.write_text(text, encoding="utf-8")
        except Exception:
            pass
        if sys.stderr is not None:
            print(text, file=sys.stderr)
        show_windows_message(
            "SNS Media Collector - 起動エラー",
            "起動中にエラーが発生しました。\n\n"
            f"{CRASH_LOG}\n\nに詳細を保存しました。",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
