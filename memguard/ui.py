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

from .advisor import analyze, format_advice
from .clean import top_processes
from .config import gb
from .winapi import get_mem, user32

# ---------------------------------------------------------------- 托盘图标


_ICON_SIZE = 64
_ICON_FONT_PX = 26
_FONT_CACHE: dict = {}
# 图标内容只取决于「显示文本 + 颜色档」，故按该组合缓存；分桶后组合数很少，
# 无需每轮刷新都重新绘制并重新加载字体文件。
_ICON_CACHE: dict = {}
_ICON_CACHE_MAX = 512


def _icon_font():
    """加载图标字体（只读一次，避免每帧 truetype 反复读字体文件）。"""
    font = _FONT_CACHE.get(_ICON_FONT_PX)
    if font is None:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", _ICON_FONT_PX)
        except Exception:
            font = ImageFont.load_default()
        _FONT_CACHE[_ICON_FONT_PX] = font
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
    if len(_ICON_CACHE) >= _ICON_CACHE_MAX:
        _ICON_CACHE.clear()
    _ICON_CACHE[key] = img
    return img


def _render_icon(text: str, color: tuple) -> Image.Image:
    size = _ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, size - 3, size - 3], fill=color)

    font = _icon_font()
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


# ---------------------------------------------------------------- Top10 窗口

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


# ---------------------------------------------------------------- 趋势窗口

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


# ---------------------------------------------------------------- 优化建议窗口

_advice_window_open = False


def show_advice_window(cfg: dict) -> None:
    """弹出可刷新的优化建议窗口；无 tkinter 时回退到 MessageBox。"""
    global _advice_window_open
    if _advice_window_open:
        return
    _advice_window_open = True

    def worker() -> None:
        global _advice_window_open
        try:
            import tkinter as tk
        except Exception:
            _advice_window_open = False
            message_box("MemGuard - 优化建议", format_advice(analyze(cfg)))
            return
        try:
            root = tk.Tk()
            root.title("MemGuard - 优化建议")
            root.geometry("640x460")
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass

            body = tk.Text(root, font=("Consolas", 10), wrap="word",
                           relief="flat", background="#f7f7f7")
            body.pack(fill="both", expand=True, padx=10, pady=10)

            def refresh() -> None:
                items = analyze(cfg)
                body.delete("1.0", "end")
                if not items:
                    body.insert("1.0", "未发现明显可优化项，当前配置与内存状态良好。")
                    return
                for it in items:
                    label = {"warn": "注意", "tip": "建议", "info": "提示"}.get(it["level"], "·")
                    body.insert("end", f"[{label}] {it['title']}\n")
                    body.insert("end", "    " + it["text"] + "\n\n")

            tk.Button(root, text="刷新", width=12, command=refresh).pack(pady=8)
            refresh()
            root.mainloop()
        finally:
            _advice_window_open = False

    threading.Thread(target=worker, daemon=True).start()
