# -*- coding: utf-8 -*-
"""
MemGuard - Windows 内存守护托盘工具

解决什么问题
------------
Windows 报「内存不足 / 虚拟内存不足」时，真正耗尽的是 **提交内存(commit)**，
而不是物理内存。任务管理器默认不显示 commit，所以经常出现"明明还剩几个 G
却一直弹内存不足"的怪现象。本工具同时监控这两个指标。

功能
----
1. 托盘图标实时显示物理内存使用率，颜色随压力变化（绿 -> 黄 -> 红）
2. 鼠标悬停显示物理内存 + 提交内存详情
3. 任一指标超阈值时自动清理并弹气泡提醒：
     清空进程工作集 -> 刷写修改列表 -> 清理 standby list -> 清空系统文件缓存
   （原理与 ISLC / Mem Reduct 相同，调用 NtSetSystemInformation）
4. 可查看内存占用 Top10、开关自动清理、调整阈值
5. 配置热重载（改 json 无需重启）、内存趋势窗口、一键导出诊断；超阈值自动清理支持防抖

清理功能需要 **管理员权限**（要 SeProfileSingleProcessPrivilege 等特权）。
非管理员运行时程序仍可启动，但清理会失败并给出提示。

依赖：pip install psutil pystray Pillow
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import subprocess
import sys
import threading
import time
import collections
from datetime import datetime

import psutil
from PIL import Image, ImageDraw, ImageFont
import pystray

# ---------------------------------------------------------------- 路径与配置

__version__ = "1.2.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "mem_guard.json")
LOG_PATH = os.path.join(BASE_DIR, "mem_guard.log")

DEFAULT_CONFIG = {
    "phys_threshold": 85,    # 物理内存使用率阈值(%)
    "commit_threshold": 90,  # 提交内存使用率阈值(%)，这是"内存不足"的真正指标
    "interval": 10,          # 检测间隔(秒)
    "cooldown": 300,         # 两次自动清理之间的冷却时间(秒)
    "auto_clean": True,      # 是否开启自动清理
    "debounce_sec": 0,       # 内存持续超阈值的宽限秒数(防抖)，0=立即触发
}

# 不对其做 EmptyWorkingSet 的进程，清空这些进程的工作集会导致系统不稳定或界面闪烁
CLEAN_BLACKLIST = {
    "system", "system idle process", "idle", "registry", "memory compression",
    "csrss", "smss", "wininit", "winlogon", "services", "lsass", "lsaiso",
    "dwm", "fontdrvhost", "sihost", "ctfmon",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


LOG_MAX_BYTES = 1 * 1024 * 1024  # 日志超过 1MB 时轮转为 mem_guard.log.1


def _rotate_log_if_needed() -> None:
    """日志超过上限时归档为 mem_guard.log.1（只保留一个备份）。"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            bak = LOG_PATH + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(LOG_PATH, bak)
    except Exception:
        pass


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    _rotate_log_if_needed()
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


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

kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

kernel32.SetSystemFileCacheSize.argtypes = [ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
kernel32.SetSystemFileCacheSize.restype = wintypes.BOOL

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
psapi.EmptyWorkingSet.restype = wintypes.BOOL

user32.MessageBoxW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int


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


def gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f}GB"


# ---------------------------------------------------------------- 权限与清理

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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


def _set_privilege(name: str, attributes: int) -> bool:
    """设置指定特权的属性（0x2 启用 / 0 禁用），返回是否成功。"""
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
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

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
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL

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


# NtSetSystemInformation(SystemMemoryListInformation = 80)
SystemMemoryListInformation = 80
MemoryEmptyWorkingSets = 2
MemoryFlushModifiedList = 3
MemoryPurgeStandbyList = 4


class SYSTEM_MEMORY_LIST_COMMAND(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("Command", wintypes.ULONG)]


def _purge_list(cmd: int) -> int:
    c = SYSTEM_MEMORY_LIST_COMMAND(1, cmd)
    ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]
    ntdll.NtSetSystemInformation.restype = ctypes.c_long
    return ntdll.NtSetSystemInformation(
        SystemMemoryListInformation, ctypes.byref(c), ctypes.sizeof(c)
    )


