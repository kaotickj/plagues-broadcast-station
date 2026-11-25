"""Cheap content-change stamps, including Windows' actual change timestamp."""
from __future__ import annotations

import ctypes
from functools import lru_cache
import os
from pathlib import Path
import time


_WINDOWS = os.name == "nt"
_POSIX_CTIME_SETTLE_NS = 2_000_000_000


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _windows_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    return create, query, close


def _windows_change_time(path):
    """Read FILE_BASIC_INFO.ChangeTime without locking out another writer."""
    try:
        create, query, close = _windows_api()
        # FILE_READ_ATTRIBUTES, shared READ | WRITE | DELETE, OPEN_EXISTING.
        # No content-write access, privilege changes, or persistent handle.
        handle = create(str(Path(path)), 0x0080, 0x0001 | 0x0002 | 0x0004,
                        None, 3, 0, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            return None
        try:
            info = _FileBasicInfo()
            if not query(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            return info.ChangeTime if info.ChangeTime > 0 else None
        finally:
            close(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        # Unsupported filesystems/API failures must disable metadata caching.
        return None


def file_change_stamp(path, state=None):
    """Return a cache stamp, or None when callers must hash file content.

    POSIX ctime can coalesce across rapid rewrites within one filesystem clock
    tick, even when its stat field is expressed in nanoseconds. Until ctime is
    at least two seconds old, require content hashing instead of caching it.
    This settling bound assumes POSIX content-change timestamp granularity is
    no coarser than two seconds; it is not a universal inode version counter.
    Future/invalid ctime also disables reuse, including during clock rollback.
    Once settled, unchanged audio can reuse its hash without periodic full-song
    hashing. Windows keeps its native ChangeTime query: st_ctime there is creation
    time and cannot safely serve that role.
    """
    state = state if state is not None else Path(path).stat()
    stamp = (state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino)
    if not _WINDOWS:
        changed = state.st_ctime_ns
        if not isinstance(changed, int) or isinstance(changed, bool) or changed <= 0:
            return None
        if time.time_ns() - changed < _POSIX_CTIME_SETTLE_NS:
            return None
        return stamp
    changed = _windows_change_time(path)
    return stamp + (changed,) if changed is not None else None
