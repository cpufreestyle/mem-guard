# -*- coding: utf-8 -*-
"""
界面层：托盘图标绘制、气泡提示、Top10 / 趋势 / 优化建议窗口。

只负责「怎么展示」，不做业务编排：
    - 进程遍历 / 清理编排见 clean.py
    - 建议生成见 advisor.py
    - 菜单编排与主循环见 tray.py

依赖 config / winapi / clean / advisor；不依赖 pystray（图标与窗口本身不需要托盘后端）。
"""
from __future__ import annotations

import threading

from PIL import Image, ImageDraw, ImageFont

from .advisor import advice_actions, analyze, format_advice
from .clean import top_processes, top_processes_list
from .config import gb, log, update_config
from .winapi import (SM_CXSMICON, get_mem, icon_handle, set_window_class_icons,
                     toplevel_hwnd, user32)

# ---------------------------------------------------------------- 托盘图标


# 绘制边长取系统托盘指标（Windows 已按当前 DPI 折算），并夹到可读区间：
# 小于 32px 两位数字糊成一团，大于 64px 只是白占内存。
_ICON_SIZE_MIN = 32
_ICON_SIZE_MAX = 64
_ICON_SIZE_FALLBACK = 32
_ICON_FONT_RATIO = 0.4
# 抗锯齿超采样倍数：按 4 倍尺寸绘制再缩回，换来平滑边缘（成本只有几毫秒且已缓存）
_ICON_SS = 4
_FONT_CACHE: dict = {}
# 图标内容只取决于「显示文本 + 颜色档」，故按该组合缓存；分桶后组合数很少，
# 无需每轮刷新都重新绘制并重新加载字体文件。
_ICON_CACHE: dict = {}
_ICON_CACHE_MAX = 128
_icon_size_cache: int | None = None


def _tray_px() -> int:
    """系统托盘图标的像素边长；读不到返回 0。"""
    try:
        return int(user32.GetSystemMetrics(SM_CXSMICON))
    except Exception:
        return 0


def _icon_size() -> int:
    """绘制边长：托盘指标夹到 [MIN, MAX]，进程内只实测一次。"""
    global _icon_size_cache
    if _icon_size_cache is None:
        px = _tray_px() or _ICON_SIZE_FALLBACK
        _icon_size_cache = max(_ICON_SIZE_MIN, min(_ICON_SIZE_MAX, px))
    return _icon_size_cache


def _icon_font(scale: int = 1) -> ImageFont.FreeTypeFont:
    """按绘制边长取图标字体（同尺寸只加载一次，避免每帧重读字体文件）。

    scale>1 时按超采样倍数放大字号，配合 _render_icon 的抗锯齿绘制。
    """
    px = max(9, round(_icon_size() * _ICON_FONT_RATIO * scale))
    font = _FONT_CACHE.get(px)
    if font is None:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", px)
        except Exception:
            font = ImageFont.load_default()
        _FONT_CACHE[px] = font
    return font


def _icon_color(pct: float, threshold: float) -> tuple:
    """按物理使用率与阈值分档取色：红=达到阈值，黄=阈值前 10% 区间，绿=更宽松。"""
    warn = max(threshold - 10.0, 1.0)
    if pct < warn:
        return (46, 160, 67, 255)     # 绿
    if pct < threshold:
        return (214, 158, 46, 255)    # 黄
    return (207, 59, 54, 255)         # 红


def make_icon(pct: float, threshold: float = 85.0) -> Image.Image:
    """绘制托盘图标；相同（文本, 颜色）组合直接复用缓存图像（只读共享）。"""
    text = str(int(round(pct)))
    key = (text, _icon_color(pct, threshold))
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached
    img = _render_icon(text, key[1])
    while len(_ICON_CACHE) >= _ICON_CACHE_MAX:
        _ICON_CACHE.pop(next(iter(_ICON_CACHE)))   # dict 保持插入顺序，弹掉最旧的键
    _ICON_CACHE[key] = img
    return img


