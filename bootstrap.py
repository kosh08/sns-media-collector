from __future__ import annotations

import os
import subprocess
import sys
import traceback
import venv
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PY = VENV / "Scripts" / "python.exe"
LOG = ROOT / "setup.log"


def write_log(text: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")


def banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)
    write_log(f"\n=== {text} ===")


def run_tee(cmd: list[str]) -> int:
    write_log("$ " + subprocess.list2cmdline(cmd))
    p = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert p.stdout is not None
    for line in p.stdout:
        print(line, end="", flush=True)
        write_log(line.rstrip("\n"))
    return p.wait()


def fail(message: str, code: int = 1) -> int:
    print(f"\n[ERROR] {message}", flush=True)
    print(f"See: {LOG}", flush=True)
    write_log(f"[ERROR] {message}")
    return code


def main() -> int:
    os.chdir(ROOT)
    LOG.write_text(
        f"SNS Media Collector setup log\nTime: {datetime.now().isoformat(timespec='seconds')}\n"
        f"Bootstrap Python: {sys.version}\nExecutable: {sys.executable}\n",
        encoding="utf-8",
    )

    if sys.version_info < (3, 10):
        return fail("Python 3.10 or newer is required.", 9)

    banner("1/5 Create virtual environment")
    if not VENV_PY.exists():
        try:
            venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
        except Exception:
            write_log(traceback.format_exc())
            return fail("Could not create .venv")
    else:
        print("Existing .venv found.")
        write_log("Existing .venv found.")

    banner("2/5 Upgrade pip")
    rc = run_tee([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"])
    if rc:
        return fail(f"pip upgrade failed (code {rc})", rc)

    banner("3/5 Install PySide6 and gallery-dl")
    rc = run_tee([str(VENV_PY), "-m", "pip", "install", "--upgrade", "-r", str(ROOT / "requirements.txt")])
    if rc:
        return fail(f"dependency install failed (code {rc})", rc)

    banner("4/5 Run startup smoke test")
    rc = run_tee([str(VENV_PY), str(ROOT / "smoke_test.py")])
    if rc:
        return fail(f"smoke test failed (code {rc})", rc)

    banner("5/5 Launch GUI")
    print("Keep this console open while testing. If the GUI crashes, crash.log will be created.", flush=True)
    write_log("Launching launcher.py")
    rc = subprocess.call([str(VENV_PY), str(ROOT / "launcher.py")], cwd=str(ROOT))
    if rc:
        return fail(f"application exited with code {rc}", rc)

    print("\nApplication closed normally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
