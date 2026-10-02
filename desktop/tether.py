"""Processes the studio starts end when the studio does, however it ends.

The model server and the servers a project runs are child processes. When the
app closes normally it stops them (app.shutdown). When it does not -- ended
from the Task Manager, a crash, the window killed -- Windows leaves the
children running: a llama-server holding seven gigabytes of memory and the
graphics card, with nothing left that knows about it, and the next start
finds less of the card free.

So on Windows each child goes into a job object that is closed when this
process ends, by any route, and the system then ends everything in it. One
job for the whole process, made the first time it is needed.

Linux has PR_SET_PDEATHSIG, but it fires when the *thread* that started the
child ends, and the model server is started from a short-lived thread: it
would kill the server a moment after starting it. There, as on macOS, a
normal close is what stops the children.
"""

from __future__ import annotations

import os
import subprocess
import threading

_job = None
_lock = threading.Lock()


def _windows_job():
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000           # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None                                           # 9: JobObjectExtendedLimitInformation
    return kernel32, job


def tie(process: subprocess.Popen) -> bool:
    """End `process` with this one. Best effort: False where it cannot be done."""
    global _job
    if os.name != "nt":
        return False
    try:
        with _lock:
            if _job is None:
                _job = _windows_job() or False
        if not _job:
            return False
        kernel32, job = _job
        return bool(kernel32.AssignProcessToJobObject(job, int(process._handle)))
    except Exception:  # noqa: BLE001 - never stop a start over this
        return False
