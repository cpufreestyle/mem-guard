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


# -------------------------------------------- 清理提前量自调优判据（v1.16.0）


def test_recurrent_short_relief_needs_floor_but_not_majority():
    """短效累计 ≥2 但还没过半：建议提前阈值；过半则交给 3b 的「改用激进」。"""
    base = {"clean_level": "conservative", "stats": {"count": 10, "short_relief": 2}}
    assert advisor.recurrent_short_relief(base) is True
    once = {**base, "stats": {"count": 10, "short_relief": 1}}
    assert advisor.recurrent_short_relief(once) is False
    # 过半（6/10）：frequent_short_relief 已覆盖，别让同一份证据出两条建议
    majority = {**base, "stats": {"count": 10, "short_relief": 6}}
    assert advisor.recurrent_short_relief(majority) is False
    assert advisor.recurrent_short_relief({**base, "stats": {}}) is False
    assert advisor.recurrent_short_relief({**base, "headroom_adapt": False}) is False
    assert advisor.recurrent_short_relief(
        {**base, "headroom_adapt_done": True}) is False
    assert advisor.recurrent_short_relief(
        {**base, "target_headroom_pct": advisor._HEADROOM_MAX}) is False
    # 脏余量走不到有效区间：没证据就不下结论
    assert advisor.recurrent_short_relief(
        {**base, "target_headroom_pct": "x"}) is False


def test_analyze_advises_headroom_when_short_relief_recurrent():
    """短效连续但未过半：建议把判定阈值提前几个百分点，带一键应用动作。"""
    cfg = normalize_config({"stats": {"count": 5, "short_relief": 2}})
    items = advisor.analyze(cfg, _mem())
    hit = [i for i in items if "清理效果连续偏短" in i["title"]]
    assert len(hit) == 1
    assert hit[0]["level"] == advisor.LEVEL_TIP
    assert hit[0]["action"] == {"label": "提前量 0→5",
                                "changes": {"target_headroom_pct": 5,
                                            "headroom_adapt_done": True}}
    assert "2" in hit[0]["text"] and "0 → 5" in hit[0]["text"]


def test_recurrent_lowmem_shortfall_needs_low_mem_evidence():
    """短效里至少 2 次由绝对下限触发才算「下限偏低」；与提前量判据互斥。"""
    base = {"clean_level": "conservative",
            "stats": {"count": 10, "short_relief": 2, "low_mem": 2},
            "min_avail_mb": 1024, "headroom_adapt_done": True}
    assert advisor.recurrent_lowmem_shortfall(base) is True
    # 没有低内存触发的证据：短效该归提前量管
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "stats": {"count": 10, "short_relief": 2}}) is False
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "stats": {"count": 10, "short_relief": 2, "low_mem": 1}}) is False
    # 短效一次都不算连发：没证据就不下结论
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "stats": {"count": 10, "short_relief": 1, "low_mem": 2}}) is False
    # 短效过半：那该改用激进，不是抬下限
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "stats": {"count": 10, "short_relief": 6, "low_mem": 2}}) is False
    # 提前量判据仍适用：同一份短效证据只出一条建议，先归提前量管
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "headroom_adapt_done": False}) is False
    # 开关关闭 / 已抬高过 / 已到上限 / 脏值 / 下限关闭：没有空间就不下结论
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "min_avail_adapt": False}) is False
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "min_avail_adapt_done": True}) is False
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "min_avail_mb": advisor._MINAVAIL_MAX}) is False
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "min_avail_mb": "x"}) is False
    assert advisor.recurrent_lowmem_shortfall(
        {**base, "min_avail_mb": 0}) is False


def test_analyze_advises_min_avail_when_lowmem_shortfall_recurrent():
    """短效多半由绝对下限触发：建议把可用内存下限抬高一级，带一键应用动作。"""
    cfg = normalize_config({"stats": {"count": 5, "short_relief": 2, "low_mem": 2},
                            "min_avail_mb": 1024, "headroom_adapt_done": True})
    items = advisor.analyze(cfg, _mem())
    hit = [i for i in items if "可用内存下限偏低" in i["title"]]
    assert len(hit) == 1
    assert hit[0]["level"] == advisor.LEVEL_TIP
    assert hit[0]["action"] == {"label": "下限 1024→1280MB",
                                "changes": {"min_avail_mb": 1280,
                                            "min_avail_adapt_done": True}}
    assert "2" in hit[0]["text"] and "1024 → 1280" in hit[0]["text"]


