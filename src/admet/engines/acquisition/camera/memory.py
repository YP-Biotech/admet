"""How much memory the PC has, without a dependency for it: what a recording may hold before the disk takes it."""

from __future__ import annotations


def memory_gb() -> tuple[float, float | None]:
    """The PC's memory, total and free, in GB; free is None where the system does not say."""
    import os
    import sys

    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("free", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                        ("page_free", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                        ("virtual_free", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]

        status = Status()
        status.length = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))     # type: ignore[attr-defined]
        return status.total / 2**30, status.free / 2**30
    page = os.sysconf("SC_PAGE_SIZE")
    total = os.sysconf("SC_PHYS_PAGES") * page / 2**30
    try:
        free = os.sysconf("SC_AVPHYS_PAGES") * page / 2**30
    except (ValueError, OSError):
        free = None
    return total, free
