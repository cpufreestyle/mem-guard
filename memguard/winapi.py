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

user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

SW_HIDE = 0

# 小图标（托盘用）像素边长，Windows 已按当前 DPI 折算
SM_CXSMICON = 49

# ---------------------------------------------------------------- 任务栏分组与唤窗

# 任务栏分组标识：MemGuard 的概览 / Top / 趋势 / 建议窗口都归到同一个任务栏按钮下，
# 而不是散落成四五个「Python 的 Tk 锤子」按钮。固定到任务栏的快捷方式要写同一个值
# （快捷方式的 System.AppUserModel.ID），两边一致才会并到一个按钮上。
APP_USER_MODEL_ID = "MemGuard.MemoryGuard"

# 二次启动的「唤窗信号」。从任务栏（或桌面）再点一次图标时，已在运行的实例要能弹出
# 概览窗，否则用户的感觉就是「点了没反应」。用命名事件而不是写文件/弹窗：零文件依赖。
# 名字带 Local\ 前缀，只在本会话内可见，同机其它用户收不到。
SHOW_EVENT_NAME = "Local\\MemGuard_ShowRequest"

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long

kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

_EVENT_ALL_ACCESS = 0x001F0003
_WAIT_OBJECT_0 = 0x00000000


def set_app_user_model_id(aumid: str = APP_USER_MODEL_ID) -> bool:
    """给当前进程指定任务栏 AppUserModelID；失败只是分组不正常，托盘照常工作。"""
    try:
        return shell32.SetCurrentProcessExplicitAppUserModelID(aumid) >= 0
    except Exception:
        return False


def create_show_event():
    """主实例创建唤窗事件；返回句柄，失败返回 None。"""
    handle = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    return handle or None


def open_show_event():
    """打开运行中实例的唤窗事件；打不开（没在跑 / 老版本）返回 None。"""
    handle = kernel32.OpenEventW(_EVENT_ALL_ACCESS, False, SHOW_EVENT_NAME)
    return handle or None


def close_show_event(handle) -> None:
    """释放唤窗事件句柄（进程退出时调用，否则句柄泄漏到内核对象表）。"""
    if handle:
        kernel32.CloseHandle(handle)


def request_show_overview() -> bool:
    """通知已在运行的实例打开内存概览窗；返回信号是否送达。"""
    handle = open_show_event()
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        close_show_event(handle)


def wait_show_request(handle, timeout_ms: int = 500) -> bool:
    """等一次唤窗信号；超时返回 False，供守护线程轮询式等待。"""
    try:
        return kernel32.WaitForSingleObject(handle, timeout_ms) == _WAIT_OBJECT_0
    except Exception:
        return False


# ---------------------------------------------------------------- 窗口类图标（HICON）


# Tk 的 iconphoto 会自己填类图标（GCLP_HICON / GCLP_HICONSM）：大图直接转 HICON，
# 小图是从大图重采样得来的，且全程是预乘 alpha——标题栏那枚 16px 会明显发灰
# （实测红通道比原图低约 20%）。这里按 .ico 32bpp 帧的布局就地造单帧资源字节，
# 交 CreateIconFromResourceEx 生成 HICON 再覆盖类图标：颜色不经预乘往返。
GCLP_HICON = -14
GCLP_HICONSM = -34
# Tk 顶层窗口的类名：winfo_id() 拿到的是 TkChild（类图标 0），父窗口才是它
_TK_TOPLEVEL_CLASS = "TkTopLevel"
# .ico 资源格式版本，CreateIconFromResourceEx 的 dwVersion 必须填这个值
_ICON_RESOURCE_VERSION = 0x00030000
LR_DEFAULTCOLOR = 0x00000000


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


# 图标相关 Win32 函数的参数/返回类型在此一次声明（同样是为了 64 位下手动截断句柄）
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.CreateIconFromResourceEx.argtypes = [
    ctypes.POINTER(ctypes.c_ubyte), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.CreateIconFromResourceEx.restype = wintypes.HANDLE
user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetClassLongPtrW.restype = ctypes.c_ssize_t

# CreateIconFromResourceEx 不保证调用返回后资源字节就可以丢，按句柄留住缓冲区
_ICON_RES_KEEPALIVE: list = []


def window_class_name(hwnd: int) -> str:
    """窗口类名；拿不到（句柄失效等）返回空串。"""
    buf = ctypes.create_unicode_buffer(256)
    if not user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256):
        return ""
    return buf.value