def empty_process_working_sets() -> int:
    """逐个进程清空工作集（把不活跃的物理页移到 standby list）。"""
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.restype = wintypes.BOOL
    kernel32.OpenProcess.restype = wintypes.HANDLE

    count = 0
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name in CLEAN_BLACKLIST or p.info["pid"] in (0, 4):
                continue
            h = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, p.info["pid"]
            )
            if h:
                try:
                    if psapi.EmptyWorkingSet(h):
                        count += 1
                finally:
                    kernel32.CloseHandle(h)
        except Exception:
            continue
    return count


def clear_file_cache() -> bool:
    """清空系统文件缓存工作集。传 -1 表示不限大小，效果为立即释放缓存。"""
    kernel32.SetSystemFileCacheSize.argtypes = [ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    kernel32.SetSystemFileCacheSize.restype = wintypes.BOOL
    return bool(kernel32.SetSystemFileCacheSize(ctypes.c_size_t(-1), ctypes.c_size_t(-1), 0))


CLEAN_PRIVILEGES = (
    "SeDebugPrivilege",
    "SeProfileSingleProcessPrivilege",
    "SeIncreaseQuotaPrivilege",
    "SeIncreaseBasePriorityPrivilege",
)


def do_clean(reason: str = "手动") -> dict:
    """执行一次完整清理，返回结果统计。清理后会恢复特权开关（敏感特权用完即关）。"""
    if not is_admin():
        return {"ok": False, "msg": "需要管理员权限才能清理内存"}

    # 记录特权初始状态；清理结束后把"原本未启用"的特权恢复为禁用
    prior = {p: privilege_state(p) for p in CLEAN_PRIVILEGES}
    # 显式检查特权启用结果：失败时给出可读原因，避免只看到黑盒 0xC0000061
    failed_privs = [p for p in CLEAN_PRIVILEGES if not enable_privilege(p)]

    try:
        before = get_mem()
        detail = []
        if failed_privs:
            detail.append("特权启用失败:" + ",".join(failed_privs))

        r_ws = _purge_list(MemoryEmptyWorkingSets)
        detail.append(f"清空工作集:{'成功' if r_ws == 0 else f'失败(0x{r_ws & 0xffffffff:X})'}")

        r_fm = _purge_list(MemoryFlushModifiedList)
        detail.append(f"刷写修改页:{'成功' if r_fm == 0 else f'失败(0x{r_fm & 0xffffffff:X})'}")

        r_sb = _purge_list(MemoryPurgeStandbyList)
        standby_ok = r_sb == 0
        detail.append(f"清理Standby:{'成功' if standby_ok else f'失败(0x{r_sb & 0xffffffff:X})'}")

        n = empty_process_working_sets()
        detail.append(f"进程数:{n}")

        fc = clear_file_cache()
        detail.append(f"文件缓存:{'成功' if fc else '失败'}")

        # 三个核心 NT 操作全部失败 -> 视为整体失败，直接返回可读原因
        if r_ws != 0 and r_fm != 0 and r_sb != 0:
            why = ("特权启用失败: " + ", ".join(failed_privs)) if failed_privs else "特权未启用或权限不足"
            log(f"{reason}清理失败 | {why} | {', '.join(detail)}")
            return {"ok": False, "msg": f"清理失败（{why}）"}

        time.sleep(1.5)  # 等系统把页回收计入可用内存
        after = get_mem()

        freed = after["avail_phys"] - before["avail_phys"]
        result = {
            "ok": True,
            "freed": freed,
            "before": before,
            "after": after,
            "detail": ", ".join(detail),
        }
        log(f"{reason}清理 | 可用物理 {gb(before['avail_phys'])} -> {gb(after['avail_phys'])} "
            f"(释放 {gb(max(freed, 0))}) | 提交 {before['commit_pct']:.0f}% -> {after['commit_pct']:.0f}% | {result['detail']}")
        return result
    finally:
        # 仅禁用"清理前处于禁用状态"的特权，避免高危特权（如 SeDebugPrivilege）常驻
        for p, st in prior.items():
            if st is not None and not (st & 0x2):
                disable_privilege(p)


def top_processes_list(n: int = 10) -> list:
    """返回物理内存占用最高的 n 个进程 [(name, rss_bytes, pid), ...]。"""
    rows = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            rss = p.info["memory_info"].rss
            if rss:
                rows.append((p.info["name"] or "?", rss, p.pid))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:n]


