from __future__ import annotations
import importlib
import platform
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def main() -> int:
    print("SNS Media Collector diagnostics")
    print("Python:", sys.version)
    print("Executable:", sys.executable)
    print("Platform:", platform.platform())
    print("Arch:", struct.calcsize("P") * 8, "bit")
    print("Folder:", ROOT)
    print()
    try:
        pyside = importlib.import_module("PySide6")
        gallery = importlib.import_module("gallery_dl")
        print("PySide6:", getattr(pyside, "__version__", "unknown"))
        print("gallery-dl module: OK")
        from PySide6.QtWidgets import QApplication
        app = QApplication([])
        print("Qt QApplication: OK")
        app.quit()
    except Exception as exc:
        print("IMPORT/Qt ERROR:", repr(exc))
        return 1
    print()
    rc = subprocess.call([sys.executable, "-m", "gallery_dl", "--version"])
    print("gallery-dl CLI exit code:", rc)
    return rc

if __name__ == "__main__":
    raise SystemExit(main())