def test_frequent_deepen_requires_majority_and_floor():
    """加深占比过半且至少 3 次才算「频繁」；档位已激进则无上升空间，返回 False。"""
    base = {"clean_level": "conservative", "stats": {"count": 10, "deepen": 6}}
    assert advisor.frequent_deepen(base) is True
    fewer = {**base, "stats": {"count": 10, "deepen": 2}}
    assert advisor.frequent_deepen(fewer) is False
    under_half = {**base, "stats": {"count": 10, "deepen": 3}}
    assert advisor.frequent_deepen(under_half) is False
    minority = {**base, "stats": {"count": 7, "deepen": 3}}
    assert advisor.frequent_deepen(minority) is False
    assert advisor.frequent_deepen({**base, "stats": {"count": 6, "deepen": 3}}) is True
    assert advisor.frequent_deepen({**base, "stats": {}}) is False
    assert advisor.frequent_deepen({**base, "clean_level": "aggressive"}) is False


def test_analyze_advises_aggressive_when_deepen_frequent():
    """加深频繁触发：先挑大户再撒宽每次都要走第二轮，建议一次清到位。"""
    cfg = normalize_config({"stats": {"count": 10, "deepen": 6}})
    items = advisor.analyze(cfg, _mem())
    hit = [i for i in items if "频繁加深" in i["title"]]
    assert hit
    assert hit[0]["action"] == {"label": "改用激进档",
                                "changes": {"clean_level": "aggressive"}}
    assert "6" in hit[0]["text"]


def test_analyze_silent_when_deepen_rare():
    """偶尔加深一次是正常现象（撒宽一轮就按住），不该制造配置调整噪音。"""
    cfg = normalize_config({"stats": {"count": 100, "deepen": 2}})
    items = advisor.analyze(cfg, _mem())
    assert all("频繁加深" not in i["title"] for i in items)


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

# ------------- 3d. 定向覆盖面自调优（v1.15.0：大户榜集中在同几个进程才建议加宽）


def _narrow_history():
    """6 份采样里大户榜每次都是同三个进程（跨度 500s）：集中度 1.0。"""
    return [(1000.0 + i * 100.0,
             {1: ("a.exe", 4 * GB), 2: ("b.exe", 3 * GB), 3: ("c.exe", 1 * GB)})
            for i in range(6)]


def test_narrow_top_coverage_requires_target_clean_and_evidence():
    """关定向 / 已是激进档 / 大户数到顶或非正 / 没有采样：一律判「不窄」。"""
    hist = _narrow_history()
    assert advisor.narrow_top_coverage(normalize_config({"target_clean_top": 3}),
                                       hist) is True
    assert advisor.narrow_top_coverage(
        normalize_config({"target_clean": False}), hist) is False
    assert advisor.narrow_top_coverage(
        normalize_config({"clean_level": "aggressive"}), hist) is False
    assert advisor.narrow_top_coverage(
        normalize_config({"target_clean_top": advisor.TARGET_CLEAN_TOP_MAX}),
        hist) is False
    # 脏 top 走不到采样：裸 dict 不做 clamp，0 直接判负
    assert advisor.narrow_top_coverage(
        {"target_clean": True, "target_clean_top": 0}, hist) is False
    assert advisor.narrow_top_coverage(
        normalize_config({"target_clean_top": 3}), None) is False
    assert advisor.narrow_top_coverage(
        normalize_config({"target_clean_top": 3}), []) is False

    # 大户数已到上限：连建议都不出，改不动的东西不占建议位
    cfg = normalize_config({"target_clean_top": advisor.TARGET_CLEAN_TOP_MAX})
    items = advisor.analyze(cfg, _mem(), [], history=hist)
    assert not any("集中在同几个进程" in i["title"] for i in items)