def top_processes(n: int = 10) -> str:
    lines = [f"{i + 1:>2}. {name:<28} {rss / 1024 ** 3:>6.2f} GB   (PID {pid})"
             for i, (name, rss, pid) in enumerate(top_processes_list(n))]
    return "\n".join(lines)


# ---------------------------------------------------------------- 托盘图标

def make_icon(pct: float, threshold: float = 85.0) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 颜色分档跟随可调的物理阈值：红=达到阈值，黄=阈值前 10% 区间，绿=更宽松
    warn = max(threshold - 10.0, 1.0)
    if pct < warn:
        color = (46, 160, 67, 255)     # 绿
    elif pct < threshold:
        color = (214, 158, 46, 255)    # 黄
    else:
        color = (207, 59, 54, 255)     # 红

    d.ellipse([2, 2, size - 3, size - 3], fill=color)

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 26)
    except Exception:
        font = ImageFont.load_default()

    text = str(int(round(pct)))
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1] - 2),
           text, fill=(255, 255, 255, 255), font=font)
    return img


def message_box(title: str, text: str) -> None:
    threading.Thread(
        target=lambda: user32.MessageBoxW(0, text, title, 0x00000000 | 0x00040000),
        daemon=True,
    ).start()


_top_window_open = False


def show_top_window() -> None:
    """弹出可刷新的内存占用 Top10 窗口；无 tkinter 时回退到 MessageBox。"""
    global _top_window_open
    if _top_window_open:
        return
    _top_window_open = True

    def worker() -> None:
        global _top_window_open
        try:
            import tkinter as tk
        except Exception:
            _top_window_open = False
            st = get_mem()
            message_box(
                "内存占用 Top10",
                f"物理内存 {st['phys_pct']:.0f}%   提交内存 {st['commit_pct']:.0f}%\n"
                f"{'-' * 46}\n{top_processes(10)}",
            )
            return
        try:
            root = tk.Tk()
            root.title("MemGuard - 内存占用 Top10")
            root.geometry("600x440")
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass

            header = tk.Label(root, text="", justify="left", anchor="w", font=("Consolas", 10))
            header.pack(fill="x", padx=10, pady=(10, 4))

            body = tk.Text(root, font=("Consolas", 10), wrap="none",
                           relief="flat", background="#f7f7f7")
            body.pack(fill="both", expand=True, padx=10)

            def refresh() -> None:
                st = get_mem()
                header.config(
                    text=(f"物理内存 {st['phys_pct']:.0f}%   "
                          f"({gb(st['used_phys'])} / {gb(st['total_phys'])})\n"
                          f"提交内存 {st['commit_pct']:.0f}%   "
                          f"(可用 {gb(st['avail_commit'])} / {gb(st['total_commit'])})")
                )
                body.delete("1.0", "end")
                body.insert("1.0", top_processes(10))

            tk.Button(root, text="刷新", width=12, command=refresh).pack(pady=8)
            refresh()
            root.mainloop()
        finally:
            _top_window_open = False

    threading.Thread(target=worker, daemon=True).start()


_trend_window_open = False


