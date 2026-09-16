from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    print("SNS Media Collector full test")
    print("Python:", sys.version)

    # Syntax must stay compatible with the user's Python 3.10 runtime.
    for path in sorted(ROOT.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path), feature_version=(3, 10))
    print("Python 3.10 syntax compatibility: OK")

    suite = unittest.defaultTestLoader.loadTestsFromNames(
        ["test_core", "test_auth_store", "test_release", "test_updater"]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 11

    # If dependencies are present, also run the real startup smoke test.
    try:
        import PySide6  # noqa: F401
        import gallery_dl  # noqa: F401
    except Exception:
        print("Dependencies not installed here; startup smoke test skipped.")
        return 0

    regressions = unittest.defaultTestLoader.loadTestsFromName("test_regressions")
    if not unittest.TextTestRunner(verbosity=2).run(regressions).wasSuccessful():
        return 16
    return subprocess.call([sys.executable, str(ROOT / "smoke_test.py")], cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
