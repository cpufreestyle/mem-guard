# -*- coding: utf-8 -*-
"""
Win32 / Native API 绑定：内存查询、权限、单实例、底层清理调用。

只依赖标准库（ctypes）+ config，不包含业务逻辑：
    - 进程遍历 / 清理编排见 clean.py
    - 托盘 / 菜单见 tray.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes

# ---------------------------------------------------------------- Win32 句柄

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---------------------------------------------------------------- Win32 类型声明（统一）
# 统一声明所有用到的 Win32 函数的参数/返回类型。64 位 Python 下若不声明，
# HANDLE 与结构体指针会被 ctypes 默认按 32 位 int 处理而截断，导致调用静默失败。
kernel32.GetCurrentProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

kernel32.SetSystemFileCacheSize.argtypes = [ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
kernel32.SetSystemFileCacheSize.restype = wintypes.BOOL

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
psapi.EmptyWorkingSet.restype = wintypes.BOOL

user32.MessageBoxW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int

kernel32.GetConsoleWindow.argtypes = []
kernel32.GetConsoleWindow.restype = wintypes.HWND

user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

SW_HIDE = 0


# ---------------------------------------------------------------- Win32 内存查询

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


# GlobalMemoryStatusEx 需按引用传入 MEMORYSTATUSEX，故在此声明精确参数类型
kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]


def get_mem() -> dict:
    """读取物理内存与提交内存状态。"""
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise ctypes.WinError(ctypes.get_last_error())

    total_phys, avail_phys = m.ullTotalPhys, m.ullAvailPhys
    # 提交内存上限 = 物理内存 + 页面文件；可用提交 = ullAvailPageFile
    total_commit, avail_commit = m.ullTotalPageFile, m.ullAvailPageFile

    return {
        "total_phys": total_phys,
        "avail_phys": avail_phys,
        "used_phys": total_phys - avail_phys,
        "phys_pct": (total_phys - avail_phys) / total_phys * 100 if total_phys else 0,
        "total_commit": total_commit,
        "avail_commit": avail_commit,
        "used_commit": total_commit - avail_commit,
        "commit_pct": (total_commit - avail_commit) / total_commit * 100 if total_commit else 0,
    }


# ---------------------------------------------------------------- 权限 / 单实例

# 管理员身份在进程生命周期内不变，缓存一次即可（菜单/建议/清理/自检都会反复调用）
_admin_cache: bool | None = None


def is_admin() -> bool:
    global _admin_cache
    if _admin_cache is None:
        try:
            _admin_cache = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            _admin_cache = False
    return _admin_cache


# 命名互斥体句柄：需保持引用到进程退出，否则互斥体被回收会导致锁失效
_instance_mutex = None


def acquire_single_instance() -> bool:
    """用命名互斥体实现单实例；若已有实例在运行则返回 False。"""
    global _instance_mutex
    ERROR_ALREADY_EXISTS = 183
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\MemGuard_SingleInstance")
    if not _instance_mutex:
        # 创建失败（极少数情况）时不阻止启动
        return True
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


# 特权相关 Win32 函数的参数/返回类型在此**一次声明**：原先散落在各函数体内，每次调用
# 都重复赋值（既浪费又易漏），一旦漏声明 64 位句柄会被 ctypes 按 32 位截断而静默失败。
advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE, wintypes.BOOL,
    ctypes.POINTER(TOKEN_PRIVILEGES), wintypes.DWORD,
    ctypes.POINTER(TOKEN_PRIVILEGES), ctypes.POINTER(wintypes.DWORD),
]
advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def _set_privilege(name: str, attributes: int) -> bool:
    """设置指定特权的属性（0x2 启用 / 0 禁用），返回是否成功。"""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008 | 0x0020, ctypes.byref(token)
    ):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = attributes
        ok = advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), ctypes.sizeof(tp), None, None
        )
        return bool(ok) and ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(token)


def enable_privilege(name: str) -> bool:
    """启用指定特权。"""
    return _set_privilege(name, 0x00000002)  # SE_PRIVILEGE_ENABLED


def disable_privilege(name: str) -> bool:
    """禁用指定特权（用于清理后恢复，避免高危特权常驻）。"""
    return _set_privilege(name, 0x00000000)  # SE_PRIVILEGE_DISABLED


def privilege_state(name: str) -> int | None:
    """查询指定特权当前属性位（0=禁用, 2=启用）；查询失败返回 None。"""
    TokenPrivileges = 3
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenPrivileges, None, 0, ctypes.byref(size))
        if size.value == 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TokenPrivileges, buf, size.value, ctypes.byref(size)):
            return None
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        arr = ctypes.cast(ctypes.byref(buf, ctypes.sizeof(wintypes.DWORD)),
                          ctypes.POINTER(LUID_AND_ATTRIBUTES * count)).contents
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return None
        for item in arr:
            if item.Luid.LowPart == luid.LowPart and item.Luid.HighPart == luid.HighPart:
                return int(item.Attributes)
        return None
    finally:
        kernel32.CloseHandle(token)


# ---------------------------------------------------------------- 底层清理（Native API）

# NtSetSystemInformation(SystemMemoryListInformation = 80)
SystemMemoryListInformation = 80
MemoryEmptyWorkingSets = 2
MemoryFlushModifiedList = 3
MemoryPurgeStandbyList = 4


class SYSTEM_MEMORY_LIST_COMMAND(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("Command", wintypes.ULONG)]


# NtSetSystemInformation 的类型声明同样集中一次（SetSystemFileCacheSize 已在顶部声明）
ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]
ntdll.NtSetSystemInformation.restype = ctypes.c_long


def _purge_list(cmd: int) -> int:
    c = SYSTEM_MEMORY_LIST_COMMAND(1, cmd)
    return ntdll.NtSetSystemInformation(
        SystemMemoryListInformation, ctypes.byref(c), ctypes.sizeof(c)
    )


def clear_file_cache() -> bool:
    """清空系统文件缓存工作集。传 -1 表示不限大小，效果为立即释放缓存。"""
    return bool(kernel32.SetSystemFileCacheSize(ctypes.c_size_t(-1), ctypes.c_size_t(-1), 0))