def _render_icon(text: str, color: tuple) -> Image.Image:
    """绘制图标：放大 _ICON_SS 倍画完再缩回目标尺寸，得到抗锯齿边缘。

    直接在最终尺寸（16–32px）上画椭圆和数字，边缘会是硬锯齿、数字发虚；超采样后
    Lanczos 缩小，圆边和字形都平滑，托盘里观感差别明显。颜色/文本仍是缓存键。
    """
    size = _icon_size()
    ss = _ICON_SS
    big = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)

    pad = 2 * ss
    d.ellipse([pad, pad, size * ss - pad, size * ss - pad], fill=color)
    # 深色描边：浅色任务栏上是"描边"，深色任务栏上是"分层"，两种背景下都不糊
    d.ellipse([pad, pad, size * ss - pad, size * ss - pad],
              outline=(0, 0, 0, 90), width=max(1, ss - 1))

    font = _icon_font(ss)
    bbox = d.textbbox((0, 0), text, font=font)   # 已是大画布坐标系，不能再乘倍数
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size * ss - w) / 2 - bbox[0], (size * ss - h) / 2 - bbox[1] - 2 * ss),
           text, fill=(255, 255, 255, 255), font=font)

    return big.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- 窗口 / 任务栏图标


def message_box(title: str, text: str) -> None:
    threading.Thread(
        target=lambda: user32.MessageBoxW(0, text, title, 0x00000000 | 0x00040000),
        daemon=True,
    ).start()

# 窗口图标给多个尺寸：标题栏和任务栏按钮各取最接近的一张，不会糊
_APP_ICON_SIZES = (16, 32, 64)
_APP_ICON_CACHE: dict = {}


def _render_app_icon(size: int) -> Image.Image:
    """按 mem_guard.ico 的同一套视觉绘制品牌图标（蓝底内存颗粒）。

    比例照抄 make_icon.py 的口径（6/64 边距、radius 12、3 行 4 列存储颗粒），按目标
    尺寸等比缩放。窗口图标是静态的，不带内存百分比——百分比是托盘里那一枚的职责。
    """
    ss = _ICON_SS
    u = size * ss / 64.0
    big = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([6 * u, 6 * u, 57 * u, 57 * u], radius=round(12 * u),
                        fill=(37, 99, 235, 255))
    for r in range(3):
        for c in range(4):
            x0, y0 = (14 + c * 11) * u, (16 + r * 11) * u
            d.rectangle([x0, y0, x0 + 7 * u, y0 + 7 * u], fill=(226, 232, 240, 255))
    return big.resize((size, size), Image.LANCZOS)


def app_icon(size: int) -> Image.Image:
    """品牌图标（按尺寸缓存）：窗口标题栏、任务栏按钮用同一张图。"""
    img = _APP_ICON_CACHE.get(size)
    if img is None:
        img = _render_app_icon(size)
        _APP_ICON_CACHE[size] = img
    return img


def apply_window_icon(root) -> None:
    """把 MemGuard 自己的图标挂到 tkinter 窗口上。

    不挂的话任务栏按钮 / Alt-Tab / 标题栏显示的是 Tk 默认图标（源码运行时甚至是 Python
    的图标），用户根本看不出这是 MemGuard。失败就保持默认图标，不影响任何功能。
    """
    try:
        from PIL import ImageTk

        photos = [ImageTk.PhotoImage(app_icon(size)) for size in _APP_ICON_SIZES]
        root.iconphoto(True, *photos)
        # Tk 只在命令执行期间持有这些图片，Python 侧也必须留住引用，否则被 GC 后
        # 任务栏按钮会变空白
        root._memguard_icon_photos = photos
    except Exception:
        pass
    # Tk 的小类图标是从大图重采样 + 预乘 alpha 得来的，标题栏那枚 16px 会发灰；
    # 用自己造的 HICON 再覆盖一次。失败无所谓——Tk 那份依旧在，窗口照常。
    _override_class_icons(root)