def show_trend_window(history_getter) -> None:
    """弹出可刷新的内存趋势窗口（物理/提交两条曲线）。无 tkinter 时静默返回。"""
    global _trend_window_open
    if _trend_window_open:
        return
    _trend_window_open = True

    def worker() -> None:
        global _trend_window_open
        try:
            import tkinter as tk
        except Exception:
            _trend_window_open = False
            return
        try:
            root = tk.Tk()
            root.title("MemGuard - 内存趋势")
            root.geometry("660x440")
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass

            canvas = tk.Canvas(root, bg="white")
            canvas.pack(fill="both", expand=True)

            def draw() -> None:
                canvas.delete("all")
                data = list(history_getter())
                w = max(canvas.winfo_width(), 1)
                h = max(canvas.winfo_height(), 1)
                canvas.create_text(12, 8, anchor="nw",
                                   text="蓝=物理 / 红=提交 (使用率 %)", fill="#333")
                if len(data) < 2:
                    canvas.create_text(w / 2, h / 2, anchor="center",
                                       text="采样中...（稍候自动刷新）", fill="#999")
                    return
                n = len(data)
                for lvl, col in ((85, "#cccccc"), (100, "#e0a0a0")):
                    y = h - 10 - (h - 20) * (lvl / 100)
                    canvas.create_line(10, y, w - 10, y, fill=col, dash=(4, 4))
                for key, color in ((lambda p, c: p, "#2d6cdf"),
                                   (lambda p, c: c, "#cf3b36")):
                    coords = []
                    for i, (_, ph, cm) in enumerate(data):
                        x = 10 + (w - 20) * i / (n - 1)
                        y = h - 10 - (h - 20) * (key(ph, cm) / 100)
                        coords += [x, y]
                    canvas.create_line(*coords, fill=color, width=2)

            def tick() -> None:
                try:
                    if root.winfo_exists():
                        draw()
                        root.after(1000, tick)
                except Exception:
                    pass

            tk.Button(root, text="刷新", width=12, command=draw).pack(pady=8)
            draw()
            root.after(1000, tick)
            root.mainloop()
        finally:
            _trend_window_open = False

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- 开机自启（计划任务）

AUTOSTART_TASK = "MemGuard"
_CREATE_NO_WINDOW = 0x08000000


