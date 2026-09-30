# -*- coding: utf-8 -*-
"""winapi：内存读取结构、权限查询缓存、单实例锁、进程快照快路径。"""
import ctypes
import ctypes.wintypes as wintypes
import os

import uuid

import psutil
import pytest
from PIL import Image

from memguard import winapi
from memguard.winapi import acquire_single_instance, get_mem, is_admin


def test_get_mem_shape():
    s = get_mem()
    for key in ("total_phys", "avail_phys", "used_phys", "phys_pct",
                "total_commit", "avail_commit", "used_commit", "commit_pct"):
        assert key in s
    assert s["total_phys"] > 0
    assert 0 <= s["phys_pct"] <= 100
    assert 0 <= s["commit_pct"] <= 100


def test_is_admin_is_bool_and_stable():
    assert isinstance(is_admin(), bool)
    assert is_admin() == is_admin()   # 进程内缓存，结果稳定


def test_acquire_single_instance_returns_bool():
    assert isinstance(acquire_single_instance(), bool)


def test_process_working_sets_reports_self():
    """快路径必须能读到自己：结构体偏移错位时这里最先失败（读不到或数值离谱）。"""
    rows = {(pid): (name, rss) for name, rss, pid in winapi.process_working_sets()}
    assert os.getpid() in rows, "快照应包含当前进程"
    name, rss = rows[os.getpid()]
    assert name.lower().endswith(".exe")
    own = psutil.Process(os.getpid()).memory_info().rss
    assert abs(rss - own) < max(own * 0.2, 8 * 1024 * 1024), f"{rss} vs {own}"


def test_process_working_sets_reports_many_with_a_working_set():
    rows = winapi.process_working_sets()
    assert len(rows) > 10
    assert sum(1 for _, rss, _ in rows if rss > 0) > 10


# ---------------------------------------------------------------- 唤窗信号（任务栏再点一次）


def _unique_event_name(monkeypatch) -> str:
    """把唤窗事件换成本测试专属的名字。

    绝不能直接用 SHOW_EVENT_NAME：开发机上可能正跑着一个真的 MemGuard，
    一 SetEvent 就把用户的概览窗弹出来了。
    """
    name = "Local\\MemGuard_Test_" + uuid.uuid4().hex
    monkeypatch.setattr(winapi, "SHOW_EVENT_NAME", name)
    return name


def test_show_event_round_trip(monkeypatch):
    """主实例建事件 -> 二次启动 SetEvent -> 主实例收到，链路必须通。"""
    _unique_event_name(monkeypatch)
    handle = winapi.create_show_event()
    assert handle
    try:
        assert not winapi.wait_show_request(handle, 50), "刚建好不该有信号"
        assert winapi.request_show_overview(), "信号应送达主实例的事件"
        assert winapi.wait_show_request(handle, 1000), "主实例应能等到这次唤窗"
        assert not winapi.wait_show_request(handle, 50), "自动重置：第二次等不到了"
    finally:
        winapi.close_show_event(handle)


def test_show_event_opened_by_other_handle(monkeypatch):
    """open_show_event 拿的是另一个句柄：证明事件是命名对象，不是私有的。"""
    _unique_event_name(monkeypatch)
    handle = winapi.create_show_event()
    try:
        other = winapi.open_show_event()
        try:
            assert other
        finally:
            winapi.close_show_event(other)
    finally:
        winapi.close_show_event(handle)


def test_request_show_overview_false_when_no_instance(monkeypatch):
    """没有实例在跑时信号发不出去：cli 据此回落到「已在运行」提示框。"""
    _unique_event_name(monkeypatch)
    assert winapi.request_show_overview() is False


def test_close_show_event_tolerates_none():
    """进程退出时的兜底调用，None 不能炸。"""
    winapi.close_show_event(None)
    assert True


def test_set_app_user_model_id_returns_bool():
    """设置失败只是任务栏分组不正常，不允许抛异常砸掉托盘启动。"""
    assert isinstance(winapi.set_app_user_model_id(), bool)


# ---------------------------------------------------------------- 窗口类图标（HICON）


