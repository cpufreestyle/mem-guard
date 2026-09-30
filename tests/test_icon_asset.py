# -*- coding: utf-8 -*-
r"""程序图标资产：ico 的每一帧必须与运行时窗口图标同源同像素。

回归背景：make_icon.py 早期只画一张 64x64，剩下的尺寸全靠 Pillow 的 ICO 写入器
按默认尺寸表 thumbnail() 降采样补齐。而运行时窗口图标是「按目标尺寸超采样 4 倍
再 Lanczos 缩回」，两条路子在同一设计下像素并不等价——32px 一帧实测 max_delta
到 255。后果是 exe 资源里的图标和标题栏/任务栏按钮不是同一张脸：桌面图标、
ALT-Tab 预览看着发虚，和窗口标题栏对不上。

这里把 make_icon 产出的 ico 逐帧抠出来，与 memguard.ui.app_icon(size) 对账；
两边任何一边改了另一边忘了改，都会立刻红。不读仓库里的 mem_guard.ico：它是
gitignore 的构建产物，CI 也是先跑本测试再打包，本文件断言「生成函数」的契约。
"""
import os

import pytest
from PIL import Image, ImageChops

import make_icon
from memguard import ui

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUILD_SCRIPT = os.path.join(_ROOT, "build.ps1")


def _save(tmp_path) -> str:
    frames = make_icon.build_frames()
    path = str(tmp_path / "mem_guard.ico")
    make_icon.save_icon(frames, path)
    return path


def _frame(path: str, size: int) -> Image.Image:
    """从多帧 ico 里按尺寸取一帧（Pillow 需要先指定 size 再 load）。"""
    img = Image.open(path)
    img.size = (size, size)
    return img.convert("RGBA")


def test_ico_covers_every_size_window_icons_use() -> None:
    """运行时图标涉及的每个尺寸，ico 里都得有专属帧。

    缺一帧，Windows 就按最接近的那一帧缩放，任务栏按钮、ALT-Tab 预览都会发虚。
    """
    sizes = set(make_icon.SIZES)
    assert sizes >= set(ui._APP_ICON_SIZES)
    assert all(size in sizes for size in (16, 24, 32, 48, 64))


def test_ico_lists_every_frame_in_header(tmp_path) -> None:
    """裸读 ico 头：帧数、尺寸、位深都得写对，少一帧资源管理器就当小图标用。"""
    path = _save(tmp_path)
    with open(path, "rb") as f:
        raw = f.read()
    assert int.from_bytes(raw[2:4], "little") == 1, "type 必须是 icon"
    count = int.from_bytes(raw[4:6], "little")
    assert count == len(make_icon.SIZES)
    dims = []
    for i in range(count):
        off = 6 + i * 16
        # ico 里 0 表示 256，本图标最大 64，直接取字节即可
        dims.append(raw[off])
        assert raw[off + 1] == raw[off], "ico 帧应为正方形"
        assert int.from_bytes(raw[off + 6:off + 8], "little") == 32, "32bpp 才带 alpha"
    assert dims == sorted(make_icon.SIZES)
    last = 6 + 16 * (count - 1)
    last_bytes = int.from_bytes(raw[last + 8:last + 12], "little")
    last_offset = int.from_bytes(raw[last + 12:last + 16], "little")
    assert last_offset + last_bytes == len(raw), "最后一帧的数据得完整落在文件里"


@pytest.mark.parametrize("size", make_icon.SIZES)
def test_each_frame_is_pixel_identical_to_runtime_icon(tmp_path, size) -> None:
    """逐帧与 app_icon(size) 对账：同源同像素，而不是把大图缩出来的近似。"""
    path = _save(tmp_path)
    frame = _frame(path, size)
    ref = ui.app_icon(size).convert("RGBA")
    assert frame.size == (size, size)
    assert frame.mode == ref.mode
    diff = ImageChops.difference(frame, ref)
    worst = max(upper for _, upper in diff.getextrema())
    assert worst == 0, ("ico 的 %dpx 帧与运行时窗口图标不一致，"
                      "Pillow 又回去降采样了？" % size)


def test_save_icon_always_writes_every_frame(tmp_path) -> None:
    """不管帧按什么顺序递进来，五帧都得写全。

    Pillow 的 ICO _save 会跳过比主图大的尺寸：主图取 16px 时 24/32/48/64 全被静默
    丢掉，写出来的 ico 只剩一帧。save_icon 因此固定拿最大那张当主图，帧顺序无关。
    """
    frames = list(reversed(make_icon.build_frames()))     # 小图排在最前也能写全
    path = str(tmp_path / "reversed.ico")
    make_icon.save_icon(frames, path)
    with open(path, "rb") as f:
        count = int.from_bytes(f.read(6)[4:6], "little")
    assert count == len(make_icon.SIZES), "主图不是最大那张，帧会被 Pillow 静默丢掉"


def test_build_script_regenerates_icon_every_run() -> None:
    """build.ps1 必须每次都重生成图标。

    写成"仅当不存在时生成"时，改过设计的老 checkout 会一直用旧 mem_guard.ico，
    打完的 exe 里还是上一版图标，而 make_icon.py 已经是对的——这种"看着改好了、
    实际没生效"的坑只有脚本断言能兜住。
    """
    # ps1 是 UTF-8 with BOM：utf-8-sig 才能把签名吃掉，否则第一行带着乱码
    with open(_BUILD_SCRIPT, encoding="utf-8-sig") as f:
        text = f.read()
    lines = text.splitlines()
    pos = next(i for i, ln in enumerate(lines) if "python make_icon.py" in ln)
    assert lines[pos - 1].strip() == "try {", "生成图标必须无条件执行，不能拿文件是否存在当跳过条件"
    # 生成失败时要能回落到现有图标（而不是让打包中断），但要明说，别静默
    assert "沿用现有 mem_guard.ico" in text, "图标生成失败应给出可回退的提示"
