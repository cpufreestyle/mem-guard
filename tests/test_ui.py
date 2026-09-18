# -*- coding: utf-8 -*-
"""ui：托盘图标分档取色与缓存（不弹任何窗口）。"""
from memguard.ui import _icon_color, make_icon

GREEN = (46, 160, 67, 255)
YELLOW = (214, 158, 46, 255)
RED = (207, 59, 54, 255)


def test_make_icon_size():
    assert make_icon(50, 85).size == (64, 64)


def test_make_icon_cached_identity():
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