def test_analyze_advises_wider_top_when_names_cluster():
    """集中度够了就建议大户数 +1：带一键应用动作，文案点明占比与目标值。"""
    cfg = normalize_config({"target_clean_top": 3})
    items = advisor.analyze(cfg, _mem(), [], history=_narrow_history())
    hit = [i for i in items if "集中在同几个进程" in i["title"]]
    assert len(hit) == 1
    assert hit[0]["level"] == advisor.LEVEL_TIP
    assert hit[0]["action"] == {"label": "大户数 3→4",
                                "changes": {"target_clean_top": 4}}
    assert "3 → 4" in hit[0]["text"] and "100% 的位次" in hit[0]["text"]


def test_analyze_silent_when_top_coverage_wide():
    """大户每份都在换人（只有一份长期在榜）：集中度不够，不给这条建议。"""
    hist = [(1000.0 + i * 100.0,
             {1: ("keep.exe", 4 * GB), 2: (f"big{i}.exe", 3 * GB)})
            for i in range(6)]
    cfg = normalize_config({"target_clean_top": 2})
    items = advisor.analyze(cfg, _mem(), [], history=hist)
    assert not any("集中在同几个进程" in i["title"] for i in items)


# ------------- 3g. 定向大户下限自调优（v1.19.0：榜上有人却够不着下限，把下限降一档）


def _minmb_history():
    """6 份采样里大户榜每次都是同三个进程（400/300/200MB，跨度 500s）：中位数 300MB。"""
    return [(1000.0 + i * 100.0,
             {1: ("a.exe", 400 * 1024 ** 2), 2: ("b.exe", 300 * 1024 ** 2),
              3: ("c.exe", 200 * 1024 ** 2)})
            for i in range(6)]


def _minmb_cfg(**kw):
    """构造一份「定向大户下限自适应开着、还没降过」的归一化配置，可按关键字覆盖单项。"""
    base = {"target_clean": True, "target_clean_top": 3,
            "target_clean_min_mb": 1024, "min_mb_adapt": True,
            "min_mb_adapt_done": False,
            "stats": {"count": 3, "freed": 0, "bg_trimmed": 1}}
    base.update(kw)
    return normalize_config(base)


def test_unreachable_top_threshold_requires_every_gate():
    """逐门验假：任一门不过都判「够得着」，没有证据就不动下限。"""
    hist = _minmb_history()
    assert advisor.unreachable_top_threshold(_minmb_cfg(), hist) is True
    for kw in ({"target_clean": False}, {"clean_level": "aggressive"},
               {"min_mb_adapt": False}, {"min_mb_adapt_done": True},
               {"target_clean_min_mb": 128},
               # 中位数 300MB 正好踩线：够得着，不是够不着
               {"target_clean_min_mb": 300},
               {"stats": {"count": 3, "targeted": 1}},
               {"stats": {"count": 2}},
               {"stats": {"count": 3}}):
        assert advisor.unreachable_top_threshold(
            _minmb_cfg(**kw), hist) is False, kw
    # 脏大户数走不到采样：裸 dict 不 clamp，0 / 越上限 / 非数字直接判负
    for dirty_top in (0, 11, "x"):
        bare = {"target_clean": True, "target_clean_top": dirty_top,
                "target_clean_min_mb": 1024, "min_mb_adapt": True}
        assert advisor.unreachable_top_threshold(bare, hist) is False, dirty_top
    # 没有 stats 的裸 dict：没有累计清理证据，一样不动下限
    bare = {"target_clean": True, "target_clean_top": 3,
            "target_clean_min_mb": 1024, "min_mb_adapt": True}
    assert advisor.unreachable_top_threshold(bare, hist) is False
    # 大户每份都在换人：榜上根本没钉住谁
    rotating = [(1000.0 + i * 100.0, {1: (f"p{i}.exe", 400 * 1024 ** 2)})
                for i in range(6)]
    assert advisor.unreachable_top_threshold(_minmb_cfg(), rotating) is False
    # 没有采样可看
    for empty in (None, []):
        assert advisor.unreachable_top_threshold(_minmb_cfg(), empty) is False


