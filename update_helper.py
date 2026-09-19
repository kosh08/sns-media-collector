from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import sys
import time

from install_layout import cleanup_old_versions


def wait_for_process(pid: int, timeout_ms: int = 120000) -> None:
    if sys.platform != "win32":
        return
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if handle:
        try:
            kernel32.WaitForSingleObject(handle, timeout_ms)
        finally:
            kernel32.CloseHandle(handle)
    else:
        time.sleep(2)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        Path(sys.argv[2]).write_text(json.dumps({"success": True}), encoding="utf-8")
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "--cleanup-versions":
        try:
            cleanup_old_versions(Path(sys.argv[2]), sys.argv[3], keep_previous=1)
        except (OSError, ValueError):
            # Cleanup is best-effort. A locked rollback version must never make an
            # otherwise successful install unusable.
            pass
        return 0
    if len(sys.argv) != 4:
        return 2
    pid = int(sys.argv[1])
    installer = Path(sys.argv[2])
    target = Path(sys.argv[3])
    if not installer.is_file():
        return 3
    wait_for_process(pid)
    completed = subprocess.run([
        str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=",
    ], timeout=600)
    if completed.returncode != 0 or not target.is_file():
        return 4
    subprocess.Popen([str(target)], close_fds=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
