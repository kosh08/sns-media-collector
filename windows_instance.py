"""The installer checks this mutex before updating/uninstalling a running app."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import sys

MUTEX_NAME = r'Local\SNSMediaCollectorDesktop'
_handle = None


def acquire() -> bool:
    global _handle
    if sys.platform != 'win32':
        return True
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        return False
    # Keep the handle until process exit, including Qt shutdown.
    _handle = handle
    return True