# 自造 HICON 按尺寸缓存：四个 Tk 窗口共享同一个类（TkTopLevel），同句柄重复
# SetClassLongPtr 不泄漏；但 Tk 每建一次窗都会重设类图标，所以每个窗口都要再覆盖
_CLASS_ICON_CACHE: dict = {}


def _class_icon_handle(size: int) -> int:
    """按尺寸取（或造）一枚 HICON；造不出来返回 0。"""
    got = _CLASS_ICON_CACHE.get(size)
    if got is None:
        got = icon_handle(app_icon(size))
        if got:
            _CLASS_ICON_CACHE[size] = got
    return got or 0


def _override_class_icons(root) -> None:
    """iconphoto 之后把类图标换成本地造的 HICON（见 winapi.set_window_class_icons）。

    建窗后立刻调到这里时 TkTopLevel 包装窗口往往还没建（GetParent 返回 0），
    不先 update_idletasks 把它催出来，句柄拿不到、覆盖整个被跳过。
    """
    try:
        root.update_idletasks()
        hwnd = toplevel_hwnd(root)
        if not hwnd:
            return
        set_window_class_icons(hwnd, _class_icon_handle(32), _class_icon_handle(16))
    except Exception:
        pass


def _raise_window(root) -> None:
    """把已经开着的窗口重新拉到前台（只能在窗口所在的事件循环线程里调用）。

    deiconify 负责最小化，lift 负责被别的窗口压住；update_idletasks 让布局先落地，
    否则某些主题下 lift 之后窗口内容会闪一下空白。
    """
    try:
        root.deiconify()
        root.update_idletasks()
        root.lift()
        root.focus_force()
    except Exception:
        pass

# 各窗口的单实例开关：同一种窗口同时只开一个（tkinter 多 Tk 实例容易出怪问题）
_overview_window_open = False


# 「概览窗已开着、但需要亮出来」的跨线程请求：二次启动（任务栏/桌面快捷方式再点一次）
# 和托盘左键都会置位。窗口只认自己线程里的调用，所以这里只置位，
# 由窗口自己的轮询取走并执行（见 _build_overview 的 raise_watch）。
_overview_raise = threading.Event()
_top_window_open = False
_trend_window_open = False
_advice_window_open = False

LEVEL_COLORS = {"warn": "#c0392b", "tip": "#b8860b", "info": "#2d6cdf"}
LEVEL_TAGS = {"warn": "注意", "tip": "建议", "info": "提示"}


def _mem_lines(s: dict) -> tuple:
    """把内存状态拆成 (两条进度条标签, 明细行, 可用行)，窗口与回退文案共用。"""
    bars = (f"{s['phys_pct']:5.1f}%", f"{s['commit_pct']:5.1f}%")
    detail = (f"已用 物理 {gb(s['used_phys'])} / 共 {gb(s['total_phys'])}"
              f"    已用 提交 {gb(s['used_commit'])} / 共 {gb(s['total_commit'])}")
    free = f"可用 物理 {gb(s['avail_phys'])}    可用 提交 {gb(s['avail_commit'])}"
    return bars, detail, free


def _overview_text(rows=None) -> str:
    """概览纯文本版：无 tkinter 时的回退，也供测试断言内容。"""
    s = get_mem()
    bars, detail, free = _mem_lines(s)
    lines = [f"物理内存  {bars[0]}    提交内存  {bars[1]}", detail, free, "-" * 46]
    for i, (name, rss, pid) in enumerate(rows if rows is not None
                                         else top_processes_list(5), start=1):
        share = rss / s["total_phys"] * 100 if s["total_phys"] else 0
        lines.append(f"{i}. {name[:26]:<26} {gb(rss):>7}  {share:4.1f}%  PID {pid}")
    return "\n".join(lines)


