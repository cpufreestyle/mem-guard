# -*- coding: utf-8 -*-
"""advisor：建议生成与渲染（注入内存状态，避免依赖真实机器与真实进程扫描）。"""
import pytest

from memguard import advisor
from memguard.config import normalize_config

GB = 1024 ** 3


def _mem(phys_pct=50.0, commit_pct=60.0, total_phys=16 * GB, avail_phys=8 * GB,
         total_commit=32 * GB, avail_commit=16 * GB):
    return {
        "total_phys": total_phys,
        "avail_phys": avail_phys,
        "used_phys": total_phys - avail_phys,
        "phys_pct": phys_pct,
        "total_commit": total_commit,
        "avail_commit": avail_commit,
        "used_commit": total_commit - avail_commit,
        "commit_pct": commit_pct,
    }


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """分析不应在测试里遍历真实进程，默认也假定为管理员。"""
    monkeypatch.setattr(advisor, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(advisor, "is_admin", lambda: True)


def test_low_pagefile_warns():
    mem = _mem(commit_pct=85.0, total_phys=16 * GB, avail_phys=1 * GB,
               total_commit=17 * GB, avail_commit=1 * GB)
    items = advisor.analyze(normalize_config({}), mem)
    assert any(i["level"] == advisor.LEVEL_WARN and "虚拟内存" in i["title"] for i in items)


def test_not_admin_warns(monkeypatch):
    monkeypatch.setattr(advisor, "is_admin", lambda: False)
    items = advisor.analyze(normalize_config({}), _mem())
    assert any("管理员" in i["title"] for i in items)


def test_auto_clean_off_info():
    items = advisor.analyze(normalize_config({"auto_clean": False}), _mem())
    assert any("自动清理" in i["title"] for i in items)


def test_warn_margin_zero_tip():
    items = advisor.analyze(normalize_config({"warn_margin": 0}), _mem())
    assert any("预警" in i["title"] for i in items)


def test_cooldown_short_tip():
    items = advisor.analyze(normalize_config({"cooldown": 30}), _mem())
    assert any("冷却" in i["title"] for i in items)


def test_threshold_relation_tip():
    normal = normalize_config({"phys_threshold": 90, "commit_threshold": 95})
    assert not any("阈值关系" in i["title"] for i in advisor.analyze(normal, _mem()))
    inverted = normalize_config({"phys_threshold": 90, "commit_threshold": 80})
    assert any("阈值关系" in i["title"] for i in advisor.analyze(inverted, _mem()))


def test_healthy_info():
    items = advisor.analyze(normalize_config({}), _mem(phys_pct=40.0, commit_pct=50.0))
    assert any("内存充裕" in i["title"] for i in items)


def test_items_sorted_warn_first():
    items = advisor.analyze(normalize_config({"auto_clean": False}),
                            _mem())  # 配合 autouse 的 is_admin=True
    levels = [i["level"] for i in items]
    assert levels == sorted(levels, key=lambda lv: advisor._LEVEL_ORDER[lv])


def test_analyze_survives_mem_read_failure(monkeypatch):
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(advisor, "get_mem", _boom)
    items = advisor.analyze(normalize_config({}))
    assert len(items) == 1
    assert items[0]["level"] == advisor.LEVEL_INFO


def test_format_advice_empty():
    assert "未发现" in advisor.format_advice([])


def test_format_advice_nonempty():
    text = advisor.format_advice([{"level": advisor.LEVEL_TIP, "title": "T", "text": "X"}])
    assert "[建议] T" in text
    assert "X" in text
