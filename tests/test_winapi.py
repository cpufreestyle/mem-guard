# -*- coding: utf-8 -*-
"""winapi：内存读取结构、权限查询缓存、单实例锁、进程快照快路径。"""
import ctypes
import ctypes.wintypes as wintypes
import os

import types
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



# ---------------------------------------------------------------- 托盘常驻（IsPromoted）

_PY_EXE = r"C:\Python313\python.exe"
_OTHER_EXE = r"C:\Program Files\OneDrive\OneDrive.exe"


class _FakeWinreg:
    """假注册表层：只托盘条目这一层，用来离线验证 IsPromoted 的读写。

    handle 用路径元组表示，OpenKey 的嵌套层级即注册表层级；条目树形如
    {条目 key: {值名: 值}}，与真实 NotifyIconSettings 下的形状保持一致。
    """

    HKEY_CURRENT_USER = "HKCU"

    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    KEY_WOW64_64KEY = 0x0100
    KEY_WOW64_32KEY = 0x0200
    REG_DWORD = 4

    def __init__(self, tree=None, fail_open=False):
        self.tree = dict(tree or {})
        self.writes = []          # 已发生的写入：[条目 key, 值名, 值]
        self.opened = 0
        self.closed = 0
        self.fail_open = fail_open      # True 时 NotifyIconSettings 根本打不开

    def OpenKey(self, root, sub="", reserved=0, access=KEY_READ):
        if self.fail_open and root == self.HKEY_CURRENT_USER:
            raise OSError(5, "access denied")
        base = (root,) if isinstance(root, str) else tuple(root)
        path = base + tuple(p for p in sub.split(chr(92)) if p)
        if len(path) > 3 and path[3] not in self.tree:
            raise OSError(2, "not found")
        self.opened += 1
        return path

    def CloseKey(self, key):
        self.closed += 1

    def EnumKey(self, key, index):
        names = list(self.tree)
        if index >= len(names):
            raise OSError(259, "no more items")
        return names[index]

    def QueryValueEx(self, key, name):
        values = self.tree.get(key[-1], {})
        if name not in values:
            raise OSError(2, "value not found")
        return values[name], self.REG_DWORD

    def SetValueEx(self, key, name, reserved, typ, value):
        self.writes.append([key[-1], name, value])
        self.tree.setdefault(key[-1], {})[name] = value


def _pin_reg(monkeypatch, tree=None, **kw):
    fake = _FakeWinreg(tree, **kw)
    monkeypatch.setattr(winapi, "winreg", fake)
    return fake


def test_find_tray_entries_matches_only_own_executable(monkeypatch):
    """冻结版 / dev 实例 / OneDrive 各拿各的条目，互不干扰。"""
    _pin_reg(monkeypatch, {
        "key-a": {"ExecutablePath": _PY_EXE, "UID": 0},
        "key-b": {"ExecutablePath": _OTHER_EXE, "UID": 501, "IsPromoted": 1},
    })
    rows = winapi.find_tray_entries(_PY_EXE)
    assert [r["key"] for r in rows] == ["key-a"]
    assert rows[0]["uid"] == 0
    assert rows[0]["promoted"] is None


def test_find_tray_entries_matches_path_case_insensitively(monkeypatch):
    """注册表里存的路径大小写不保证，匹配必须不区分大小写。"""
    _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE.lower(), "UID": 0}})
    assert [r["key"] for r in winapi.find_tray_entries(_PY_EXE)] == ["key-a"]


def test_read_tray_entry_returns_values_and_closes_every_key(monkeypatch):
    fake = _pin_reg(monkeypatch,
                    {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0, "IsPromoted": 1}})
    row = winapi.read_tray_entry("key-a")
    assert row == {"key": "key-a", "exe": _PY_EXE, "uid": 0, "promoted": 1}
    assert fake.closed == fake.opened, "打开过的键必须都关上"


def test_read_tray_entry_missing_key_returns_none(monkeypatch):
    _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0}})
    assert winapi.read_tray_entry("nope") is None


def test_ensure_tray_promoted_writes_is_promoted(monkeypatch):
    """未常驻的条目补写 IsPromoted=1，且只写自己那一条。"""
    fake = _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0}})
    assert winapi.ensure_tray_promoted(_PY_EXE) == "updated"
    assert fake.writes == [["key-a", "IsPromoted", 1]]