def _group_rows(rows) -> list:
    """按进程名合并多实例：(进程名, 合计内存, 实例数, 最大PID)。

    对标任务管理器的默认视图：同名多开（浏览器、终端）合并成一行，否则 Top 列表
    会被同一程序刷屏。占比合计口径不变。
    """
    acc = {}
    for name, rss, pid in rows:
        cur = acc.get(name)
        if cur is None:
            acc[name] = [rss, 1, pid]
        else:
            cur[0] += rss
            cur[1] += 1
            cur[2] = max(cur[2], pid)
    out = [(name, v[0], v[1], v[2]) for name, v in acc.items()]
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _top_grouped(n: int) -> list:
    """Top N 进程**组**：先按名前 N*4 取行再合并，保证合并后仍凑得满 N 组。

    直接用 top N 行合并会被同名多开挤掉名额（3 个浏览器实例吃掉 3 行却只显示 1 组）。
    """
    return _group_rows(top_processes_list(max(n * 4, n + 10)))[:n]


def _fmt_bytes(n: float) -> str:
    """表格用的内存格式：小于 1GB 显示 MB（任务管理器口径），否则保留两位小数。"""
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.0f}MB"
    return f"{n / 1024 ** 3:.2f}GB"


def _table_rows(groups, total):
    """把 _group_rows 的结果渲染成表格行（名称带 ×N 标记、内存、占比、PID）。"""
    out = []
    for name, rss, count, pid in groups:
        shown = f"{name} ×{count}" if count > 1 else name
        out.append((shown, _fmt_bytes(rss), f"{rss / total * 100:.1f}%" if total else "-", pid))
    return out


def _bar_color(pct: float) -> str:
    return "#cf3b36" if pct >= 85 else ("#d69e2e" if pct >= 75 else "#2ea043")


def show_overview_window(hooks: dict) -> None:
    """左键概览窗口：内存两条进度条 + Top5 进程 + 常用动作。

    hooks 由 tray/菜单注入，ui 因此不需要反向依赖 tray：
        clean  -> 立即清理回调；top / trend / advice -> 打开对应窗口；history -> 取趋势采样
    """
    global _overview_window_open
    if _overview_window_open:
        # 窗口已经开着（可能被最小化，或压在别的窗口下面）：亮出来而不是静默返回；
        # 不亮的话用户的感觉就是「点了没反应」
        _overview_raise.set()
        return
    _overview_window_open = True

    def worker() -> None:
        global _overview_window_open
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            _overview_window_open = False
            message_box("MemGuard - 内存概览", _overview_text())
            return
        try:
            _build_overview(tk, ttk, hooks)
        except Exception as e:
            message_box("MemGuard - 内存概览", f"窗口打开失败：{e!r}\n\n" + _overview_text())
        finally:
            _overview_window_open = False

    threading.Thread(target=worker, daemon=True).start()


