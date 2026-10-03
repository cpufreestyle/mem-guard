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


def test_analyze_uses_injected_top_snapshot(monkeypatch):
    """传入 top 快照时不得再自行扫描进程（监控线程要复用同一份采样）。"""
    def _boom(n=15):
        raise AssertionError("注入 top 之后不应再扫描进程")

    monkeypatch.setattr(advisor, "top_processes_list", _boom)
    items = advisor.analyze(normalize_config({}), _mem(), [("chrome.exe", 6 * GB, 1)])
    assert any("高内存占用进程" in i["title"] for i in items)


def test_analyze_scans_processes_only_when_top_missing(monkeypatch):
    """未注入 top 时才现场扫描，保持菜单/窗口等单次调用方的实时性。"""
    calls = []
    monkeypatch.setattr(advisor, "top_processes_list",
                        lambda n=15: calls.append(n) or [("chrome.exe", 6 * GB, 1)])
    items = advisor.analyze(normalize_config({}), _mem())
    assert calls == [15]
    assert any("高内存占用进程" in i["title"] for i in items)


def test_format_advice_nonempty():
    text = advisor.format_advice([{"level": advisor.LEVEL_TIP, "title": "T", "text": "X"}])
    assert "[建议] T" in text
    assert "X" in text


# ---------------------------------------------------------------- 升档统计自调优（v1.5.1）

def test_analyze_advises_aggressive_when_escalation_frequent():
    """保守清理频繁补激进时，建议直接把清理力度改成激进：少跑一遍、少打扰一次。"""
    cfg = normalize_config({"stats": {"count": 10, "escalated": 6}})
    items = advisor.analyze(cfg, _mem())
    assert any("频繁升档" in i["title"] for i in items)


def test_analyze_silent_when_escalation_rare():
    """升档占比低是正常兜底，不该制造配置调整噪音。"""
    cfg = normalize_config({"stats": {"count": 100, "escalated": 2}})
    items = advisor.analyze(cfg, _mem())
    assert all("频繁升档" not in i["title"] for i in items)


def test_analyze_advises_enabling_escalation_when_over_threshold():
    """关掉自动升档又已超阈值：提醒开启，否则保守清完仍超阈值。"""
    cfg = normalize_config({"escalate_clean": False})
    items = advisor.analyze(cfg, _mem(phys_pct=88.0))
    assert any("自动升档已关闭" in i["title"] for i in items)


def test_analyze_silent_when_escalation_disabled_but_healthy():
    """升档关闭但内存健康：不额外打扰。"""
    cfg = normalize_config({"escalate_clean": False})
    items = advisor.analyze(cfg, _mem(phys_pct=40.0))
    assert all("自动升档已关闭" not in i["title"] for i in items)


# ---------------------------------------------------------------- 频繁升档判据（v1.6.0：与 tray 自动改激进同源）

def test_frequent_escalation_requires_majority_and_floor():
    """升档占比过半且至少 3 次才算「频繁」；档位已激进则无上升空间，一律返回 False。"""
    base = {"clean_level": "conservative", "stats": {"count": 10, "escalated": 6}}
    assert advisor.frequent_escalation(base) is True
    assert advisor.frequent_escalation({**base, "stats": {"count": 10, "escalated": 2}}) is False
    assert advisor.frequent_escalation({**base, "stats": {"count": 10, "escalated": 3}}) is False
    assert advisor.frequent_escalation({**base, "stats": {"count": 7, "escalated": 3}}) is False
    assert advisor.frequent_escalation({**base, "stats": {"count": 6, "escalated": 3}}) is True
    assert advisor.frequent_escalation({**base, "stats": {}}) is False
    assert advisor.frequent_escalation({**base, "clean_level": "aggressive"}) is False


# ---------------------------------------------------------------- 一键应用动作（自动优化闭环：建议能落地）

def test_analyze_attaches_one_click_actions_for_automation_knobs():
    """自动优化相关建议都带 action：建议窗口据此生成「一键应用」按钮。"""
    cfg = normalize_config({
        "auto_clean": False, "warn_margin": 0, "cooldown": 60,
        "escalate_clean": False,
        "stats": {"count": 6, "escalated": 3},
    })
    items = advisor.analyze(cfg, _mem(phys_pct=99.0))
    acts = {a["label"]: a["changes"]
            for a in advisor.advice_actions(items)}

    assert acts["开启自动清理"] == {"auto_clean": True}
    assert acts["开启预警(15%)"] == {"warn_margin": 15}
    assert acts["冷却调为 300s"] == {"cooldown": 300}
    assert acts["开启自动升档"] == {"escalate_clean": True}
    assert acts["改用激进档"] == {"clean_level": "aggressive"}


def test_healthy_config_has_no_actionable_knob():
    """配置健康时不给按钮：建议窗口只剩「内存充裕」这类提示，没有可乱点的东西。"""
    items = advisor.analyze(normalize_config({}), _mem())
    assert advisor.advice_actions(items) == []