def _read_hicon(handle: int, size):
    """把 HICON 画进 32bpp DIB 读回 RGBA（DrawIconEx 输出是预乘 alpha）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    user32.DrawIconEx.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
        ctypes.c_int, ctypes.c_int, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
    ]
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    try:
        bi = winapi.BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(winapi.BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = size[0], -size[1]
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        gdi32.SelectObject(mem, wintypes.HGDIOBJ(hbmp))
        user32.DrawIconEx(mem, 0, 0, wintypes.HICON(handle), size[0], size[1], 0, None, 3)
        raw = ctypes.string_at(bits, size[0] * size[1] * 4)
        gdi32.DeleteObject(wintypes.HGDIOBJ(hbmp))
        return Image.frombuffer("RGBA", size, raw, "raw", "BGRA", 0, 1)
    finally:
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def _depremul(px):
    """反预乘：读回 HICON 后必须还原再比（alpha=255 时该运算是恒等）。"""
    r, g, b, a = px
    if not a:
        return (0, 0, 0, 0)
    return (min(255, round(r * 255 / a)), min(255, round(g * 255 / a)),
            min(255, round(b * 255 / a)), a)


def test_icon_resource_bytes_layout():
    """资源字节布局：40 字节头 + BGRA XOR（自底向上）+ 全零 1bpp AND。"""
    img = Image.new("RGBA", (2, 3))
    img.putpixel((0, 2), (10, 20, 30, 255))      # 左下角应排进 XOR 第一格
    blob = winapi._icon_resource_bytes(img)
    head = winapi.BITMAPINFOHEADER.from_buffer_copy(blob[:40])
    assert head.biSize == 40 and head.biBitCount == 32 and head.biCompression == 0
    assert (head.biWidth, head.biHeight) == (2, 6)       # XOR + AND 上下摞
    assert head.biSizeImage == 2 * 3 * 4
    assert len(blob) == 40 + 2 * 3 * 4 + (2 + 31) // 32 * 4 * 3
    assert blob[40:44] == bytes((30, 20, 10, 255))       # BGRA 序
    assert set(blob[40 + 2 * 3 * 4:]) == {0}


def test_icon_handle_builds_handle_and_rejects_bad_input():
    """合法图像造出 HICON；空图 / 超大图 / 非图像一律 0，调用方据此回退。"""
    handle = winapi.icon_handle(Image.new("RGBA", (16, 16), (37, 99, 235, 255)))
    assert isinstance(handle, int) and handle > 0
    assert winapi.icon_handle(Image.new("RGBA", (0, 0))) == 0
    assert winapi.icon_handle(Image.new("RGBA", (300, 300))) == 0
    assert winapi.icon_handle(None) == 0


def test_icon_handle_round_trip_keeps_pixels_exact():
    """HICON 读回（反预乘）后不透明像素与原图逐通道一致。

    这条是标题栏 16px 发灰的回归护栏：走 Tk 的"大图重采样 + 预乘 alpha"链时，
    小图标不透明像素最大偏差到过 43；自造 HICON 必须 0 偏差。
    """
    img = Image.new("RGBA", (16, 16), (37, 99, 235, 255))
    handle = winapi.icon_handle(img)
    assert handle > 0
    back = _read_hicon(handle, (16, 16))
    for xy in ((0, 0), (8, 8), (15, 15)):
        assert back.getpixel(xy) == (37, 99, 235, 255)


def test_toplevel_hwnd_is_the_tk_parent_window():
    """winfo_id() 是 TkChild（类图标 0）；父窗口才是 TkTopLevel。"""
    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    root.update()        # TkTopLevel 包装窗口要先 update 才建出来
    try:
        hwnd = winapi.toplevel_hwnd(root)
        assert hwnd > 0 and hwnd != int(root.winfo_id())
        assert winapi.window_class_name(hwnd) == "TkTopLevel"
    finally:
        root.destroy()


def test_toplevel_hwnd_returns_zero_without_tk():
    assert winapi.toplevel_hwnd(object()) == 0


def test_set_window_class_icons_writes_both_indexes(monkeypatch):
    calls = []

    def fake_set(hwnd, index, value):
        calls.append((hwnd.value, index, value))       # argtypes 传进来是 c_void_p
        return 0        # 旧值本身可能为 0，不能拿返回值判成败

    monkeypatch.setattr(winapi.user32, "SetClassLongPtrW", fake_set)
    assert winapi.set_window_class_icons(4242, 111, 222) is True
    assert calls == [
        (4242, winapi.GCLP_HICON, 111), (4242, winapi.GCLP_HICONSM, 222)]


def test_set_window_class_icons_skips_zero_handles(monkeypatch):
    calls = []
    monkeypatch.setattr(winapi.user32, "SetClassLongPtrW",
                        lambda hwnd, index, value: calls.append((hwnd, index, value)) or 1)
    assert winapi.set_window_class_icons(4242, 0, 0) is True and calls == []
    assert winapi.set_window_class_icons(0, 111, 222) is False and calls == []
