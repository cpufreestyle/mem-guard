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
from datetime import datetime

import psutil
from PIL import Image, ImageDraw, ImageFont
import pystray

# ---------------------------------------------------------------- 路径与配置

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "mem_guard.json")
LOG_PATH = os.path.join(BASE_DIR, "mem_guard.log")

DEFAULT_CONFIG = {
    "phys_threshold": 85,    # 物理内存使用率阈值(%)
    "commit_threshold": 90,  # 提交内存使用率阈值(%)，这是"内存不足"的真正指标
    "interval": 10,          # 检测间隔(秒)
    "cooldown": 300,         # 两次自动清理之间的冷却时间(秒)
    "auto_clean": True,      # 是否开启自动清理
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


def enable_privilege(name: str) -> bool:
    """启用指定特权，返回是否成功。"""
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
        tp.Privileges[0].Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
        ok = advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), ctypes.sizeof(tp), None, None
        )
        return bool(ok) and ctypes.get_last_error() == 0
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
    """执行一次完整清理，返回结果统计。"""
    if not is_admin():
        return {"ok": False, "msg": "需要管理员权限才能清理内存"}

    # 显式检查特权启用结果：失败时给出可读原因，避免只看到黑盒 0xC0000061
    failed_privs = [p for p in CLEAN_PRIVILEGES if not enable_privilege(p)]

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
        why = "特权启用失败: " + ", ".join(failed_privs) if failed_privs else "特权未启用或权限不足"
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


def top_processes(n: int = 10) -> str:
    rows = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            rss = p.info["memory_info"].rss
            if rss:
                rows.append((p.info["name"] or "?", rss, p.pid))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    lines = [f"{i + 1:>2}. {name:<28} {rss / 1024 ** 3:>6.2f} GB   (PID {pid})"
             for i, (name, rss, pid) in enumerate(rows[:n])]
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


# ---------------------------------------------------------------- 主程序

class Guard:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.state = get_mem()
        self.last_clean = 0.0
        self.stop = threading.Event()
        self.icon: pystray.Icon | None = None

    # -- 菜单回调 ------------------------------------------------

    def on_clean_now(self, icon=None, item=None) -> None:
        r = do_clean("手动")
        if not r["ok"]:
            icon.notify(r["msg"], "MemGuard")
            return
        freed = max(r["freed"], 0)
        icon.notify(f"释放 {gb(freed)}\n可用物理 {gb(r['after']['avail_phys'])}", "MemGuard 清理完成")
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
            pystray.MenuItem("退出", self.on_quit),
        )

    # -- 循环 ----------------------------------------------------

    def refresh(self) -> None:
        self.state = get_mem()

    def monitor(self) -> None:
        while not self.stop.is_set():
            try:
                self.refresh()
                s = self.state
                if self.icon:
                    self.icon.icon = make_icon(s["phys_pct"], self.cfg["phys_threshold"])
                    self.icon.title = (
                        f"MemGuard  物理 {s['phys_pct']:.0f}% / 提交 {s['commit_pct']:.0f}%\n"
                        f"可用物理 {gb(s['avail_phys'])}    可用提交 {gb(s['avail_commit'])}"
                    )
                now = time.time()
                over = (s["phys_pct"] >= self.cfg["phys_threshold"]
                        or s["commit_pct"] >= self.cfg["commit_threshold"])
                if over and self.cfg["auto_clean"] and now - self.last_clean > self.cfg["cooldown"]:
                    self.last_clean = now
                    r = do_clean("自动")
                    if r["ok"] and self.icon:
                        self.icon.notify(
                            f"物理 {s['phys_pct']:.0f}% / 提交 {s['commit_pct']:.0f}% 超阈值\n"
                            f"已释放 {gb(max(r['freed'], 0))}",
                            "MemGuard 自动清理",
                        )
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


if __name__ == "__main__":
    if "--once" in sys.argv:
        once()
    else:
        if not acquire_single_instance():
            log("检测到已有 MemGuard 实例在运行，本次启动已取消")
            user32.MessageBoxW(None, "MemGuard 已在运行（请查看系统托盘），本次不再重复启动。",
                               "MemGuard", 0x40)  # MB_ICONINFORMATION
            sys.exit(0)
        Guard().run()