def test_advice_actions_dedupes_and_ignores_plain_items():
    """同一键被多条建议惦记时只留第一条；无 action 的建议（页面文件/进程类）忽略。"""
    items = [
        {"title": "没有动作", "text": "x"},
        {"action": {"label": "先来的", "changes": {"cooldown": 300}}},
        {"action": {"label": "后来的", "changes": {"cooldown": 600}}},
        {"action": {"label": "空 changes", "changes": {}}},
        "not-a-dict",
    ]

    assert advisor.advice_actions(items) == [
        {"label": "先来的", "changes": {"cooldown": 300}}]

# ---------------------------------------------------------------- 预防式清理判据（v1.8.0）

def test_frequent_prevention_requires_majority_and_floor():
    """预防占比过半且至少 3 次才算「频繁」；档位已激进则无上升空间，返回 False。"""
    base = {"clean_level": "conservative", "stats": {"count": 10, "preventive": 6}}
    assert advisor.frequent_prevention(base) is True
    assert advisor.frequent_prevention({**base, "stats": {"count": 10, "preventive": 2}}) is False
    assert advisor.frequent_prevention({**base, "stats": {"count": 10, "preventive": 3}}) is False
    assert advisor.frequent_prevention({**base, "stats": {"count": 7, "preventive": 3}}) is False
    assert advisor.frequent_prevention({**base, "stats": {"count": 6, "preventive": 3}}) is True
    assert advisor.frequent_prevention({**base, "stats": {}}) is False
    assert advisor.frequent_prevention({**base, "clean_level": "aggressive"}) is False


def test_analyze_advises_aggressive_when_prevention_frequent():
    """预防式清理频繁触发：说明内存在持续上涨，提前温和清只是拖延。"""
    cfg = normalize_config({"stats": {"count": 10, "preventive": 6}})
    items = advisor.analyze(cfg, _mem())
    hit = [i for i in items if "预防式清理频繁" in i["title"]]
    assert hit
    assert hit[0]["action"] == {"label": "改用激进档",
                               "changes": {"clean_level": "aggressive"}}
    assert "6" in hit[0]["text"]


def test_analyze_silent_when_prevention_rare():
    """偶尔预防一次是特性在正常工作，不该制造配置调整噪音。"""
    cfg = normalize_config({"stats": {"count": 100, "preventive": 2}})
    items = advisor.analyze(cfg, _mem())
    assert all("预防式清理频繁" not in i["title"] for i in items)


# ---------------------------------------------------------------- 清理效果判据（v1.9.0）


def test_frequent_short_relief_requires_majority_and_floor():
    """短效占比过半且至少 3 次才算「频繁」；档位已激进则无上升空间，返回 False。"""
    base = {"clean_level": "conservative", "stats": {"count": 10, "short_relief": 6}}
    assert advisor.frequent_short_relief(base) is True
    fewer = {**base, "stats": {"count": 10, "short_relief": 2}}
    assert advisor.frequent_short_relief(fewer) is False
    under_half = {**base, "stats": {"count": 10, "short_relief": 3}}
    assert advisor.frequent_short_relief(under_half) is False
    minority = {**base, "stats": {"count": 7, "short_relief": 3}}
    assert advisor.frequent_short_relief(minority) is False
    assert advisor.frequent_short_relief({**base, "stats": {"count": 6, "short_relief": 3}}) is True
    assert advisor.frequent_short_relief({**base, "stats": {}}) is False
    assert advisor.frequent_short_relief({**base, "clean_level": "aggressive"}) is False


def test_analyze_advises_aggressive_when_short_relief_frequent():
    """清理效果偏短频繁触发：先温和再反复清只是拖延，建议一次清到位。"""
    cfg = normalize_config({"stats": {"count": 10, "short_relief": 6}})
    items = advisor.analyze(cfg, _mem())
    hit = [i for i in items if "清理效果不佳" in i["title"]]
    assert hit
    assert hit[0]["action"] == {"label": "改用激进档",
                                "changes": {"clean_level": "aggressive"}}
    assert "6" in hit[0]["text"]


def test_analyze_silent_when_short_relief_rare():
    """偶尔短效一次是正常现象（清完内存很快又涨），不该制造配置调整噪音。"""
    cfg = normalize_config({"stats": {"count": 100, "short_relief": 2}})
    items = advisor.analyze(cfg, _mem())
    assert all("清理效果不佳" not in i["title"] for i in items)


def test_analyze_flags_suspected_leak_processes():
    """疑似泄漏进程要单独点名：给出名字、当前占用，并说明会优先被定向清理。"""
    leaks = [("leaky.exe", 2 * GB, 7)]
    items = advisor.analyze(normalize_config({}), _mem(), [], leaks=leaks)
    hit = [i for i in items if "疑似泄漏" in i["title"]]
    assert hit
    assert "leaky.exe" in hit[0]["text"]
    assert "2.0GB" in hit[0]["text"]
    assert hit[0]["level"] == advisor.LEVEL_TIP


def test_analyze_silent_when_no_leak_candidates():
    """不传 leaks（CLI / 一次性自检拿不到历史）时不该凭空报泄漏。"""
    items = advisor.analyze(normalize_config({}), _mem(), [])
    assert all("疑似泄漏" not in i["title"] for i in items)
    assert all("疑似泄漏" not in i["title"]
               for i in advisor.analyze(normalize_config({}), _mem(), [], leaks=[]))
