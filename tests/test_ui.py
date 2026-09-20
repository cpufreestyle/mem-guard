# -*- coding: utf-8 -*-
"""ui：托盘图标分档取色、绘制尺寸与缓存（不弹任何窗口）。"""
import pytest

from memguard import ui
from memguard.ui import _icon_color, make_icon

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
