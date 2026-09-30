# -*- coding: utf-8 -*-
"""生成 MemGuard 的程序图标 mem_guard.ico（蓝底内存颗粒）。

每一帧都由 memguard.ui._render_app_icon(size) 绘制——那是整套图标链路的唯一真源：
    exe 资源图标（PyInstaller --icon）、窗口标题栏、任务栏按钮、ALT-Tab、
    快捷方式图标取的都是它。这里只负责「按尺寸产出 + 打包成 ico」。

为什么不能只画一张 64x64 交给 Pillow 补齐：Pillow 的 ICO 写入器在没有显式
sizes= 时会按默认尺寸表 [(16,16),(24,24),(32,32),(48,48),(64,64),...] 处理，
缺的尺寸用 thumbnail() 从手上那张图降采样补出来。而运行时是「按目标尺寸超采样
4 倍画完再 Lanczos 缩回」，两条路子在同一设计下像素并不等价——实测 32px 一帧
max_delta 到 255，结果是 exe 里那张脸和窗口标题栏那张脸对不上。显式把每个尺寸
的图喂进 append_images，Pillow 才会原样采用，不再降采样。

用法：python make_icon.py（build.ps1 每次打包都会调，小仓库里也能单独跑）
"""
from __future__ import annotations

import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from memguard.ui import _render_app_icon  # noqa: E402  （设计真源，见模块 docstring）

# ico 帧序列：16=标题栏/小图标，24/32=任务栏按钮，48=资源管理器与桌面大图标，
# 64=ALT-Tab 与高 DPI。少一帧，Windows 就会按最接近的一帧缩放，图标发虚。
SIZES = (16, 24, 32, 48, 64)
OUTPUT = os.path.join(HERE, "mem_guard.ico")


def build_frames() -> list[Image.Image]:
    """逐个尺寸出图：和运行时窗口图标走同一条绘制路径，不是把大图缩小。"""
    return [_render_app_icon(size) for size in SIZES]


def save_icon(frames: list[Image.Image], path: str = OUTPUT) -> None:
    """写成多帧 ico。

    主图必须取最大的那一帧：Pillow 的 ICO _save 里有 size > width 的判断，比主图
    大的尺寸会被直接 continue 掉。拿 16px 当主图，24/32/48/64 全部静默丢掉，最后
    只剩一帧，而且不报错。所以这里按面积排一遍，不依赖调用方递进来的顺序；其余帧
    由 sizes= 按尺寸精确匹配上，顺序无关。
    """
    ordered = sorted(frames, key=lambda im: im.size[0] * im.size[1], reverse=True)
    ordered[0].save(path, format="ICO",
                   sizes=[(size, size) for size in SIZES],
                   append_images=ordered[1:])


def main() -> int:
    frames = build_frames()
    save_icon(frames, OUTPUT)
    print("icon generated -> %s（%s px）" % (OUTPUT, "/".join(str(s) for s in SIZES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