def _build_overview(tk, ttk, hooks: dict) -> None:
    root = tk.Tk()
    root.title("MemGuard - 内存概览")
    root.geometry("560x470")
    apply_window_icon(root)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    stats = {"rows": []}
    summary = ttk.Frame(root, padding=12)
    summary.pack(fill="x")

    bars = {}
    for key, label in (("phys", "物理内存"), ("commit", "提交内存")):
        row = ttk.Frame(summary)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=8).pack(side="left")
        bar = ttk.Progressbar(row, length=320, maximum=100)
        bar.pack(side="left", padx=(0, 10))
        num = ttk.Label(row, text="--%", width=7, justify="right",
                        font=("Consolas", 11, "bold"))
        num.pack(side="left")
        bars[key] = (bar, num)

    detail_lbl = ttk.Label(summary, text="", foreground="#666")
    detail_lbl.pack(fill="x", pady=(2, 0))
    free = ttk.Label(summary, text="", foreground="#666")
    free.pack(fill="x")

    ttk.Separator(root).pack(fill="x", padx=10)
    ttk.Label(root, text="内存占用 Top5", font=("", 10, "bold")).pack(anchor="w", padx=12, pady=(6, 2))

    table = ttk.Treeview(root, columns=("name", "rss", "share", "pid"),
                         show="headings", height=6)
    for col, text, w, anchor in (("name", "进程", 200, "w"), ("rss", "内存", 70, "e"),
                                 ("share", "占比", 60, "e"), ("pid", "PID", 60, "e")):
        table.heading(col, text=text)
        table.column(col, width=w, anchor=anchor)
    table.pack(fill="both", expand=True, padx=12)
    for i in range(12):                       # 预建斑马纹 tag，避免每次刷新建新 style
        table.tag_configure("odd" if i % 2 else "even",
                            background="#f7f9fb" if i % 2 else "#ffffff")

    def refresh(with_table: bool = True) -> None:
        s = get_mem()
        pct_texts, detail_text, free_text = _mem_lines(s)
        for key, pct in (("phys", s["phys_pct"]), ("commit", s["commit_pct"])):
            bar, num = bars[key]
            bar["value"] = pct
            num.config(text=pct_texts[0] if key == "phys" else pct_texts[1],
                       foreground=_bar_color(pct))
        detail_lbl.config(text=detail_text)
        free.config(text=free_text)

        if with_table:
            rows = _top_grouped(5)
            stats["rows"] = rows
            table.delete(*table.get_children())
            for i, values in enumerate(_table_rows(rows, s["total_phys"])):
                table.insert("", "end", values=values, tags=(("odd" if i % 2 else "even"),))

    btns = ttk.Frame(root, padding=12)
    btns.pack(fill="x")
    ttk.Button(btns, text="立即清理", command=lambda: _safe_clean(hooks, refresh)).pack(side="left")
    ttk.Button(btns, text="刷新", command=refresh).pack(side="left", padx=6)
    ttk.Button(btns, text="Top10", command=lambda: hooks.get("top") and hooks["top"]()
               ).pack(side="left")
    ttk.Button(btns, text="趋势", command=lambda: hooks.get("trend") and hooks["trend"]()
               ).pack(side="left", padx=6)
    ttk.Button(btns, text="优化建议",
               command=lambda: hooks.get("advice") and hooks["advice"]()).pack(side="left")

    refresh()

    # 让概览窗像任务管理器一样"活"着：进度条/数字每 1s 刷新（get_mem 是廉价 ctypes 调用），
    # Top5 表格每 3 个 tick 才重建一次，避免同名进程每帧重排造成视觉抖动。
    tick_state = {"n": 0}

    # 二次启动唤窗信号 / 托盘左键喊「窗口亮出来」时在自己的事件循环里轮询取走：
    # Tk 不认跨线程调用，直接调主窗口方法会崩，所以要窗口自己响应
    def raise_watch() -> None:
        if _overview_raise.is_set():
            _overview_raise.clear()
            _raise_window(root)
        if root.winfo_exists():
            root.after(200, raise_watch)

    root.after(200, raise_watch)

    def tick() -> None:
        try:
            if root.winfo_exists():
                refresh(with_table=(tick_state["n"] % 3 == 0))
                tick_state["n"] += 1
                root.after(1000, tick)
        except Exception:
            pass

    root.after(1000, tick)
    root.mainloop()


def _safe_clean(hooks: dict, on_done) -> None:
    """在后台线程执行清理，避免主线程（托盘/窗口）被清理动作卡住。"""
    def run():
        try:
            if hooks.get("clean"):
                hooks["clean"]()
        except Exception:
            pass
        try:
            on_done()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


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
            from tkinter import ttk
        except Exception:
            _top_window_open = False
            st = get_mem()
            bars, detail, free = _mem_lines(st)
            message_box("内存占用 Top10",
                        "\n".join([f"物理 {bars[0]} / 提交 {bars[1]}", detail, free])
                        + "\n" + "-" * 46 + "\n" + top_processes(10))
            return
        try:
            _build_top(tk, ttk)
        finally:
            _top_window_open = False

    threading.Thread(target=worker, daemon=True).start()


