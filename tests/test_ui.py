# -*- coding: utf-8 -*-
"""ui：托盘图标分档取色/绘制尺寸/缓存、概览与表格纯函数、无 tkinter 回退（不弹窗口）。

回退用例用 sys.modules["tkinter"]=None 触发 ImportError，并 monkeypatch message_box
记录调用，绝不弹出真实窗口。
"""
import sys
import threading
import time

import pytest

from PIL import ImageTk

from memguard import ui
from memguard.ui import (_fmt_bytes, _group_rows, _icon_color, _overview_text,
                          _table_rows, _top_grouped, make_icon,
                          show_advice_window, show_overview_window, show_top_window,
                          show_trend_window)

GREEN = (46, 160, 67, 255)
YELLOW = (214, 158, 46, 255)
RED = (207, 59, 54, 255)


@pytest.fixture
def clean_cache():
    """图标缓存/尺寸缓存在用例间隔离，避免相互污染。"""
    ui._ICON_CACHE.clear()
    ui._icon_size_cache = None
    yield
    ui._ICON_CACHE.clear()
    ui._icon_size_cache = None


def _use_tray_px(monkeypatch, px):
    monkeypatch.setattr(ui, "_tray_px", lambda: px)
    ui._icon_size_cache = None


def test_icon_size_follows_system_metric(monkeypatch, clean_cache):
    """按系统托盘真实像素绘制：Windows 已把 DPI 折算进 SM_CXSMICON。"""
    _use_tray_px(monkeypatch, 48)
    assert make_icon(50, 85).size == (48, 48)


@pytest.mark.parametrize("metric,expected", [
    (32, 32), (64, 64),             # 指标在可读区间内原样采用
    (16, 32), (0, 32),              # 太小或读不到（0）→ 下限，两位数字才看得清
    (256, 64),
])
def test_icon_size_clamped_to_readable_range(monkeypatch, metric, expected):
    _use_tray_px(monkeypatch, metric)
    assert ui._icon_size() == expected


def test_icon_size_measured_once(monkeypatch, clean_cache):
    """GetSystemMetrics 每轮刷新都读没意义，结果缓存。"""
    calls = []

    def count():
        calls.append(1)
        return 40
    monkeypatch.setattr(ui, "_tray_px", count)
    ui._icon_size_cache = None
    assert ui._icon_size() == 40 and ui._icon_size() == 40
    assert len(calls) == 1


def test_icon_cache_evicts_oldest_entry(monkeypatch, clean_cache):
    """到上限淘汰最旧的键，而不是整个清空（清空会让常用档位全部重绘一遍）。"""
    monkeypatch.setattr(ui, "_ICON_CACHE_MAX", 4)
    for pct in range(10):
        make_icon(pct, 85)
    assert len(ui._ICON_CACHE) == 4
    assert list(ui._ICON_CACHE)[:4] == [(str(p), GREEN) for p in range(6, 10)]
    assert make_icon(50, 85) is make_icon(50, 85)


def test_make_icon_cached_identity(clean_cache):
    """同一组合应复用同一图像对象（缓存命中）。"""
    assert make_icon(33, 85) is make_icon(33, 85)


def test_icon_color_bands():
    assert _icon_color(50, 85) == GREEN
    assert _icon_color(80, 85) == YELLOW
    assert _icon_color(90, 85) == RED


def test_icon_color_boundaries():
    assert _icon_color(85, 85) == RED       # 达到阈值即为红
    assert _icon_color(75, 85) == YELLOW    # 阈值前 10% 区间起点为黄


def test_icon_color_uses_raw_float():
    """回归：颜色必须按原始 float 判断，84.6% 属黄档，不能被 round 成 85% 的红档。"""
    assert _icon_color(84.6, 85) == YELLOW
    assert make_icon(84.6, 85) is not make_icon(85, 85)


def test_threshold_affects_cache_key():
    assert make_icon(80, 85) is not make_icon(80, 92)


