"""Windows worker descendants die with their Job Object; no extra dependency."""
from __future__ import annotations


def attach_job(process, settings):
    import ctypes
    from ctypes import wintypes
    from ai_apitest.execution.isolation import positive

    size_t = ctypes.c_size_t

    class BasicLimits(ctypes.Structure):
        _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64), ("Flags", wintypes.DWORD), ("MinimumWorkingSet", size_t), ("MaximumWorkingSet", size_t), ("ActiveProcesses", wintypes.DWORD), ("Affinity", size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperations", "WriteOperations", "OtherOperations", "ReadBytes", "WriteBytes", "OtherBytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("Basic", BasicLimits), ("IO", IoCounters), ("ProcessMemory", size_t), ("JobMemory", size_t), ("PeakProcessMemory", size_t), ("PeakJobMemory", size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.Basic.Flags = 0x2000 | 0x200 | 0x4 | 0x8  # kill on close, job memory/time, active processes
    limits.JobMemory = int(positive(settings, "memory_mb", 512)) * 1024 * 1024
    limits.Basic.JobTime = int(positive(settings, "hard_timeout_seconds", 300) * 10_000_000)
    limits.Basic.ActiveProcesses = int(positive(settings, "pids_limit", 64))
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(handle)
        raise error
    return lambda: kernel.CloseHandle(handle)