def _build_top(tk, ttk) -> None:
    root = tk.Tk()
    root.title("MemGuard - 内存占用 Top10")
    root.geometry("620x470")
    apply_window_icon(root)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    header = ttk.Label(root, text="", padding=(12, 10, 12, 4), justify="left")
    header.pack(fill="x")

    table = ttk.Treeview(root, columns=("rank", "name", "rss", "share", "pid"),
                         show="headings", height=10)
    for col, text, w, anchor in (("rank", "#", 34, "e"), ("name", "进程", 220, "w"),
                                 ("rss", "内存", 80, "e"), ("share", "占物理", 70, "e"),
                                 ("pid", "PID", 70, "e")):
        table.heading(col, text=text)
        table.column(col, width=w, anchor=anchor)
    table.pack(fill="both", expand=True, padx=12)
    for i in range(10):
        table.tag_configure("odd" if i % 2 else "even",
                            background="#f7f9fb" if i % 2 else "#ffffff")

    def refresh() -> None:
        st = get_mem()
        bars, detail, free = _mem_lines(st)
        header.config(text=f"物理内存 {bars[0]}    提交内存 {bars[1]}\n{detail}\n{free}")
        table.delete(*table.get_children())
        for i, values in enumerate(_table_rows(_top_grouped(10), st["total_phys"])):
            table.insert("", "end", values=(i + 1,) + values,
                         tags=(("odd" if i % 2 else "even"),))

    ttk.Button(root, text="刷新", command=refresh).pack(pady=8)
    refresh()
    root.mainloop()


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
            _build_trend(tk, history_getter)
        finally:
            _trend_window_open = False

    threading.Thread(target=worker, daemon=True).start()


