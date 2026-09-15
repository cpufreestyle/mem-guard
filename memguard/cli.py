# -*- coding: utf-8 -*-
"""
命令行入口：GUI 子系统退出流处理、--once、--selftest、主运行。

只负责"怎么启动"，业务实现分布在 config / winapi / clean / tray。
"""
from __future__ import annotations

import os
import sys

from .config import (
    CLEAN_BLACKLIST_STEMS,
    __version__,
    _norm_proc_name,
    gb,
    log,
    normalize_config,
)
from .clean import do_clean, top_processes
from .tray import (
    Guard,
    _parse_version,
    autostart_enabled,
    make_icon,
    message_box,
)
from .winapi import (
    acquire_single_instance,
    get_mem,
    is_admin,
    kernel32,
    privilege_state,
    user32,
)


def _hide_console() -> None:
    """隐藏随 python.exe 启动时附带的控制台窗口。

    用 pythonw.exe 启动时本就没有控制台，此函数会静默跳过；
    用 python.exe 启动（例如直接双击 .py 或未改造的旧快捷方式）时，
    在这里把黑框隐藏，托盘程序全程不出现前台窗口。
    """
    try:
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


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
    print(f"清理档位: {'激进' if r.get('level') == 'aggressive' else '保守'}")
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
    check("normalize_config", lambda: (
        f"非法档位->{normalize_config({'clean_level': 'xx'})['clean_level']}, "
        f"越界阈值->{normalize_config({'phys_threshold': 999})['phys_threshold']}, "
        f"白名单->{normalize_config({'user_blacklist': ['Chrome.EXE', 'chrome']})['user_blacklist']}"
    ))
    check("blacklist匹配", lambda: f"csrss.exe->{_norm_proc_name('csrss.exe') in CLEAN_BLACKLIST_STEMS}")
    check("_parse_version", lambda: (
        f"v1.10.2 > 1.9.9 -> {_parse_version('v1.10.2') > _parse_version('1.9.9')}, "
        f"同版本 -> {_parse_version('1.3.0') == _parse_version('v1.3.0')}"
    ))

    ok_all = all(ok for _, ok, _ in results)
    for name, ok, val in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    single = acquire_single_instance()
    print(f"  [INFO] single_instance: {'已持有锁' if single else '已有实例在运行'}")
    print("结果：" + ("全部通过" if ok_all else "存在失败项"))
    return 0 if ok_all else 1


def main() -> None:
    # 控制台 / CI 的默认编码可能无法表示中文（如英文 Windows 的 cp1252），
    # 统一把标准流切到 UTF-8 并容忍不可编码字符，避免打印时抛 UnicodeEncodeError。
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None:
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--once" in sys.argv:
        once()
    elif "--selftest" in sys.argv:
        # GUI 子系统（--noconsole）下没有 stdout，CI 冒烟测试需要把自检结果落盘才能看到明细
        _log_path = os.environ.get("MEMGUARD_SELFTEST_LOG")
        if _log_path and sys.stdout is None:
            try:
                sys.stdout = open(_log_path, "w", encoding="utf-8")
            except Exception:
                pass
        sys.exit(selftest())
    else:
        # pythonw.exe 启动时没有标准输出流，兜底到空设备，避免任何 print 抛异常
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        # 隐藏 python.exe 自带的控制台窗口，托盘程序不停留在前台
        _hide_console()
        if not acquire_single_instance():
            log("检测到已有 MemGuard 实例在运行，本次启动已取消")
            # 用守护线程弹提示，主线程立即退出，避免互斥体句柄被卡死的进程长期持有
            message_box("MemGuard", "MemGuard 已在运行（请查看系统托盘），本次不再重复启动。")
            sys.exit(0)
        Guard().run()