def test_analyze_advises_lower_min_mb_when_top_unreachable():
    """榜上有人却够不着下限：出 3g 一键降下限；覆盖面建议让位（先降下限再谈撒网）。"""
    hist = _minmb_history()
    items = advisor.analyze(_minmb_cfg(), _mem(), [], history=hist)
    hit = [i for i in items if "定向大户下限偏高" in i["title"]]
    assert len(hit) == 1
    assert hit[0]["level"] == advisor.LEVEL_TIP
    assert hit[0]["action"] == {
        "label": "下限 1024→512MB",
        "changes": {"target_clean_min_mb": 512, "min_mb_adapt_done": True}}
    assert "1024 → 512" in hit[0]["text"]
    assert "后台" in hit[0]["text"] and "一次都没定向清到" in hit[0]["text"]
    assert not any("集中在同几个进程" in i["title"] for i in items)
    assert advisor.narrow_top_coverage(_minmb_cfg(), hist) is False


def test_analyze_silent_on_min_mb_when_top_reachable():
    """中位数够得着下限：这条建议不出；榜仍钉在同几个进程，覆盖面建议照旧。"""
    big = [(1000.0 + i * 100.0,
            {1: ("a.exe", 4 * GB), 2: ("b.exe", 3 * GB), 3: ("c.exe", 1 * GB)})
           for i in range(6)]
    items = advisor.analyze(_minmb_cfg(), _mem(), [], history=big)
    assert not any("定向大户下限偏高" in i["title"] for i in items)
    hit = [i for i in items if "集中在同几个进程" in i["title"]]
    assert len(hit) == 1
    assert hit[0]["action"]["changes"] == {"target_clean_top": 4}
# -------- 3h. 定向加深轮数自调优（v1.20.0：加深被轮数上限卡住，把轮数上限 +1）


def _rsat_cfg(**kw):
    """构造一份「加深轮数自适应开着、还没加过」的归一化配置，可按关键字覆盖单项。"""
    base = {"target_clean": True, "stage_deepen": True,
            "stage_deepen_rounds": 1, "deepen_rounds_adapt": True,
            "deepen_rounds_adapt_done": 0,
            "stats": {"count": 5, "freed": 0, "deepen_capped": 3}}
    base.update(kw)
    return normalize_config(base)


def test_deepen_round_saturated_requires_every_gate():
    """逐门验假：任一门不过都判「没卡住」，没有证据就不加轮数。"""
    assert advisor.deepen_round_saturated(_rsat_cfg()) is True
    for kw in ({"target_clean": False}, {"stage_deepen": False},
               {"clean_level": "aggressive"}, {"deepen_rounds_adapt": False},
               # 已经爬到顶：没有更高可加
               {"stage_deepen_rounds": 3},
               {"deepen_rounds_adapt_done": 3},
               {"stats": {"count": 5, "deepen_capped": 2}},
               {"stats": {"count": 5}}):
        assert advisor.deepen_round_saturated(_rsat_cfg(**kw)) is False, kw
    # 脏统计值读不出次数：一律当没证据（归一化前的裸 dict）
    for dirty in ("x", None, {}, [1], float("nan")):
        assert advisor.deepen_round_saturated(
            {"target_clean": True, "stage_deepen": True,
             "deepen_rounds_adapt": True, "stage_deepen_rounds": 1,
             "stats": {"deepen_capped": dirty}}) is False, dirty
    # 连 stats 都没有：一样不动轮数
    assert advisor.deepen_round_saturated(
        {"target_clean": True, "stage_deepen": True,
         "deepen_rounds_adapt": True, "stage_deepen_rounds": 1}) is False


def test_analyze_advises_one_more_deepen_round_when_saturated():
    """加深反复把轮数跑满仍没按住：出 3h 一键把轮数上限 +1。"""
    items = advisor.analyze(_rsat_cfg(), _mem(), [])
    hit = [i for i in items if "定向加深被轮数上限卡住" in i["title"]]
    assert len(hit) == 1
    assert hit[0]["level"] == advisor.LEVEL_TIP
    assert hit[0]["action"] == {
        "label": "加深轮数 1→2",
        "changes": {"stage_deepen_rounds": 2, "deepen_rounds_adapt_done": 2}}
    assert "加深轮数上限 1 → 2" in hit[0]["text"]
    assert "3 次" in hit[0]["text"]


