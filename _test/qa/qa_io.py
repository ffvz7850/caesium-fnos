#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台读取「进程累计读字节数」——用来证明扫描/解析到底还读不读盘。

无第三方依赖：Windows 走 kernel32.GetProcessIoCounters，Linux 走 /proc/<pid>/io。
取不到返回 None（调用方要能容忍）。
"""
import ctypes
import ctypes.wintypes as wt
import os


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


def _win_k32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.CloseHandle.restype = wt.BOOL
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.GetProcessIoCounters.argtypes = [wt.HANDLE, ctypes.POINTER(_IO_COUNTERS)]
    k32.GetProcessIoCounters.restype = wt.BOOL
    return k32


_K32 = _win_k32() if os.name == "nt" else None


def read_bytes(pid=None):
    """进程累计 ReadTransferCount（pid=None 表示当前进程）。"""
    if os.name == "nt":
        if _K32 is None:
            return None
        if pid is None:
            h = _K32.GetCurrentProcess()
            own = False
        else:
            h = _K32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            own = True
            if not h:
                return None
        try:
            io = _IO_COUNTERS()
            if not _K32.GetProcessIoCounters(h, ctypes.byref(io)):
                return None
            return int(io.ReadTransferCount)
        finally:
            if own:
                _K32.CloseHandle(h)
    path = "/proc/%s/io" % ("self" if pid is None else int(pid))
    try:
        with open(path, "r") as f:
            for line in f:
                if line.startswith("read_bytes:"):
                    return int(line.split(":")[1].strip())
    except OSError:
        return None
    return None


def delta_during(fn, pid=None):
    """执行 fn() 期间进程读了多少字节（读不到计数返回 None）。"""
    a = read_bytes(pid)
    fn()
    b = read_bytes(pid)
    if a is None or b is None:
        return None
    return b - a
