"""Stable Windows taskbar identity across versioned application installs.

Uses the Shell's documented AppUserModel properties, without editing the user's
pinned shortcuts. https://learn.microsoft.com/en-us/windows/win32/shell/appids
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from pathlib import Path
import subprocess
import sys
import uuid

from install_layout import APP_EXE, version_key

APP_ID = "MasterTools.SNSMediaCollector.Desktop"
DISPLAY_NAME = "SNS Media Collector"
PROPERTY_IDS = {"command": 2, "icon": 3, "name": 4, "id": 5}


class GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]


class _VariantValue(ctypes.Union):
    _fields_ = [("pointer", ctypes.c_void_p), ("storage", ctypes.c_void_p * 2)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3),
                ("value", _VariantValue)]


IID_STORE = GUID.parse("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
KEY_GUID = GUID.parse("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3")
LINK_GUID = GUID.parse("b9b4b3fc-2b51-4a42-b5d8-324146afcf25")


def _check(result):
    if result < 0:
        raise OSError(f"Windows Shell operation failed (0x{result & 0xffffffff:08x})")


def _method(store, index, result_type, *arg_types):
    table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *arg_types)(table[index])


@contextmanager
def _property_store(hwnd=None, path=None, flags=0):
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = ctypes.c_int32
    initialized = ole.CoInitializeEx(None, 2)
    if initialized < 0 and initialized != -2147417850:
        _check(initialized)
    shell = ctypes.WinDLL("shell32")
    store = ctypes.c_void_p()
    try:
        if hwnd is not None:
            get = shell.SHGetPropertyStoreForWindow
            get.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
            get.restype = ctypes.c_int32
            _check(get(hwnd, ctypes.byref(IID_STORE), ctypes.byref(store)))
        else:
            get = shell.SHGetPropertyStoreFromParsingName
            get.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32,
                            ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
            get.restype = ctypes.c_int32
            _check(get(str(path), None, flags, ctypes.byref(IID_STORE), ctypes.byref(store)))
        yield store
    finally:
        if store:
            _method(store, 2, ctypes.c_uint32)(store)
        if initialized >= 0:
            ole.CoUninitialize()


def _write(store, pid, text):
    key = PROPERTYKEY(KEY_GUID, pid)
    value = PROPVARIANT()
    # SetValue copies the borrowed Python buffer. Only COM-returned variants
    # should be passed to PropVariantClear; this buffer is owned by Python.
    buffer = ctypes.create_unicode_buffer(text) if text is not None else None
    if buffer is not None:
        value.vt = 31  # VT_LPWSTR; a zero variant removes the window property.
        value.value.pointer = ctypes.cast(buffer, ctypes.c_void_p).value
    set_value = _method(store, 6, ctypes.c_int32, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    _check(set_value(store, ctypes.byref(key), ctypes.byref(value)))


def _read(store, pid, fmtid=KEY_GUID):
    key = PROPERTYKEY(fmtid, pid)
    value = PROPVARIANT()
    get_value = _method(store, 5, ctypes.c_int32, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    _check(get_value(store, ctypes.byref(key), ctypes.byref(value)))
    try:
        return ctypes.wstring_at(value.value.pointer) if value.vt == 31 and value.value.pointer else ""
    finally:
        clear = ctypes.WinDLL("ole32").PropVariantClear
        clear.argtypes = [ctypes.POINTER(PROPVARIANT)]
        clear.restype = ctypes.c_int32
        _check(clear(ctypes.byref(value)))


def set_process_identity():
    if sys.platform != "win32":
        return
    set_id = ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
    set_id.argtypes = [ctypes.c_wchar_p]
    set_id.restype = ctypes.c_int32
    _check(set_id(APP_ID))


def process_identity():
    shell = ctypes.WinDLL("shell32")
    get_id = shell.GetCurrentProcessExplicitAppUserModelID
    get_id.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    get_id.restype = ctypes.c_int32
    value = ctypes.c_void_p()
    _check(get_id(ctypes.byref(value)))
    try:
        return ctypes.wstring_at(value)
    finally:
        free = ctypes.WinDLL("ole32").CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        free(value)


def relaunch_executable(executable: Path) -> Path | None:
    target = Path(executable).resolve()
    if target.name.casefold() != APP_EXE.casefold():
        return None
    if target.parent.parent.name.casefold() == "versions" and version_key(target.parent.name):
        stable = target.parent.parent.parent / APP_EXE
        return stable if stable.is_file() else None
    return target if target.is_file() else None


def window_values(executable: Path) -> dict[str, str]:
    stable = relaunch_executable(executable)
    if stable is None:
        return {"id": APP_ID}
    return {"command": subprocess.list2cmdline([str(stable)]),
            "icon": f"{stable},0", "name": DISPLAY_NAME, "id": APP_ID}


def configure_window(hwnd: int, executable: Path):
    if sys.platform != "win32":
        return
    with _property_store(hwnd=hwnd) as store:
        # Relaunch properties precede ID so the Shell sees a complete identity.
        try:
            for name, text in window_values(executable).items():
                _write(store, PROPERTY_IDS[name], text)
        except Exception:
            for pid in PROPERTY_IDS.values():
                _write(store, pid, None)
            raise


def clear_window(hwnd: int):
    if sys.platform == "win32":
        with _property_store(hwnd=hwnd) as store:
            for pid in PROPERTY_IDS.values():
                _write(store, pid, None)


def read_window(hwnd: int):
    with _property_store(hwnd=hwnd) as store:
        return {name: _read(store, pid) for name, pid in PROPERTY_IDS.items()}


def shortcut_app_id(path: Path) -> str:
    with _property_store(path=path) as store:
        return _read(store, PROPERTY_IDS["id"])


def shortcut_metadata(path: Path) -> dict[str, str]:
    with _property_store(path=path) as store:
        return {"id": _read(store, PROPERTY_IDS["id"]), "target": _read(store, 2, LINK_GUID)}


def native_self_test(executable: Path) -> dict:
    """Read real Shell properties on an invisible window; never create a pin."""
    if sys.platform != "win32":
        return {"skipped": True}
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = ctypes.c_int32
    initialized = ole.CoInitializeEx(None, 2)  # apartment-threaded
    if initialized < 0 and initialized != -2147417850:  # existing different apartment
        _check(initialized)
    user = ctypes.WinDLL("user32", use_last_error=True)
    create = user.CreateWindowExW
    create.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    user.DestroyWindow.argtypes = [ctypes.c_void_p]
    user.DestroyWindow.restype = ctypes.c_int
    hwnd = create(0, "STATIC", "SNS taskbar self-test", 0, 0, 0, 1, 1, None, None, None, None)
    try:
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        set_process_identity()
        if process_identity() != APP_ID:
            raise RuntimeError("Process taskbar identity did not persist")
        configure_window(hwnd, executable)
        values = read_window(hwnd)
        expected = {name: window_values(executable).get(name, "") for name in PROPERTY_IDS}
        if values != expected:
            raise RuntimeError("Taskbar identity differs from stable launcher")
        clear_window(hwnd)
        if any(read_window(hwnd).values()):
            raise RuntimeError("Taskbar window properties were not cleared")
        return values
    finally:
        if hwnd:
            clear_window(hwnd)
            user.DestroyWindow(hwnd)
        if initialized >= 0:
            ole.CoUninitialize()