def toplevel_hwnd(root) -> int:
    """tkinter 窗口对应的真·顶层窗口句柄（TkTopLevel）。"""
    try:
        child = int(root.winfo_id())
    except Exception:
        return 0
    if not child:
        return 0
    try:
        hwnd = int(user32.GetParent(wintypes.HWND(child)) or 0)
    except Exception:
        return 0
    if not hwnd or window_class_name(hwnd) != _TK_TOPLEVEL_CLASS:
        return 0
    return hwnd


def _icon_resource_bytes(img) -> bytes:
    """把 RGBA 图像编成 CreateIconFromResourceEx 吃的单帧 .ico 资源字节。

    布局 = BITMAPINFOHEADER（biHeight 取 2h：XOR 彩色位图与 AND 掩码上下摞）
    + XOR 彩色位图（BGRA、自底向上）+ AND 掩码（1bpp、行按 4 字节对齐、全零）。
    alpha 走彩色位图第四通道，与 .ico 32bpp 帧一致，不做任何预乘。
    """
    im = img.convert("RGBA")
    w, h = im.size
    rgba = im.tobytes("raw", "BGRA")
    stride = w * 4
    # DIB 在 biHeight > 0 时自底向上，PIL 给的行是自顶向下，逐行倒序拼回
    xor = b"".join(rgba[y * stride:(y + 1) * stride] for y in range(h - 1, -1, -1))
    and_row = (w + 31) // 32 * 4     # 1bpp 行宽按 DWORD 对齐
    header = BITMAPINFOHEADER(
        biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=h * 2,
        biPlanes=1, biBitCount=32, biCompression=0, biSizeImage=len(xor),
        biXPelsPerMeter=0, biYPelsPerMeter=0, biClrUsed=0, biClrImportant=0,
    )
    return bytes(header) + xor + bytes(and_row * h)


def icon_handle(img) -> int:
    """把 PIL RGBA 图像造成单帧 HICON；失败返回 0。"""
    try:
        w, h = img.size
        if not w or not h or w > 256 or h > 256:
            return 0
        blob = _icon_resource_bytes(img)
        buf = ctypes.create_string_buffer(blob)
        _ICON_RES_KEEPALIVE.append(buf)
        handle = user32.CreateIconFromResourceEx(
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
            len(blob), True, _ICON_RESOURCE_VERSION, w, h, LR_DEFAULTCOLOR,
        )
        return int(handle or 0)
    except Exception:
        return 0


def set_window_class_icons(hwnd: int, big: int, small: int) -> bool:
    """给窗口类装大/小两枚类图标（标题栏、Alt-Tab、任务栏按钮都从这里取）。

    类图标按窗口类而不是按窗口生效：同进程的 TkTopLevel 共享一份，所以每个窗口
    挂完 iconphoto 都要再覆盖一次（Tk 建窗时会重设类图标）。句柄为 0 跳过。
    SetClassLongPtr 返回的是旧值（旧值本身可能就是 0），成败看 GetLastError。
    """
    if not hwnd:
        return False
    ok = True
    try:
        for index, handle in ((GCLP_HICON, big), (GCLP_HICONSM, small)):
            if not handle:
                continue
            ctypes.set_last_error(0)
            user32.SetClassLongPtrW(wintypes.HWND(hwnd), index, handle)
            ok = ctypes.get_last_error() == 0 and ok
    except Exception:
        return False
    return ok


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


# ---------------------------------------------------------------- 进程枚举（快路径）

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD
]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def _snapshot_processes() -> list:
    """Toolhelp32 快照一次取回全部 (进程名, PID)；失败抛 OSError 由调用方回退。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or ctypes.cast(snap, ctypes.c_void_p).value == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        out = []
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.szExeFile or "?", int(entry.th32ProcessID)))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        return out
    finally:
        kernel32.CloseHandle(snap)


def _working_set(pid: int) -> int:
    """进程工作集字节数；句柄打不开（受保护进程）返回 0，由调用方决定是否补齐。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def process_working_sets() -> list:
    """返回 [(进程名, 工作集字节, PID), ...]，受保护进程的工作集记为 0。

    比 psutil 逐进程建对象读 memory_info 快两个数量级（450 进程实测 16ms vs 1150ms），
    代价是拿不到 PROCESS_QUERY_LIMITED_INFORMATION 被拒绝的进程，故返回 0 让上层补齐。
    """
    return [(name, _working_set(pid), pid) for name, pid in _snapshot_processes()]


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
# 命令值取自 SYSTEM_MEMORY_LIST_COMMAND 枚举（winnt.h）
SystemMemoryListInformation = 80
MemoryEmptyWorkingSets = 2
MemoryFlushModifiedList = 3
MemoryPurgeStandbyList = 4
# 只清理 standby 列表中的低优先级部分（较新 Windows 支持；不支持时返回非 0，属预期）
MemoryPurgeLowPriorityStandbyList = 5


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