def test_ensure_tray_promoted_already_is_idempotent(monkeypatch):
    fake = _pin_reg(monkeypatch,
                    {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0, "IsPromoted": 1}})
    assert winapi.ensure_tray_promoted(_PY_EXE) == "already"
    assert fake.writes == []


def test_ensure_tray_promoted_clears_zero_back_to_one(monkeypatch):
    """IsPromoted=0 与缺失等价：同样要补写。"""
    fake = _pin_reg(monkeypatch,
                    {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0, "IsPromoted": 0}})
    assert winapi.ensure_tray_promoted(_PY_EXE) == "updated"
    assert fake.writes == [["key-a", "IsPromoted", 1]]


def test_ensure_tray_promoted_reports_missing_entry(monkeypatch):
    """Explorer 还没登记本程序时返回 missing，绝不凭空写。"""
    fake = _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _OTHER_EXE, "UID": 501}})
    assert winapi.ensure_tray_promoted(_PY_EXE) == "missing"
    assert fake.writes == []


def test_notify_icon_data_layout_is_what_shell_expects():
    """64 位下 cbSize 必须是 NOTIFYICONDATAW 的真实尺寸，否则 Shell_NotifyIconW 拒收。"""
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("只钉 64 位布局")
    d = winapi.NOTIFYICONDATAW
    assert ctypes.sizeof(d) == 952
    assert (d.cbSize.offset, d.hWnd.offset) == (0, 8)
    assert (d.uID.offset, d.uFlags.offset) == (16, 20)
    assert (d.hIcon.offset, d.szTip.offset) == (32, 40)


def test_ensure_tray_promoted_adds_placeholder_when_entry_missing(monkeypatch):
    """首装：条目只能由 Explorer 建，所以先 NIM_ADD 补占位图标，再写 IsPromoted。"""
    fake = _pin_reg(monkeypatch, {})
    calls = []

    def _add():
        calls.append("add")
        fake.tree["key-a"] = {"ExecutablePath": _PY_EXE, "UID": 0}
        return True

    monkeypatch.setattr(winapi, "register_placeholder_icon", _add)
    assert winapi.ensure_tray_promoted(_PY_EXE, allow_add=True) == "updated"
    assert calls == ["add"], "条目缺失时必须先补占位图标"
    assert fake.writes == [["key-a", "IsPromoted", 1]]


def test_ensure_tray_promoted_without_allow_add_never_adds(monkeypatch):
    """没开 allow_add 就不该凭空 NIM_ADD —— 托盘进程自己就有图标。"""
    fake = _pin_reg(monkeypatch, {})
    calls = []
    monkeypatch.setattr(winapi, "register_placeholder_icon",
                        lambda: calls.append("add") or True)
    assert winapi.ensure_tray_promoted(_PY_EXE) == "missing"
    assert calls == []
    assert fake.writes == []


def test_ensure_tray_promoted_waits_for_the_entry(monkeypatch):
    """pystray 登记有延迟：wait_sec 内轮询到条目就照常写。"""
    fake = _pin_reg(monkeypatch, {})
    real_find = winapi.find_tray_entries
    seen = {"n": 0}

    def _find(path=None):
        seen["n"] += 1
        if seen["n"] >= 3:
            fake.tree["key-a"] = {"ExecutablePath": _PY_EXE, "UID": 0}
        return real_find(path)

    monkeypatch.setattr(winapi, "find_tray_entries", _find)
    assert winapi.ensure_tray_promoted(_PY_EXE, wait_sec=5.0) == "updated"
    assert fake.writes == [["key-a", "IsPromoted", 1]]


def test_ensure_tray_promoted_wait_gives_up(monkeypatch):
    """等不到就认 missing，一个字都不写。"""
    fake = _pin_reg(monkeypatch, {})
    assert winapi.ensure_tray_promoted(_PY_EXE, wait_sec=0.05) == "missing"
    assert fake.writes == []


def test_ensure_tray_promoted_only_writes_own_uid(monkeypatch):
    """同一个 exe 可能残留别的 uid 的旧条目：只写 pystray 自己那枚。"""
    fake = _pin_reg(monkeypatch, {
        "key-own": {"ExecutablePath": _PY_EXE, "UID": winapi.TRAY_OWN_UID},
        "key-old": {"ExecutablePath": _PY_EXE, "UID": 7},
        "key-guid": {"ExecutablePath": _PY_EXE},
    })
    assert winapi.ensure_tray_promoted(_PY_EXE) == "updated"
    assert fake.writes == [["key-own", "IsPromoted", 1]]


