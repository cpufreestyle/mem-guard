# -*- coding: utf-8 -*-
"""
前台 / 全屏判定：全屏应用（游戏 / 投屏）运行期间让自动清理让路（v1.24.0）。

判定本身不发明，靠 Win32 现算；窗口矩形铺满整块显示器即算全屏，与进程名无关。
全栈路径用假 Win32 覆盖——测试里没有窗口可铺，也不能真去动用户桌面。
"""
import ctypes

from memguard import foreground, winapi
from memguard.config import normalize_config


def test_covers_rect_exact_slack_and_smaller():
    assert foreground.covers_rect((0, 0, 1920, 1080), (0, 0, 1920, 1080))
    # 无边框窗口常有 1px 缝隙，算铺满
    assert foreground.covers_rect((-1, -1, 1921, 1081), (0, 0, 1920, 1080))
    assert foreground.covers_rect((0, 0, 1920, 1078), (0, 0, 1920, 1080))
    assert not foreground.covers_rect((0, 0, 1920, 1077), (0, 0, 1920, 1080))
    assert not foreground.covers_rect((100, 100, 1000, 800), (0, 0, 1920, 1080))
    # 窗口化但高度够：只覆盖一半宽度，不算
    assert not foreground.covers_rect((0, 0, 960, 1080), (0, 0, 1920, 1080))
    # 副屏坐标：原点火偏移时依然按各自矩形比较
    assert foreground.covers_rect((1920, 0, 3360, 1080), (1920, 0, 3360, 1080))
    assert not foreground.covers_rect((1920, 0, 3000, 1080), (1920, 0, 3360, 1080))


def _fake_rect(l, t, r, b, ok=True):
    def fake(hwnd, ptr):
        if not ok:
            return False
        rect = ctypes.cast(ptr, ctypes.POINTER(winapi.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = l, t, r, b
        return True
    return fake


def _fake_monitor(l, t, r, b, ok=True):
    def fake(mon, ptr):
        if not ok:
            return False
        mi = ctypes.cast(ptr, ctypes.POINTER(winapi.MONITORINFO)).contents
        mi.rcMonitor.left, mi.rcMonitor.top = l, t
        mi.rcMonitor.right, mi.rcMonitor.bottom = r, b
        return True
    return fake


def _install(monkeypatch, *, hwnd=1001, iconic=False, rect=(0, 0, 1920, 1080),
            rect_ok=True, mon=7, mon_ok=True, screen=(0, 0, 1920, 1080)):
    monkeypatch.setattr(winapi.user32, "GetForegroundWindow", lambda: hwnd)
    monkeypatch.setattr(winapi.user32, "IsIconic", lambda h: iconic)
    monkeypatch.setattr(winapi.user32, "GetWindowRect",
                        _fake_rect(*rect, ok=rect_ok))
    monkeypatch.setattr(winapi.user32, "MonitorFromWindow", lambda h, f: mon)
    monkeypatch.setattr(winapi.user32, "GetMonitorInfoW",
                        _fake_monitor(*screen, ok=mon_ok))


def test_fullscreen_when_window_covers_the_whole_monitor(monkeypatch):
    _install(monkeypatch)
    assert foreground.fullscreen_hwnd() == 1001
    assert foreground.is_fullscreen() is True


def test_not_fullscreen_when_windowed(monkeypatch):
    _install(monkeypatch, rect=(0, 0, 1000, 800))
    assert foreground.fullscreen_hwnd() is None


def test_fullscreen_on_secondary_monitor(monkeypatch):
    _install(monkeypatch, rect=(1920, 0, 3360, 1080), screen=(1920, 0, 3360, 1080))
    assert foreground.fullscreen_hwnd() == 1001


def test_lookup_failures_are_treated_as_not_fullscreen(monkeypatch):
    # 没有前台窗口
    _install(monkeypatch, hwnd=0)
    assert foreground.fullscreen_hwnd() is None
    # 最小化中的窗口不算全屏
    _install(monkeypatch, iconic=True)
    assert foreground.fullscreen_hwnd() is None
    # 矩形取不到
    _install(monkeypatch, rect_ok=False)
    assert foreground.fullscreen_hwnd() is None
    # 显示器取不到
    _install(monkeypatch, mon_ok=False)
    assert foreground.fullscreen_hwnd() is None


def test_fullscreen_swallows_unexpected_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("win32 blew up")
    monkeypatch.setattr(winapi.user32, "GetForegroundWindow", boom)
    assert foreground.fullscreen_hwnd() is None, "探测失败绝不能当成永久停摆"


def test_foreground_pid_reads_the_foreground_window(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(winapi.user32, "GetWindowThreadProcessId",
                        _fake_pid(4242))
    assert foreground.foreground_pid() == 4242
    _install(monkeypatch, hwnd=0)
    assert foreground.foreground_pid() is None


def _fake_pid(pid):
    def fake(hwnd, ptr):
        value = ctypes.cast(ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents
        value.value = pid
        return 0
    return fake


def test_protected_pid_respects_the_switch(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(winapi.user32, "GetWindowThreadProcessId",
                        _fake_pid(4242))
    assert foreground.protected_pid(normalize_config({"fs_protect": True})) == 4242
    assert foreground.protected_pid(normalize_config({"fs_protect": False})) is None


def test_pause_reason_respects_the_switch(monkeypatch):
    monkeypatch.setattr(foreground, "fullscreen_hwnd", lambda: 9)
    assert foreground.pause_reason(normalize_config({"fs_pause": True})) == "全屏应用运行中"
    assert foreground.pause_reason(normalize_config({"fs_pause": False})) is None
    monkeypatch.setattr(foreground, "fullscreen_hwnd", lambda: None)
    assert foreground.pause_reason(normalize_config({"fs_pause": True})) is None