def test_analyze_silent_on_deepen_rounds_when_not_saturated():
    """卡住次数不够 3 次：这条建议不出，别为一次抖动加轮数。"""
    items = advisor.analyze(_rsat_cfg(stats={"count": 5, "deepen_capped": 2}),
                            _mem(), [])
    assert not any("定向加深被轮数上限卡住" in i["title"] for i in items)


# -------- 3i. 预防窗口自调优（v1.21.0：预防式清理没防住，预测窗口加宽一档）


def _pwnd_cfg(**kw):
    """构造一份「趋势预防式清理开着、预防窗口自适应还没加宽过」的归一化配置，可按关键字覆盖单项。"""
    base = {"predict_clean": True, "predict_window_min": 5,
            "predict_window_adapt": True, "predict_window_adapt_done": 0,
            "stats": {"count": 5, "freed": 0, "preventive_missed": 3}}
    base.update(kw)
    return normalize_config(base)


def test_predict_window_saturated_requires_every_gate():
    """逐门验假：任一门不过都判「没防住」，没有证据就不加宽窗口。"""
    assert advisor.predict_window_saturated(_pwnd_cfg()) is True
    for kw in ({"predict_clean": False}, {"predict_window_adapt": False},
               {"clean_level": "aggressive"},
               # 窗口已经到顶：没有更宽可加
               {"predict_window_min": 30},
               # 已经自动加宽到过上限
               {"predict_window_adapt_done": 30},
               {"stats": {"count": 5, "preventive_missed": 2}},
               {"stats": {"count": 5}}):
        assert advisor.predict_window_saturated(_pwnd_cfg(**kw)) is False, kw
    # 脏统计值读不出次数：一律当没证据（归一化前的裸 dict）
    for dirty in ("x", None, {}, [1], float("nan")):
        assert advisor.predict_window_saturated(
            {"predict_clean": True, "predict_window_adapt": True,
             "predict_window_min": 5,
             "stats": {"preventive_missed": dirty}}) is False, dirty
    # 连 stats 都没有：一样不动窗口
    assert advisor.predict_window_saturated(
        {"predict_clean": True, "predict_window_adapt": True,
         "predict_window_min": 5}) is False


def test_analyze_advises_wider_predict_window_when_misses_recurrent():
    """预防式清理反复没防住：出 3i 一键把预测窗口加宽一档。"""
    items = advisor.analyze(_pwnd_cfg(), _mem(), [])
    hit = [i for i in items if "预防式清理没防住" in i["title"]]
    assert len(hit) == 1
    assert hit[0]["level"] == advisor.LEVEL_TIP
    assert hit[0]["action"] == {
        "label": "预测窗口 5→10分钟",
        "changes": {"predict_window_min": 10, "predict_window_adapt_done": 10}}
    assert "预测窗口 5 → 10 分钟" in hit[0]["text"]
    assert "3 次" in hit[0]["text"]


def test_analyze_silent_on_predict_window_when_few_misses():
    """没防住的次数不够 3 次：这条建议不出，别为一次抖动把窗口加宽。"""
    items = advisor.analyze(_pwnd_cfg(stats={"count": 5, "preventive_missed": 2}),
                            _mem(), [])
    assert not any("预防式清理没防住" in i["title"] for i in items)


# -------- 3j. 触发阈值自调优（v1.22.0：温和手段用尽仍偏短，两条触发线各降一档）


def _thrsh_cfg(**kw):
    """构造一份「触发线自适应开着、还没降过」的归一化配置，可按关键字覆盖单项。

    隔离三条互斥判据：短效未过半（那条该改用激进）、提前量已落闩（那条该提前判定）、
    没有低内存证据（那条该抬下限）——同一份短效证据只剩「动触发线」这一条出路。
    """
    base = {"phys_threshold": 85, "commit_threshold": 90,
            "threshold_adapt": True, "threshold_adapt_done": 0,
            "headroom_adapt_done": True,
            "stats": {"count": 10, "freed": 0, "short_relief": 3,
                      "escalated": 1}}
    base.update(kw)
    return normalize_config(base)