def test_ensure_tray_promoted_falls_back_when_no_uid_matches(monkeypatch):
    """一条 uid 都对不上时退回全部，别因为字段缺失把自己漏掉。"""
    fake = _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE, "UID": 9}})
    assert winapi.ensure_tray_promoted(_PY_EXE) == "updated"
    assert fake.writes == [["key-a", "IsPromoted", 1]]


def test_register_placeholder_icon_adds_then_deletes(monkeypatch):
    """占位图标登记完立刻撤掉，只把条目留给注册表。"""
    ops = []
    seen = {"n": 0}
    monkeypatch.setattr(winapi, "_probe_hwnd", lambda: 4242)

    def _find(path=None):
        seen["n"] += 1
        return [{"key": "key-a"}] if seen["n"] >= 2 else []

    monkeypatch.setattr(winapi, "find_tray_entries", _find)
    monkeypatch.setattr(winapi, "shell32", types.SimpleNamespace(
        Shell_NotifyIconW=lambda msg, nid: ops.append(msg) or True))
    monkeypatch.setattr(winapi.user32, "DestroyWindow",
                        lambda hwnd: ops.append("destroy"))
    assert winapi.register_placeholder_icon(timeout_sec=5.0) is True
    assert ops == [winapi.NIM_ADD, winapi.NIM_DELETE, "destroy"]


def test_register_placeholder_icon_false_when_shell_refuses(monkeypatch):
    """Shell_NotifyIconW 直接拒绝：报 False，让上层回到 missing。"""
    ops = []
    monkeypatch.setattr(winapi, "_probe_hwnd", lambda: 4242)
    monkeypatch.setattr(winapi, "shell32", types.SimpleNamespace(
        Shell_NotifyIconW=lambda msg, nid: False))
    monkeypatch.setattr(winapi.user32, "DestroyWindow",
                        lambda hwnd: ops.append("destroy"))
    assert winapi.register_placeholder_icon() is False
    assert ops == ["destroy"], "登记没成功也一样要收窗口"


def test_register_placeholder_icon_false_when_entry_never_shows_up(monkeypatch):
    """Explorer 建不出条目（超时）：False，上层继续报 missing。"""
    ops = []
    monkeypatch.setattr(winapi, "_probe_hwnd", lambda: 4242)
    monkeypatch.setattr(winapi, "find_tray_entries", lambda path=None: [])
    monkeypatch.setattr(winapi, "shell32", types.SimpleNamespace(
        Shell_NotifyIconW=lambda msg, nid: ops.append(msg) or True))
    monkeypatch.setattr(winapi.user32, "DestroyWindow", lambda hwnd: None)
    assert winapi.register_placeholder_icon(timeout_sec=0.05) is False
    assert ops == [winapi.NIM_ADD, winapi.NIM_DELETE], "撤图标不能省"


def test_register_placeholder_icon_false_without_probe_window(monkeypatch):
    """建不出宿主窗口就没有 NIM_ADD 可言。"""
    monkeypatch.setattr(winapi, "_probe_hwnd", lambda: 0)
    assert winapi.register_placeholder_icon() is False


def test_register_placeholder_icon_false_without_winreg(monkeypatch):
    monkeypatch.setattr(winapi, "winreg", None)
    assert winapi.register_placeholder_icon() is False


def test_probe_hwnd_registers_class_once(monkeypatch):
    """窗口类只注册一次，第二次直接 CreateWindowExW。"""
    ops = []
    monkeypatch.setattr(winapi, "_PROBE_CLASS_READY", False)
    monkeypatch.setattr(winapi.kernel32, "GetModuleHandleW",
                        lambda name: ops.append("mod") or 777)
    monkeypatch.setattr(winapi.user32, "RegisterClassExW",
                        lambda wc: ops.append("class") or 1)
    monkeypatch.setattr(winapi.user32, "CreateWindowExW",
                        lambda *a: ops.append("window") or 4242)
    assert winapi._probe_hwnd() == 4242
    assert ops == ["mod", "class", "window"]
    assert winapi._probe_hwnd() == 4242
    assert ops == ["mod", "class", "window", "mod", "window"]


