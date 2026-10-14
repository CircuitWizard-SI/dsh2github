"""Report actual listener health, never a stale success snapshot."""
import ctypes
import os
import time
from .storage import read_json


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def listener_status(settings):
    row = read_json(settings.runtime_dir / "watch-status.json", {})
    alive = process_alive(row.get("pid"))
    age = max(0, time.time() - row.get("time", 0))
    fresh = age <= max(90, settings.poll_seconds * 4)
    return {"running": alive, "fresh": fresh, "ready": alive and fresh and row.get("ok") is True,
        "pid": row.get("pid"), "age_seconds": round(age, 1) if row else None,
        "error": row.get("error"), "jobs": row.get("jobs", 0)}