def test_threshold_too_late_requires_every_gate():
    """逐门验假：任一门不过都判「触发线画得合适」，没有空间就不下结论。"""
    assert advisor.threshold_too_late(_thrsh_cfg()) is True
    for kw in ({"threshold_adapt": False},
               # 已经降满两次：两次还压不住就该上激进档或用户介入
               {"threshold_adapt_done": 2},
               # 物理线再降一步就跌破兜底下限
               {"phys_threshold": 64},
               # 提交线再降一步就跌破兜底下限
               {"commit_threshold": 69},
               # 短效次数不够：一次偏短只是抖动
               {"stats": {"count": 10, "short_relief": 1, "escalated": 1}},
               # 温和阶梯从没用尽：没有 escalated / sticky 证据
               {"stats": {"count": 10, "short_relief": 3}},
               # 短效过半：那份证据更该「一次清到位」，不由触发线管
               {"stats": {"count": 5, "short_relief": 4, "escalated": 1}},
               # 提前量还有空间：那条判据仍适用，这里让位
               {"headroom_adapt_done": False, "target_headroom_pct": 0},
               # 低内存证据足够：那条判据仍适用，这里让位
               {"min_avail_mb": 1024,
                "stats": {"count": 10, "short_relief": 3, "escalated": 1,
                          "low_mem": 2}}):
        assert advisor.threshold_too_late(_thrsh_cfg(**kw)) is False, kw
    # 脏统计值读不出次数：一律当没证据（归一化前的裸 dict）
    for dirty in ("x", None, {}, [1], float("nan")):
        assert advisor.threshold_too_late(
            {"threshold_adapt": True, "phys_threshold": 85,
             "commit_threshold": 90, "headroom_adapt_done": True,
             "stats": {"short_relief": dirty, "escalated": 1}}) is False, dirty
    # 连 stats 都没有：一样不动触发线
    assert advisor.threshold_too_late(
        {"threshold_adapt": True, "phys_threshold": 85, "commit_threshold": 90,
         "headroom_adapt_done": True}) is False


def test_threshold_too_late_ignores_sticky_only_evidence():
    """只粘滞激进却没升过档、也没短效证据：不算「线画得偏高」。"""
    cfg = _thrsh_cfg(stats={"count": 10, "sticky": 1})
    assert advisor.threshold_too_late(cfg) is False


def test_analyze_advises_lower_thresholds_when_ladder_exhausted():
    """温和手段用尽仍效果偏短：出 3j 一键把两条触发线各降一档。"""
    items = advisor.analyze(_thrsh_cfg(), _mem(), [])
    hit = [i for i in items if "温和手段用尽" in i["title"]]
    assert len(hit) == 1
    assert hit[0]["level"] == advisor.LEVEL_TIP
    assert hit[0]["action"] == {
        "label": "触发线 85→80%/90→85%",
        "changes": {"phys_threshold": 80, "commit_threshold": 85,
                    "threshold_adapt_done": 1}}
    assert "85 → 80% / 90 → 85%" in hit[0]["text"]


def test_analyze_silent_on_thresholds_when_evidence_thin():
    """短效次数不够：这条建议不出，别为一次抖动把触发线往下挪。"""
    items = advisor.analyze(
        _thrsh_cfg(stats={"count": 10, "short_relief": 1, "escalated": 1}),
        _mem(), [])
    assert not any("温和手段用尽" in i["title"] for i in items)


def test_analyze_silent_on_thresholds_when_other_advice_applies():
    """同一份短效证据只出一条补偿：该改用激进 / 该提前量时这里让位。"""
    for kw in ({"stats": {"count": 5, "short_relief": 4, "escalated": 1}},
               {"headroom_adapt_done": False, "target_headroom_pct": 0},
               # 绝对下限偏低那条判据仍适用（短效 + 低内存各 2 次起）
               {"min_avail_mb": 1024,
                "stats": {"count": 10, "short_relief": 3, "escalated": 1,
                          "low_mem": 2}}):
        items = advisor.analyze(_thrsh_cfg(**kw), _mem(), [])
        assert not any("温和手段用尽" in i["title"] for i in items), kw