def test_icon_text_is_drawn_and_centered(clean_cache):
    """回归：数字必须真的画在图内且大致居中。

    超采样版本曾把 bbox 又乘了一次倍数，文字被画到画布外，图标只剩一个色块——
    这种"看起来还在"的问题只能靠像素断言兜住。
    """
    img = make_icon(62, 85)
    px = img.load()
    # 抗锯齿后边缘是混色，只有字形核心接近纯白；用容差而不是严格相等
    ink = [(x, y) for y in range(img.size[1]) for x in range(img.size[0])
           if px[x, y][0] > 200 and px[x, y][1] > 200 and px[x, y][2] > 200 and px[x, y][3] > 200]
    assert ink, "图标里应该画出白色数字"
    xs, ys = [p[0] for p in ink], [p[1] for p in ink]
    size = img.size[0]
    assert abs((min(xs) + max(xs)) / 2 - size / 2) <= 2.5, "数字应在水平居中位置"
    # 字形 bbox 含上下留白，且绘制时有 -2px 的光学居中修正，故垂直容差放宽
    assert abs((min(ys) + max(ys)) / 2 - size / 2) <= 4.0, "数字应在垂直居中附近"


def test_icon_edges_are_anti_aliased(clean_cache):
    """超采样再缩回，边缘应出现半透明过渡，而不是 0/255 硬切。"""
    px = make_icon(50, 85).load()
    size = 32
    alphas = [px[x, 0][3] for x in range(size)] + [px[0, y][3] for y in range(size)]
    assert any(0 < a < 255 for a in alphas), "边缘应有抗锯齿过渡像素"
# -*- coding: utf-8 -*-
GB = 1024 ** 3


def _stub_sys(monkeypatch):
    """把内存/进程查询替换成确定值，让纯函数与回退测试不依赖真实系统。"""
    monkeypatch.setattr(ui, "get_mem", lambda: {
        "phys_pct": 55.0, "commit_pct": 60.0, "used_phys": 8 * GB, "total_phys": 16 * GB,
        "avail_phys": 8 * GB, "used_commit": 20 * GB, "total_commit": 32 * GB,
        "avail_commit": 12 * GB,
    })
    monkeypatch.setattr(ui, "top_processes_list",
                        lambda n: [("chrome.exe", 3 * GB, 1234)][:n])
    monkeypatch.setattr(ui, "top_processes", lambda n: "  1. chrome.exe 3.00GB")


def _capture_box(monkeypatch):
    """拦截 message_box，捕获 (title, text) 并用 Event 通知，绝不弹真实窗口。"""
    seen = {}

    def fake_box(title, text):
        seen["title"] = title
        seen["text"] = text
        seen["ev"].set()

    seen["ev"] = threading.Event()
    monkeypatch.setattr(ui, "message_box", fake_box)
    return seen