def _build_trend(tk, history_getter) -> None:
    root = tk.Tk()
    root.title("MemGuard - 内存趋势")
    root.geometry("680x460")
    apply_window_icon(root)
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass

    canvas = tk.Canvas(root, bg="white", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    PHYS, COMMIT = "#2d6cdf", "#cf3b36"

    def draw() -> None:
        canvas.delete("all")
        data = list(history_getter())
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 40 or h < 40:                     # 尚未布局完成，等下一次 tick
            return
        left, right, top, bottom = 46, 20, 24, 38
        cw, ch = w - left - right, h - top - bottom
        canvas.create_text(left, 8, anchor="nw", text="内存使用率趋势 (%)", fill="#333",
                           font=("", 10, "bold"))
        # 横向网格与纵轴刻度
        for lvl in (0, 25, 50, 75, 100):
            y = top + ch - ch * lvl / 100
            canvas.create_line(left, y, w - right, y, fill="#e6e6e6")
            canvas.create_text(left - 6, y, anchor="e", text=f"{lvl}", fill="#888")
        if len(data) < 2:
            canvas.create_text(left + cw / 2, top + ch / 2, anchor="center",
                               text="采样中...（每个检测周期一个点）", fill="#999")
        else:
            n = len(data)
            for i in range(0, n, max(1, n // 6)):   # 时间轴刻度
                x = left + cw * i / (n - 1)
                canvas.create_line(x, top, x, top + ch, fill="#f2f2f2")
                canvas.create_text(x, top + ch + 4, anchor="n",
                                   text=f"{(data[-1][0] - data[i][0]):.0f}s", fill="#999")
            for key, color in ((lambda p, c: p, PHYS), (lambda p, c: c, COMMIT)):
                coords = []
                for i, (_, ph, cm) in enumerate(data):
                    x = left + cw * i / (n - 1)
                    y = top + ch - ch * (key(ph, cm) / 100)
                    coords += [x, y]
                canvas.create_line(*coords, fill=color, width=2, smooth=True)
            phys_now, commit_now = data[-1][1], data[-1][2]
            for pct, color, text in ((phys_now, PHYS, f"物理 {phys_now:.0f}%"),
                                     (commit_now, COMMIT, f"提交 {commit_now:.0f}%")):
                y = top + ch - ch * pct / 100
                x = left + cw
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline=color)
                canvas.create_text(w - right - 4, y - 8, anchor="e", text=text, fill=color)

    def tick() -> None:
        try:
            if root.winfo_exists():
                draw()
                root.after(1000, tick)
        except Exception:
            pass

    # 首次布局完成前画布尺寸还是 1x1，直接画等于画空；绑 Configure 让尺寸一就绪就画，
    # 再补一个延迟首绘，避免用户先看到一帧空白
    canvas.bind("<Configure>", lambda _e: draw())
    root.after(100, draw)
    root.after(1000, tick)
    root.mainloop()


def apply_advice_actions(items: list, apply_fn) -> list:
    """把建议里带的 action 依次交给 apply_fn（一般是 config.update_config）。

    返回成功应用的动作列表：单条失败只跳过该条、不连带其余建议——「应用建议」是
    顺手功能，绝不能因为某一条写不进去，就把其余项也一起废掉。
    """
    done: list = []
    for act in advice_actions(items):
        changes = dict(act.get("changes") or {})
        if not changes:
            continue
        try:
            apply_fn(changes)
        except Exception:
            continue
        done.append(act)
    return done


def show_advice_window(cfg) -> None:
    """弹出可刷新的优化建议窗口；无 tkinter 时回退到 MessageBox。

    cfg 既可以是配置 dict，也可以是返回 dict 的 callable（托盘传后者）：一键应用
    会写配置并让 guard.cfg 换成新 dict，传 callable 才能让「刷新」读到最新配置，
    否则窗口里看到的永远是开窗那一刻的旧快照。
    """
    global _advice_window_open
    if _advice_window_open:
        return
    _advice_window_open = True

    def current_cfg() -> dict:
        return cfg() if callable(cfg) else cfg

    def worker() -> None:
        global _advice_window_open
        try:
            import tkinter as tk
        except Exception:
            _advice_window_open = False
            message_box("MemGuard - 优化建议", format_advice(analyze(current_cfg())))
            return
        try:
            root = tk.Tk()
            root.title("MemGuard - 优化建议")
            root.geometry("640x470")
            apply_window_icon(root)
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass

            body = tk.Text(root, font=("Microsoft YaHei UI", 10), wrap="word",
                           relief="flat", background="#fbfbfc", padx=12, pady=10)
            body.pack(fill="both", expand=True)
            for lvl, color in LEVEL_COLORS.items():
                body.tag_config(lvl, foreground=color,
                                font=("Microsoft YaHei UI", 10, "bold"))

            bar = tk.Frame(root)
            bar.pack(fill="x", pady=4)

            def apply_one(act: dict):
                def run() -> None:
                    try:
                        update_config(dict(act.get("changes") or {}))
                    except Exception as e:
                        message_box("MemGuard - 应用建议", f"应用失败：{e!r}")
                        return
                    log(f"优化建议一键应用 | {act.get('label', '应用')} -> "
                        f"{act.get('changes') or {}}")
                    refresh()
                return run

            def apply_all() -> None:
                done = apply_advice_actions(analyze(current_cfg()), update_config)
                if done:
                    log("优化建议一键应用 | 全部 " +
                        "、".join(str(a.get("label", "应用")) for a in done))
                refresh()

            def refresh() -> None:
                items = analyze(current_cfg())
                body.delete("1.0", "end")
                if not items:
                    body.insert("end", "未发现明显可优化项，当前配置与内存状态良好。")
                else:
                    for it in items:
                        lvl = it["level"]
                        body.insert("end", f"[{LEVEL_TAGS.get(lvl, '·')}] ", lvl)
                        body.insert("end", f"{it['title']}\n", lvl)
                        body.insert("end", "    " + it["text"] + "\n\n")
                for w in bar.winfo_children():
                    w.destroy()
                acts = advice_actions(items)
                if acts:
                    tk.Label(bar, text="一键应用：").pack(side="left", padx=(10, 4))
                    for act in acts:
                        tk.Button(bar, text=act["label"], width=13,
                                  command=apply_one(act)).pack(side="left", padx=3)
                    if len(acts) > 1:
                        tk.Button(bar, text="全部应用", width=9,
                                  command=apply_all).pack(side="left", padx=3)

            tk.Button(root, text="刷新", width=12, command=refresh).pack(pady=8)
            refresh()
            root.mainloop()
        finally:
            _advice_window_open = False

    threading.Thread(target=worker, daemon=True).start()