def test_probe_hwnd_tolerates_class_already_exists(monkeypatch):
    """同类已被注册过不算失败（ERROR_CLASS_ALREADY_EXISTS=1410）。"""
    monkeypatch.setattr(winapi, "_PROBE_CLASS_READY", False)
    monkeypatch.setattr(winapi.kernel32, "GetModuleHandleW", lambda name: 777)
    monkeypatch.setattr(winapi.user32, "RegisterClassExW", lambda wc: 0)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1410)
    monkeypatch.setattr(winapi.user32, "CreateWindowExW", lambda *a: 4242)
    assert winapi._probe_hwnd() == 4242


def test_probe_hwnd_zero_when_class_registration_fails(monkeypatch):
    """注册不上类就放弃，别留半截状态。"""
    monkeypatch.setattr(winapi, "_PROBE_CLASS_READY", False)
    monkeypatch.setattr(winapi.kernel32, "GetModuleHandleW", lambda name: 777)
    monkeypatch.setattr(winapi.user32, "RegisterClassExW", lambda wc: 0)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1413)
    assert winapi._probe_hwnd() == 0


def test_pin_tray_to_corner_adds_entry_when_missing(monkeypatch):
    """首装：--pin-tray 常在程序还没起时跑，照样要拿得到条目 key。"""
    fake = _pin_reg(monkeypatch, {})
    calls = []
    monkeypatch.setattr(winapi, "restart_explorer",
                        lambda: calls.append("restart") or 32500)

    def _add():
        fake.tree["key-a"] = {"ExecutablePath": _PY_EXE, "UID": 0}
        return True

    monkeypatch.setattr(winapi, "register_placeholder_icon", _add)
    assert winapi.pin_tray_to_corner(_PY_EXE) == "pinned"
    assert calls == ["restart"]
    assert fake.writes == [["key-a", "IsPromoted", 1]]


def test_pin_tray_to_corner_without_add_reports_missing(monkeypatch):
    """allow_add=False 时不补占位图标，安分报 missing。"""
    fake = _pin_reg(monkeypatch, {})
    calls = []
    monkeypatch.setattr(winapi, "register_placeholder_icon",
                        lambda: calls.append("add") or True)
    monkeypatch.setattr(winapi, "restart_explorer", lambda: None)
    assert winapi.pin_tray_to_corner(_PY_EXE, allow_add=False) == "missing"
    assert calls == []
    assert fake.writes == []


def test_set_tray_promoted_false_when_key_absent(monkeypatch):
    fake = _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0}})
    assert winapi.set_tray_promoted("nope") is False
    assert fake.writes == []


def test_registry_unavailable_degrades_to_missing(monkeypatch):
    """注册表打不开时只当没有条目，不抛异常、不写任何东西。"""
    fake = _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0}},
                    fail_open=True)
    assert winapi.ensure_tray_promoted(_PY_EXE) == "missing"
    assert fake.writes == []


def test_without_winreg_everything_reports_unsupported(monkeypatch):
    """非 Windows 兜底：winreg 为 None 时全线安全短路。"""
    monkeypatch.setattr(winapi, "winreg", None)
    assert winapi.find_tray_entries(_PY_EXE) == []
    assert winapi.read_tray_entry("key-a") is None
    assert winapi.set_tray_promoted("key-a") is False
    assert winapi.ensure_tray_promoted(_PY_EXE) == "unsupported"
    assert winapi.pin_tray_to_corner(_PY_EXE) == "unsupported"


def test_pin_tray_to_corner_restarts_explorer_after_write(monkeypatch):
    fake = _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0}})
    calls = []
    monkeypatch.setattr(winapi, "restart_explorer",
                        lambda: calls.append("restart") or 32500)
    assert winapi.pin_tray_to_corner(_PY_EXE) == "pinned"
    assert calls == ["restart"], "写完注册表必须重启外壳才生效"
    assert fake.writes == [["key-a", "IsPromoted", 1]]


def test_pin_tray_to_corner_reports_restart_failure(monkeypatch):
    _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0}})
    monkeypatch.setattr(winapi, "restart_explorer", lambda: None)
    assert winapi.pin_tray_to_corner(_PY_EXE) == "restart_failed"


def test_pin_tray_to_corner_without_restart_only_writes(monkeypatch):
    fake = _pin_reg(monkeypatch, {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0}})
    assert winapi.pin_tray_to_corner(_PY_EXE, restart=False) == "updated"
    assert fake.writes == [["key-a", "IsPromoted", 1]]