def _wait(predicate, timeout=3.0):
    """轮询等待 predicate() 为真（回退发生在 daemon 线程里，等待其收尾再断言）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------- 概览 / 表格纯函数


def test_group_rows_merges_same_name_processes():
    """同名多开合并成一行：内存求和、实例计数、PID 取最大。"""
    rows = [("a.exe", 2 * GB, 10), ("b.exe", 1 * GB, 20), ("a.exe", 3 * GB, 5)]
    assert _group_rows(rows) == [("a.exe", 5 * GB, 2, 10), ("b.exe", 1 * GB, 1, 20)]


def test_group_rows_sorted_by_total_desc():
    """合并后按合计内存降序，与各进程独立占用无关。"""
    rows = [("small.exe", 1 * GB, 1), ("chrome.exe", 1 * GB, 2),
            ("chrome.exe", 1 * GB, 3), ("mid.exe", 3 * GB, 4)]
    out = _group_rows(rows)
    assert [name for name, *_ in out] == ["mid.exe", "chrome.exe", "small.exe"]


def test_fmt_bytes_units():
    """表格口径：不足 1GB 用整数 MB，否则保留两位小数的 GB。"""
    assert _fmt_bytes(512 * 1024 ** 2) == "512MB"
    assert _fmt_bytes(0) == "0MB"
    assert _fmt_bytes(2 * GB) == "2.00GB"
    assert _fmt_bytes(1536 * 1024 ** 2) == "1.50GB"


def test_table_rows_marks_multi_instance():
    """多实例组名带 ×N 标记，单实例保持原名；积分比按合计内存算。"""
    groups = _group_rows([("a.exe", 3 * GB, 10), ("a.exe", 2 * GB, 5), ("b.exe", 1 * GB, 20)])
    rows = _table_rows(groups, 16 * GB)
    assert rows[0] == ("a.exe ×2", "5.00GB", "31.2%", 10)
    assert rows[1][0] == "b.exe"


def test_table_rows_zero_total_shows_dash():
    """总物理为 0 时占比显示 '-'，而不是 ZeroDivisionError。"""
    rows = _table_rows([("a.exe", 1 * GB, 1, 1)], 0)
    assert rows[0][2] == "-"


def test_top_grouped_oversamples_before_merge(monkeypatch):
    """先把 n*4 进程取回来再合并，避免同名多开把名额吃光后凑不满 N 组。"""
    seen = {}
    rows = [("chrome.exe", 4 * GB, 2), ("chrome.exe", 4 * GB, 5),
            ("chrome.exe", 4 * GB, 9), ("big.exe", 9 * GB, 99)]

    def fake_top(n):
        seen["n"] = n
        return sorted(rows, key=lambda r: r[1], reverse=True)[:n]

    monkeypatch.setattr(ui, "top_processes_list", fake_top)
    out = _top_grouped(2)
    assert seen["n"] == max(2 * 4, 2 + 10) == 12, "为凑满 N 组先按超采样量取进程"
    # 三实例 4GB 合并成 12GB，超过单进程 big.exe(9GB)，故合并后排第一
    assert out[0] == ("chrome.exe", 12 * GB, 3, 9)
    assert [name for name, *_ in out] == ["chrome.exe", "big.exe"]


def test_overview_text_includes_summary_and_top(monkeypatch):
    """无 tkinter 回退文案：头部两条内存概况 + Top 列表（可被测试稳定断言）。"""
    _stub_sys(monkeypatch)
    text = _overview_text()
    assert "物理内存" in text and "提交内存" in text
    assert "可用 物理" in text and "可用 提交" in text
    assert "chrome.exe" in text


# ------------------------------------------------ 无 tkinter 的回退（不弹窗）


def test_overview_falls_back_to_messagebox_without_tkinter(monkeypatch):
    monkeypatch.setitem(sys.modules, "tkinter", None)
    _stub_sys(monkeypatch)
    box = _capture_box(monkeypatch)
    assert ui._overview_window_open is False
    show_overview_window({"clean": lambda: None})
    assert _wait(box["ev"].is_set)
    assert box["title"] == "MemGuard - 内存概览"
    assert "物理内存" in box["text"] and "chrome.exe" in box["text"]
    assert ui._overview_window_open is False


def test_top_falls_back_to_messagebox_without_tkinter(monkeypatch):
    monkeypatch.setitem(sys.modules, "tkinter", None)
    _stub_sys(monkeypatch)
    box = _capture_box(monkeypatch)
    show_top_window()
    assert _wait(box["ev"].is_set)
    assert box["title"] == "内存占用 Top10"
    assert "chrome.exe" in box["text"]
    assert ui._top_window_open is False


def test_advice_falls_back_to_messagebox_without_tkinter(monkeypatch):
    monkeypatch.setitem(sys.modules, "tkinter", None)
    monkeypatch.setattr(ui, "analyze",
                        lambda cfg: [{"level": "tip", "title": "标题", "text": "正文"}])
    box = _capture_box(monkeypatch)
    show_advice_window({})
    assert _wait(box["ev"].is_set)
    assert box["title"] == "MemGuard - 优化建议"
    assert "标题" in box["text"] and "正文" in box["text"]
    assert ui._advice_window_open is False


def test_trend_returns_silently_without_tkinter(monkeypatch):
    """趋势没有可回退的文本形态，无 tkinter 时静默返回并复位开关（不弹窗）。"""
    monkeypatch.setitem(sys.modules, "tkinter", None)
    boxes = []
    monkeypatch.setattr(ui, "message_box", lambda t, x: boxes.append(t))
    assert ui._trend_window_open is False
    show_trend_window(lambda: [])
    # show_trend_window 先把开关置 True 再起 worker；import 失败由 worker 复位，需轮询等它
    assert _wait(lambda: ui._trend_window_open is False), "无 tkinter 时应复位开关"
    assert boxes == [], "趋势无文本回退，不应弹任何窗口"


class _FakePhoto:
    """顶替 PIL.ImageTk.PhotoImage：测试里不建真 Tk 根窗口也能走完整挂载流程。"""

    def __init__(self, image):
        self.image = image


class _FakeRoot:
    """记录 iconphoto 调用的假窗口，用来验证图标真的挂上去了。"""

    def __init__(self):
        self.calls = []

    def iconphoto(self, default, *photos):
        self.calls.append((default, photos))

    def update_idletasks(self):
        pass


class _BoomRoot(_FakeRoot):

    def iconphoto(self, default, *photos):
        raise RuntimeError("窗口炸了")


def test_app_icon_sizes_are_cached(monkeypatch):
    """按尺寸取图、按尺寸缓存：同一尺寸拿同一张，避免每次挂窗口重绘。"""
    monkeypatch.setattr(ui, "_ICON_SS", 4)
    ui._APP_ICON_CACHE.clear()
    first = ui.app_icon(16)
    assert first.size == (16, 16)
    assert first.mode == "RGBA"
    assert ui.app_icon(16) is first, "同尺寸应命中缓存"
    assert ui.app_icon(32).size == (32, 32)
    assert [ui.app_icon(s).size for s in ui._APP_ICON_SIZES] == [(16, 16), (32, 32), (64, 64)]
    ui._APP_ICON_CACHE.clear()


def test_app_icon_looks_like_the_brand_icon():
    """圆角方 + 蓝底 + 透明四角：画成方块或纯色就说明比例抄错了。"""
    img = ui.app_icon(16)
    assert img.getpixel((0, 0)) == (0, 0, 0, 0), "四角必须透明（圆角）"
    body = img.getpixel((2, 8))
    assert body[3] == 255
    assert body[2] > body[0] and body[2] > 200, f"底色应为品牌蓝，实际 {body}"


def test_apply_window_icon_installs_all_sizes(monkeypatch):
    """三个尺寸一起交给 iconphoto，且 Python 侧留引用——否则 GC 后任务栏按钮变空白。"""
    monkeypatch.setattr(ImageTk, "PhotoImage", _FakePhoto)
    root = _FakeRoot()
    ui.apply_window_icon(root)
    (default, photos), = root.calls
    assert default is True, "default=True 让已打开的窗口也立刻换图标"
    assert [p.image.size for p in photos] == [(16, 16), (32, 32), (64, 64)]
    # iconphoto 的 *photos 收成元组，ui 侧存的是 list：比内容同一性即可
    assert list(root._memguard_icon_photos) == list(photos)
    assert root._memguard_icon_photos[0] is photos[0]


def test_apply_window_icon_never_breaks_the_window(monkeypatch):
    """挂图标失败（老 Tk / 无 PIL）必须静默跳过，不能连带窗口打不开。"""
    monkeypatch.setattr(ImageTk, "PhotoImage", _FakePhoto)
    ui.apply_window_icon(_BoomRoot())          # 不应抛出
    monkeypatch.setattr(ImageTk, "PhotoImage", _boom_photo())
    ui.apply_window_icon(_FakeRoot())          # PhotoImage 构造失败也不应抛出


def _boom_photo():
    def make(image):
        raise RuntimeError("no Tk")
    return make


class _RaiseRoot:
    """记录 deiconify / lift / focus_force 的假窗口：验证唤窗真的会亮出来。"""

    def __init__(self, exists=True):
        self.calls = []
        self._exists = exists

    def deiconify(self):
        self.calls.append("deiconify")

    def update_idletasks(self):
        self.calls.append("update_idletasks")

    def lift(self):
        self.calls.append("lift")

    def focus_force(self):
        self.calls.append("focus_force")

    def winfo_exists(self):
        return self._exists


def test_raise_window_restores_and_focuses():
    """最小化、被压住的窗口要三件套救回来：deiconify + lift + focus_force。"""
    root = _RaiseRoot()
    ui._raise_window(root)
    assert root.calls == ["deiconify", "update_idletasks", "lift", "focus_force"]


def test_raise_window_never_raises_on_dead_window():
    """窗口刚关掉时 deiconify 会抛：唤窗失败不能把托盘线程带崩。"""

    class _DeadRoot(_RaiseRoot):

        def deiconify(self):
            raise RuntimeError("bad window path name")

    ui._raise_window(_DeadRoot())          # 不应抛出


def test_show_overview_window_raises_existing_window(monkeypatch):
    """概览窗已开着时再点（托盘左键 / 任务栏二次启动）：不再静默返回，而是发唤窗请求。"""
    monkeypatch.setattr(ui, "_overview_window_open", True)
    ui._overview_raise.clear()
    ui.show_overview_window({})
    assert ui._overview_raise.is_set(), "已开着的窗口应被要求亮出来"
    ui._overview_raise.clear()


# ---------------------------------------------------------------- 窗口类图标覆盖


def test_apply_window_icon_overrides_class_icons(monkeypatch):
    """iconphoto 之后必须自造 HICON 覆盖类图标（Tk 的重采样小图标发灰）。"""
    seen = {}
    monkeypatch.setattr(ImageTk, "PhotoImage", _FakePhoto)
    monkeypatch.setattr(ui, "toplevel_hwnd", lambda root: 4321)
    monkeypatch.setattr(
        ui, "set_window_class_icons",
        lambda hwnd, big, small: seen.update(hwnd=hwnd, big=big, small=small) or True)
    ui.apply_window_icon(_FakeRoot())
    assert seen["hwnd"] == 4321
    assert isinstance(seen["big"], int) and seen["big"] > 0
    assert isinstance(seen["small"], int) and seen["small"] > 0


def test_override_icons_are_cached_per_size(monkeypatch):
    """同尺寸 HICON 只造一次：四个 Tk 窗口共享同一个类，重复造没意义。"""
    built = []
    monkeypatch.setattr(ImageTk, "PhotoImage", _FakePhoto)
    monkeypatch.setattr(ui, "toplevel_hwnd", lambda root: 4321)
    monkeypatch.setattr(ui, "set_window_class_icons", lambda *args: True)
    monkeypatch.setattr(ui, "icon_handle",
                        lambda img: built.append(img.size[0]) or 1000 + img.size[0])
    ui._CLASS_ICON_CACHE.clear()
    ui.apply_window_icon(_FakeRoot())
    ui.apply_window_icon(_FakeRoot())
    assert built == [32, 16]
    assert ui._CLASS_ICON_CACHE == {32: 1032, 16: 1016}
    ui._CLASS_ICON_CACHE.clear()


def test_override_failure_keeps_tk_icons(monkeypatch):
    """覆盖失败（SetClassLongPtr 炸）必须静默：Tk 那份图标依旧在，窗口照常。"""
    monkeypatch.setattr(ImageTk, "PhotoImage", _FakePhoto)
    monkeypatch.setattr(ui, "toplevel_hwnd", lambda root: 4321)
    monkeypatch.setattr(ui, "icon_handle", lambda img: 1)

    def boom(*args):
        raise RuntimeError("SetClassLongPtr failed")

    monkeypatch.setattr(ui, "set_window_class_icons", boom)
    ui.apply_window_icon(_FakeRoot())          # 不应抛出
