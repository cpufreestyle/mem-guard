# -*- coding: utf-8 -*-
"""
前台窗口判定：全屏应用运行期间让自动清理让路。

对标 ISLC / Mem Reduct 用户最常见的抱怨：正在打游戏，后台一次工作集清理
就把帧率砸出个坑。常见解法是让用户手动把游戏进程加进白名单——但没人愿意
替每个游戏维护一份名单。所以这里按「前台窗口是否铺满整块显示器」判定，与
进程名无关：换任何游戏都自动被保护，无需任何配置。

判定失败（没有前台窗口 / 窗口最小化 / 矩形或显示器信息取不到）一律当成
「不在全屏」：宁可照常清理，也不能把一次探测失败变成永久停摆。
"""
from __future__ import annotations

import ctypes

from . import winapi

# 覆盖判定的容差(px)：无边框窗口常有 1px 缝隙，别为此漏判
_FULLSCREEN_SLACK = 2

# 「前台进程被保护」的统计键（v1.24.0）。口径与其它 stats 计数一致：
# 非 0 才落盘（见 config.normalize_config）
FG_STAT_KEY = "fg_protected"


def covers_rect(win, mon) -> bool:
    """窗口矩形是否铺满显示器矩形（含 _FULLSCREEN_SLACK 容差）。

    纯函数：只比边界、不碰 Win32，单测与自检都不必造窗口；两个参数都是
    (left, top, right, bottom) 四元组。
    """
    return (win[0] <= mon[0] + _FULLSCREEN_SLACK
            and win[1] <= mon[1] + _FULLSCREEN_SLACK
            and win[2] >= mon[2] - _FULLSCREEN_SLACK
            and win[3] >= mon[3] - _FULLSCREEN_SLACK)


def _covers(win: "winapi.RECT", mon: "winapi.RECT") -> bool:
    """RECT 版本的铺满判定：转成四元组交给 covers_rect，口径只有一处。"""
    return covers_rect((win.left, win.top, win.right, win.bottom),
                       (mon.left, mon.top, mon.right, mon.bottom))


def fullscreen_hwnd() -> int | None:
    """前台窗口铺满整块显示器时返回其 HWND，否则返回 None。

    任何一步取不到都返回 None（当成「不在全屏」），理由见模块 docstring。
    """
    try:
        hwnd = int(winapi.user32.GetForegroundWindow() or 0)
        if not hwnd or winapi.user32.IsIconic(hwnd):
            return None
        wr = winapi.RECT()
        if not winapi.user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            return None
        mon = int(winapi.user32.MonitorFromWindow(
            hwnd, winapi.MONITOR_DEFAULTTONEAREST) or 0)
        if not mon:
            return None
        mi = winapi.MONITORINFO()
        mi.cbSize = ctypes.sizeof(winapi.MONITORINFO)
        if not winapi.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        return hwnd if _covers(wr, mi.rcMonitor) else None
    except Exception:
        return None


def is_fullscreen() -> bool:
    """当前是否有全屏应用（每次现算，不做缓存）。"""
    return fullscreen_hwnd() is not None


def foreground_pid() -> int | None:
    """当前前台窗口所属进程的 PID；取不到返回 None。"""
    try:
        hwnd = int(winapi.user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        pid = ctypes.wintypes.DWORD()
        winapi.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:
        return None


def protected_pid(cfg: dict) -> int | None:
    """需要额外保护（不被清空工作集）的前台进程 PID；未开启保护时 None。

    与 fs_pause 是两件事：fs_pause 决定「整次自动清理要不要延后」，这里决定
    「真清的时候别清用户正在用的那个进程」——窗口化的前台程序（编辑器、
    浏览器）也照样受保护，不限于全屏。
    """
    if not cfg.get("fs_protect", True):
        return None
    return foreground_pid()


def pause_reason(cfg: dict) -> str | None:
    """此刻该不该暂停自动清理：该暂停时返回理由串，否则 None。"""
    if not cfg.get("fs_pause", True):
        return None
    if fullscreen_hwnd() is None:
        return None
    return "全屏应用运行中"