def _run_silent(cmd: list) -> bool:
    """静默运行外部命令（不弹控制台窗口），返回是否成功。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=_CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def _pythonw_path():
    import shutil
    for cand in ("pythonw.exe", "python.exe"):
        p = shutil.which(cand)
        if p:
            return p
    return None


def autostart_enabled() -> bool:
    """查询计划任务 MemGuard 是否已注册。"""
    return _run_silent(["schtasks", "/Query", "/TN", AUTOSTART_TASK])


def install_autostart() -> bool:
    """注册登录自启计划任务（最高权限，无 UAC 弹窗）。优先复用 install_autostart.ps1。"""
    ps1 = os.path.join(BASE_DIR, "install_autostart.ps1")
    if os.path.exists(ps1):
        return _run_silent(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", ps1, "-Mode", "install"])
    pyw = _pythonw_path()
    if not pyw:
        return False
    tr = f'"{pyw}" "{os.path.join(BASE_DIR, "mem_guard.py")}"'
    return _run_silent(["schtasks", "/Create", "/TN", AUTOSTART_TASK, "/TR", tr,
                        "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"])


def remove_autostart() -> bool:
    """删除开机自启计划任务。"""
    ps1 = os.path.join(BASE_DIR, "install_autostart.ps1")
    if os.path.exists(ps1):
        return _run_silent(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", ps1, "-Mode", "uninstall"])
    return _run_silent(["schtasks", "/Delete", "/TN", AUTOSTART_TASK, "/F"])


# ---------------------------------------------------------------- 诊断导出

def export_diagnostics() -> str:
    """打包内存状态/进程/日志/配置为 zip，返回路径；失败返回 'ERR:...'。"""
    import zipfile
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = os.path.join(BASE_DIR, f"diagnostics_{ts}.zip")
        s = get_mem()
        info = [
            f"version={__version__}",
            f"admin={is_admin()}",
            f"物理 {s['phys_pct']:.1f}%  ({gb(s['used_phys'])} / {gb(s['total_phys'])})",
            f"提交 {s['commit_pct']:.1f}%  (可用 {gb(s['avail_commit'])} / {gb(s['total_commit'])})",
            f"autostart={autostart_enabled()}",
        ]
        for p in CLEAN_PRIVILEGES:
            info.append(f"priv {p}={privilege_state(p)}")
        info.append("--- 物理占用 Top20 ---")
        for name, rss, pid in top_processes_list(20):
            info.append(f"{name}  {rss / 1024 ** 3:.2f} GB  PID {pid}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("diagnostics.txt", "\n".join(info))
            if os.path.exists(CONFIG_PATH):
                z.write(CONFIG_PATH, "mem_guard.json")
            if os.path.exists(LOG_PATH):
                z.write(LOG_PATH, "mem_guard.log")
        return zip_path
    except Exception as e:
        return f"ERR:{e}"


# ---------------------------------------------------------------- 主程序

class Guard:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.state = get_mem()
        self.last_clean = 0.0
        self.over_since = None       # 内存超阈值起始时刻（防抖用）
        self.history = collections.deque(maxlen=120)  # 内存使用率采样历史
        self._cfg_mtime = None       # 配置热重载：记录上次 mtime
        self.stop = threading.Event()
        self.icon: pystray.Icon | None = None

    # -- 菜单回调 ------------------------------------------------

    def on_clean_now(self, icon=None, item=None) -> None:
        r = do_clean("手动")
        if not r["ok"]:
            icon.notify(r["msg"], "MemGuard")
            return
        freed = max(r["freed"], 0)
        top3 = top_processes_list(3)
        top_txt = "\n".join(f"  {n} {rss / 1024 ** 3:.2f}GB" for n, rss, _ in top3)
        icon.notify(
            f"释放 {gb(freed)}  可用物理 {gb(r['after']['avail_phys'])}\n"
            f"当前占用 Top3:\n{top_txt}",
            "MemGuard 清理完成",
        )
        self.refresh()

    def on_top(self, icon=None, item=None) -> None:
        show_top_window()

    def on_toggle_auto(self, icon, item) -> None:
        self.cfg["auto_clean"] = not self.cfg["auto_clean"]
        save_config(self.cfg)
        log(f"自动清理 -> {'开启' if self.cfg['auto_clean'] else '关闭'}")

    def on_open_log(self, icon=None, item=None) -> None:
        try:
            os.startfile(LOG_PATH)
        except Exception:
            pass

    def on_quit(self, icon, item) -> None:
        self.stop.set()
        icon.stop()

    @staticmethod
    def _preset(cfg, phys, commit):
        def setter(icon, item):
            cfg["phys_threshold"] = phys
            cfg["commit_threshold"] = commit
            save_config(cfg)
        return setter

    @staticmethod
    def _set_cooldown(cfg, minutes):
        def setter(icon, item):
            cfg["cooldown"] = minutes * 60
            save_config(cfg)
        return setter

    def on_toggle_autostart(self, icon, item) -> None:
        if autostart_enabled():
            ok = remove_autostart()
            log(f"取消开机自启 -> {'成功' if ok else '失败'}")
            icon.notify("已取消开机自启" if ok else "取消失败", "MemGuard")
        else:
            ok = install_autostart()
            log(f"设置开机自启 -> {'成功' if ok else '失败'}")
            icon.notify("已设置开机自启（登录时自动启动）" if ok
                        else "设置失败（需管理员权限）", "MemGuard")

    def on_trend(self, icon=None, item=None) -> None:
        show_trend_window(lambda: list(self.history))

    def on_export(self, icon=None, item=None) -> None:
        path = export_diagnostics()
        if path.startswith("ERR:"):
            icon.notify("诊断导出失败：" + path, "MemGuard")
        else:
            icon.notify("诊断已导出：\n" + path, "MemGuard 诊断")

    def build_menu(self) -> pystray.Menu:
        cfg = self.cfg

        def line_phys(_):
            s = self.state
            return f"物理内存  {s['phys_pct']:.0f}%   ({gb(s['used_phys'])} / {gb(s['total_phys'])})"

        def line_commit(_):
            s = self.state
            return f"提交内存  {s['commit_pct']:.0f}%   (可用 {gb(s['avail_commit'])})"

        def line_tip(_):
            return "↑ 内存不足报错看这一行"

        def preset_menu():
            return pystray.Menu(
                pystray.MenuItem(
                    "激进  物理75% / 提交85%",
                    self._preset(cfg, 75, 85), radio=True,
                    checked=lambda i: cfg["phys_threshold"] == 75,
                ),
                pystray.MenuItem(
                    "标准  物理85% / 提交90%",
                    self._preset(cfg, 85, 90), radio=True,
                    checked=lambda i: cfg["phys_threshold"] == 85,
                ),
                pystray.MenuItem(
                    "宽松  物理92% / 提交95%",
                    self._preset(cfg, 92, 95), radio=True,
                    checked=lambda i: cfg["phys_threshold"] == 92,
                ),
            )

        def cooldown_menu():
            choices = [(1, "1 分钟"), (5, "5 分钟"), (10, "10 分钟"), (30, "30 分钟")]
            return pystray.Menu(*[
                pystray.MenuItem(
                    label,
                    self._set_cooldown(cfg, m), radio=True,
                    checked=(lambda i, mm=m: cfg["cooldown"] == mm * 60),
                ) for m, label in choices
            ])

        return pystray.Menu(
            pystray.MenuItem(line_phys, None, enabled=False),
            pystray.MenuItem(line_commit, None, enabled=False),
            pystray.MenuItem(line_tip, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即清理", self.on_clean_now),
            pystray.MenuItem("内存占用 Top10", self.on_top),
            pystray.MenuItem("内存趋势", self.on_trend),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("自动清理", self.on_toggle_auto,
                             checked=lambda i: cfg["auto_clean"]),
            pystray.MenuItem("清理阈值", preset_menu()),
            pystray.MenuItem("清理冷却", cooldown_menu()),
            pystray.MenuItem("开机自启", self.on_toggle_autostart,
                             checked=lambda i: autostart_enabled()),
            pystray.MenuItem(
                lambda i: "权限：管理员（可清理）" if is_admin() else "⚠ 非管理员，无法清理",
                None, enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开日志", self.on_open_log),
            pystray.MenuItem("导出诊断", self.on_export),
            pystray.MenuItem("退出", self.on_quit),
        )

    # -- 循环 ----------------------------------------------------

    def refresh(self) -> None:
        self.state = get_mem()

    def maybe_reload_config(self) -> None:
        """检查配置文件 mtime，变化则热重载（无需重启托盘）。"""
        try:
            mtime = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else None
            if mtime != self._cfg_mtime:
                self._cfg_mtime = mtime
                if mtime is not None:
                    self.cfg = load_config()
                    log("配置已热重载（来自 mem_guard.json）")
        except Exception as e:
            log(f"配置重载检查失败: {e}")

    def monitor(self) -> None:
        self.maybe_reload_config()
        while not self.stop.is_set():
            try:
                self.maybe_reload_config()
                self.refresh()
                s = self.state
                self.history.append((time.time(), s["phys_pct"], s["commit_pct"]))
                if self.icon:
                    self.icon.icon = make_icon(s["phys_pct"], self.cfg["phys_threshold"])
                    self.icon.title = (
                        f"MemGuard  物理 {s['phys_pct']:.0f}% / 提交 {s['commit_pct']:.0f}%\n"
                        f"可用物理 {gb(s['avail_phys'])}    可用提交 {gb(s['avail_commit'])}"
                    )
                now = time.time()
                over = (s["phys_pct"] >= self.cfg["phys_threshold"]
                        or s["commit_pct"] >= self.cfg["commit_threshold"])
                if over:
                    if self.over_since is None:
                        self.over_since = now
                    debounced = (now - self.over_since) >= self.cfg.get("debounce_sec", 0)
                    due = (self.cfg["auto_clean"] and debounced
                           and now - self.last_clean > self.cfg["cooldown"])
                    if due:
                        self.last_clean = now
                        self.over_since = None
                        r = do_clean("自动")
                        if r["ok"] and self.icon:
                            top3 = top_processes_list(3)
                            top_txt = "\n".join(f"  {n} {rss / 1024 ** 3:.2f}GB" for n, rss, _ in top3)
                            self.icon.notify(
                                f"物理 {s['phys_pct']:.0f}% / 提交 {s['commit_pct']:.0f}% 超阈值\n"
                                f"已释放 {gb(max(r['freed'], 0))}\n当前占用 Top3:\n{top_txt}",
                                "MemGuard 自动清理",
                            )
                else:
                    self.over_since = None
            except Exception as e:
                log(f"监控异常: {e}")
            self.stop.wait(self.cfg["interval"])

    def run(self) -> None:
        s = get_mem()
        self.icon = pystray.Icon(
            "MemGuard", make_icon(s["phys_pct"], self.cfg["phys_threshold"]),
            "MemGuard 启动中...", self.build_menu(),
        )
        log(f"MemGuard 启动 | 管理员={is_admin()} | 物理 {s['phys_pct']:.0f}% | 提交 {s['commit_pct']:.0f}%")
        if not is_admin():
            log("提示：非管理员运行，自动清理不会生效，请右键以管理员身份运行")
        threading.Thread(target=self.monitor, daemon=True).start()
        self.icon.run()


# ---------------------------------------------------------------- 入口

def once() -> None:
    """命令行模式：打印一次状态并执行清理，用于验证，不进托盘。"""
    s = get_mem()
    print(f"物理内存 {s['phys_pct']:.1f}%  ({gb(s['used_phys'])} / {gb(s['total_phys'])})")
    print(f"提交内存 {s['commit_pct']:.1f}%  ({gb(s['used_commit'])} / {gb(s['total_commit'])})")
    print(f"管理员权限: {is_admin()}")
    print("-" * 50)
    r = do_clean("测试")
    if not r["ok"]:
        print(r["msg"])
        return
    print(f"清理前可用物理 {gb(r['before']['avail_phys'])}  提交 {r['before']['commit_pct']:.1f}%")
    print(f"清理后可用物理 {gb(r['after']['avail_phys'])}  提交 {r['after']['commit_pct']:.1f}%")
    print(f"释放 {gb(max(r['freed'], 0))}")
    print(f"明细: {r['detail']}")


def selftest() -> int:
    """内置自检：验证关键功能可用，返回退出码（0=全部通过）。"""
    print(f"MemGuard 自检 (v{__version__})")
    results = []

    def check(name, fn):
        try:
            results.append((name, True, fn()))
        except Exception as e:
            results.append((name, False, repr(e)))

    check("get_mem", lambda: f"物理 {get_mem()['phys_pct']:.0f}% / 提交 {get_mem()['commit_pct']:.0f}%")
    check("make_icon", lambda: f"{make_icon(50, 85).size}")
    check("is_admin", is_admin)
    check("privilege_state", lambda: privilege_state("SeDebugPrivilege"))
    check("autostart_enabled", autostart_enabled)
    check("top_processes", lambda: f"{len(top_processes(5).splitlines())} 行")

    ok_all = all(ok for _, ok, _ in results)
    for name, ok, val in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    single = acquire_single_instance()
    print(f"  [INFO] single_instance: {'已持有锁' if single else '已有实例在运行'}")
    print("结果：" + ("全部通过" if ok_all else "存在失败项"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    if "--once" in sys.argv:
        once()
    elif "--selftest" in sys.argv:
        sys.exit(selftest())
    else:
        if not acquire_single_instance():
            log("检测到已有 MemGuard 实例在运行，本次启动已取消")
            user32.MessageBoxW(None, "MemGuard 已在运行（请查看系统托盘），本次不再重复启动。",
                               "MemGuard", 0x40)  # MB_ICONINFORMATION
            sys.exit(0)
        Guard().run()