def test_pin_tray_to_corner_skips_restart_when_already(monkeypatch):
    _pin_reg(monkeypatch,
             {"key-a": {"ExecutablePath": _PY_EXE, "UID": 0, "IsPromoted": 1}})
    calls = []
    monkeypatch.setattr(winapi, "restart_explorer",
                        lambda: calls.append("restart") or 32500)
    assert winapi.pin_tray_to_corner(_PY_EXE) == "already"
    assert calls == [], "已常驻就不该再闪一次任务栏"


def test_explorer_pids_filters_snapshot(monkeypatch):
    monkeypatch.setattr(winapi, "_snapshot_processes",
                        lambda: [("explorer.exe", 32500), ("python.exe", 28104),
                                 ("EXPLORER.EXE", 777), ("System", 4)])
    assert winapi._explorer_pids() == [32500, 777]


def test_restart_explorer_without_shell_is_noop(monkeypatch):
    """外壳进程列表为空时直接返回 None，不崩也不空等。"""
    monkeypatch.setattr(winapi, "_explorer_pids", lambda: [])
    assert winapi.restart_explorer(timeout_sec=0.05) is None


def test_restart_explorer_kills_and_reports_fresh_pid(monkeypatch):
    """杀旧外壳 -> 轮询到新外壳：返回新 pid。"""
    state = {"pids": [32500]}
    monkeypatch.setattr(winapi, "_explorer_pids", lambda: list(state["pids"]))
    opened, killed = [], []

    def fake_open(rights, inherit, pid):
        opened.append(pid)
        return 99

    def fake_term(handle, code):
        killed.append(handle)
        state["pids"] = [41000]        # 系统重拉了一个新外壳
        return True

    monkeypatch.setattr(winapi, "kernel32", types.SimpleNamespace(
        OpenProcess=fake_open, TerminateProcess=fake_term,
        CloseHandle=lambda handle: True))
    assert winapi.restart_explorer(timeout_sec=2.0) == 41000
    assert opened == [32500]
    assert killed == [99]


def test_restart_explorer_gives_up_after_timeout(monkeypatch):
    """外壳没被拉起来：等满超时返回 None，而不是死等。"""


def test_notify_rect_reads_corner_slot(monkeypatch):
    """Shell_NotifyIconGetRect 的 (hwnd, uID) 拿到的是图标所在那一格。"""

    def fake_get(nii, rect):
        target = getattr(rect, "_obj", rect)     # byref 传进来的是 CArgObject
        target.left, target.top = 1546, 1020
        target.right, target.bottom = 1586, 1080
        return winapi._S_OK

    monkeypatch.setattr(winapi, "shell32",
                        types.SimpleNamespace(Shell_NotifyIconGetRect=fake_get))
    assert winapi._notify_rect_for_hwnd(4242) == (1546, 1020, 1586, 1080)


def test_notify_rect_none_when_icon_not_registered(monkeypatch):
    """未登记时返回 E_FAIL：读不到矩形，不能当成 (0,0,0,0)。"""
    monkeypatch.setattr(winapi, "shell32", types.SimpleNamespace(
        Shell_NotifyIconGetRect=lambda nii, rect: -2147467259))
    assert winapi._notify_rect_for_hwnd(4242) is None


def test_notify_icon_rect_picks_first_registered_window(monkeypatch):
    """进程可能残留多张旧窗口，逐个探测，取第一张登记过的。"""
    rects = {101: None, 202: (1546, 1020, 1586, 1080), 303: (1586, 1020, 1626, 1080)}
    monkeypatch.setattr(winapi, "_tray_windows", lambda pid: [101, 202, 303])
    monkeypatch.setattr(winapi, "_notify_rect_for_hwnd",
                        lambda hwnd, uid=winapi.TRAY_OWN_UID: rects[hwnd])
    assert winapi.notify_icon_rect(4242) == (202, rects[202])


def test_notify_icon_rect_none_without_registered_icon(monkeypatch):
    monkeypatch.setattr(winapi, "_tray_windows", lambda pid: [101])
    monkeypatch.setattr(winapi, "_notify_rect_for_hwnd",
                        lambda hwnd, uid=winapi.TRAY_OWN_UID: None)
    assert winapi.notify_icon_rect(4242) is None