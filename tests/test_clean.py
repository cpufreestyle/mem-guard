# -*- coding: utf-8 -*-
"""clean / actions：清理动作常量绑定回归、非管理员分支、Top 进程统计。

关键回归点：`Memory*` 三个命令常量若漏导入，非管理员路径永不触发不会暴露，
真正的管理员清理才会在调用时抛 NameError。这里通过拦截底层调用并断言常量
被正确引用，在不真正清理内存的前提下挡住该类回归。
"""
import json
from types import SimpleNamespace

from memguard import actions, clean, config
from memguard.config import normalize_config
from memguard.winapi import (
    MemoryEmptyWorkingSets,
    MemoryFlushModifiedList,
    MemoryPurgeLowPriorityStandbyList,
    MemoryPurgeStandbyList,
)


def test_purge_actions_bind_memory_constants(monkeypatch):
    seen = []
    monkeypatch.setattr(actions, "_purge_list", lambda cmd: seen.append(cmd) or 0)
    assert actions.purge_working_sets() == 0
    assert actions.flush_modified_list() == 0
    assert actions.purge_standby_list() == 0
    assert seen == [MemoryEmptyWorkingSets, MemoryFlushModifiedList, MemoryPurgeStandbyList]


def test_memory_constants_visible_in_actions_namespace():
    """漏导入时这里会 AttributeError（比运行到管理员路径才发现更早）。"""
    assert actions.MemoryEmptyWorkingSets == 2
    assert actions.MemoryFlushModifiedList == 3
    assert actions.MemoryPurgeStandbyList == 4


def test_do_clean_requires_admin(monkeypatch):
    monkeypatch.setattr(clean, "is_admin", lambda: False)
    r = clean.do_clean("测试")
    assert r["ok"] is False
    assert "管理员" in r["msg"]


def test_top_processes_list_shape():
    rows = clean.top_processes_list(5)
    assert len(rows) <= 5
    for name, rss, pid in rows:
        assert isinstance(name, str)
        assert isinstance(rss, int)
        assert isinstance(pid, int)


def test_top_processes_text_lines():
    assert len(clean.top_processes(3).splitlines()) <= 3


def test_low_priority_standby_binds_constant(monkeypatch):
    """回归：低优先级 standby 动作必须正确引用命令常量，否则会 NameError。"""
    seen = []
    monkeypatch.setattr(actions, "_purge_list", lambda cmd: seen.append(cmd) or 0)
    assert actions.purge_low_priority_standby() == 0
    assert seen == [MemoryPurgeLowPriorityStandbyList]


# ---------------------------------------------------------------- 进程统计快路径

class _NoPsutil:
    """快路径命中时不应该碰 psutil（其全进程扫描是 1s 级开销）。"""

    def __getattr__(self, name):
        raise AssertionError(f"快路径不应使用 psutil（访问了 {name}）")


def test_top_processes_list_sorts_fast_snapshot(monkeypatch):
    """快路径结果按 RSS 降序截断，且不调用 psutil。"""
    rows = [("b.exe", 200, 2), ("a.exe", 300, 1), ("c.exe", 100, 3), ("d.exe", 50, 4)]
    monkeypatch.setattr(clean, "process_working_sets", lambda: rows)
    monkeypatch.setattr(clean, "psutil", _NoPsutil())
    assert clean.top_processes_list(3) == [("a.exe", 300, 1), ("b.exe", 200, 2), ("c.exe", 100, 3)]


def test_top_processes_list_fills_denied_pids_with_psutil(monkeypatch):
    """受保护进程打不开句柄（rss=0），只对这些 PID 用 psutil 定点补齐后再排序。"""
    rows = [("big.exe", 900, 1), ("MsMpEng.exe", 0, 2)]
    filled = []
    monkeypatch.setattr(clean, "process_working_sets", lambda: rows)

    class FakePsutil:
        @staticmethod
        def Process(pid):
            filled.append(pid)
            return SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=500))

    monkeypatch.setattr(clean, "psutil", FakePsutil())
    assert clean.top_processes_list(10) == [("big.exe", 900, 1), ("MsMpEng.exe", 500, 2)]
    assert filled == [2]


def test_top_processes_list_skips_fill_when_too_many_denied(monkeypatch):
    """句柄被拒的进程过多（非管理员跑在别人的会话上）时整体不补齐，避免退化到秒级。"""
    rows = [("ok.exe", 500, 1)] + [(f"p{i}.exe", 0, i + 2) for i in range(clean._FILL_MAX + 1)]
    monkeypatch.setattr(clean, "process_working_sets", lambda: rows)
    monkeypatch.setattr(clean, "psutil", _NoPsutil())
    assert clean.top_processes_list(10) == [("ok.exe", 500, 1)]


def test_top_processes_list_falls_back_when_snapshot_fails(monkeypatch):
    """快路径整体失败（快照 API 异常）时回退到原有 psutil 实现。"""
    def boom():
        raise OSError("snapshot unavailable")
    monkeypatch.setattr(clean, "process_working_sets", boom)

    class Row:
        def __init__(self, name, rss, pid):
            self.info = {"name": name, "memory_info": SimpleNamespace(rss=rss)}
            self.pid = pid

    monkeypatch.setattr(clean.psutil, "process_iter",
                        lambda attrs: [Row("x.exe", 111, 9), Row("y.exe", 222, 8)])
    assert clean.top_processes_list(10) == [("y.exe", 222, 8), ("x.exe", 111, 9)]


def test_do_clean_areas_all_off_not_reported_failed(monkeypatch):
    """所有核心区域关闭时不应误报失败（非管理员直接早返回，这里强制管理员分支关闭）。"""
    monkeypatch.setattr(clean, "is_admin", lambda: False)
    r = clean.do_clean("测试")
    assert r["ok"] is False and "管理员" in r["msg"]


# ---------------------------------------------------------------- 清理后的等待与统计

class _FakeClock:
    """替掉 time.time / time.sleep，让轮询节奏可断言（毫秒不差）。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += sec


def test_wait_avail_rise_returns_as_soon_as_memory_recovers(monkeypatch):
    """可用内存一回升就返回，不再把等待窗口耗完。"""
    clock = _FakeClock()
    monkeypatch.setattr(clean, "time", clock)
    samples = iter([{"avail_phys": 100}, {"avail_phys": 100}, {"avail_phys": 200}])
    monkeypatch.setattr(clean, "get_mem", lambda: next(samples))

    after = clean._wait_avail_rise({"avail_phys": 100})

    assert after["avail_phys"] == 200
    assert abs(clock.now - 1000.0 - 2 * clean._RECLAIM_SAMPLE) < 1e-9


def test_wait_avail_rise_bails_out_when_nothing_reclaimed(monkeypatch):
    """什么都没释放时提前收尾，不等满整个窗口（旧实现会等满 1.5s）。"""
    clock = _FakeClock()
    monkeypatch.setattr(clean, "time", clock)
    monkeypatch.setattr(clean, "get_mem", lambda: {"avail_phys": 100})

    after = clean._wait_avail_rise({"avail_phys": 100})

    assert after["avail_phys"] == 100
    elapsed = clock.now - 1000.0
    assert clean._RECLAIM_SETTLE - 1e-9 <= elapsed < clean._RECLAIM_WINDOW


def test_bump_stats_accumulates_and_persists(monkeypatch, tmp_path):
    """累计次数 +1、释放量累加；释放量为负时统计不下跌，别的字段不被冲掉。"""
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text(
        json.dumps({"stats": {"count": 3, "freed": 100}, "warn_margin": 20}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    assert clean._bump_stats(50) == {"count": 4, "freed": 150}
    assert clean._bump_stats(-50) == {"count": 5, "freed": 150}

    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["stats"] == {"count": 5, "freed": 150}
    assert on_disk["warn_margin"] == 20, "统计写盘不能把用户手改的字段冲掉"


def test_bump_stats_uses_fresh_config_under_concurrent_write(monkeypatch, tmp_path):
    """清理统计读-改-写期间别人改了配置：不能把别人的改动回滚掉。"""
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text(json.dumps({"stats": {"count": 1, "freed": 0}, "scheduled_minutes": 0}),
                        encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    # 模拟：清理读到配置后，菜单先把定时清理改成了 30 分钟并落盘
    config.update_config({"scheduled_minutes": 30})
    clean._bump_stats(10)

    final = config.load_config()
    assert final["scheduled_minutes"] == 30, "与菜单写盘并发时不能覆盖对方的修改"
    assert final["stats"] == {"count": 2, "freed": 10}


# ---------------------------------------------------------------- do_clean 管理员路径
# 这条分支在非管理员机器上永不触发，v1.3.9 的 NameError（漏导入 Memory* 常量）
# 正是从这里漏掉的——所以这里显式 mock 管理员 + 特权窗口 + 各动作，把编排本身测住。

from contextlib import contextmanager


def _admin_env(monkeypatch, level="conservative", areas=None, results=None,
               escalate=None, wait_after=None, base_avail=None, base_commit=50.0,
               calls=None, min_avail=None, cfg_over=None, top_rows=None,
               esw_result=(1, 0), bg_trim=False, bg_result=(2, 0)):
    """把 do_clean 的外部依赖全部换成假的，只留编排逻辑本身。

    escalate=None 沿用 normalize_config 的默认（True）；wait_after 支持单个 dict
    （每次 _wait_avail_rise 都返回它）或按调用次序取样的 list——后者用来覆盖
    「再等一次」的升档路径。calls 若为 list，则把每次动作/等待调用记进去，便于
    断言升档确实触发了完整的第二遍。base_avail/base_commit 控制 before 采样。
    min_avail 设置「低内存触发」下限（MB），用于下限兜底的升档路径。
    cfg_over 直接覆盖归一化后的单项配置（如 {"target_clean": False}）；top_rows
    假造 Top 进程采样（默认全是小个进程，跑不到定向清理）；esw_result 是
    empty_selected_working_sets 的假返回值 (已清理数, 跳过数)。
    bg_trim 显式覆盖配置里的 bg_trim（默认 False：让不关心后台清理的旧阶梯测试
    保持原来的调用序列）；bg_result 是 empty_background_working_sets 的假返回值。
    """
    monkeypatch.setattr(clean, "is_admin", lambda: True)
    raw_cfg = {"clean_level": level}
    if bg_trim is not None:
        raw_cfg["bg_trim"] = bool(bg_trim)
    if areas is not None:
        raw_cfg["clean_areas"] = dict(areas)
    if escalate is not None:
        raw_cfg["escalate_clean"] = bool(escalate)
    if min_avail is not None:
        raw_cfg["min_avail_mb"] = int(min_avail)
    raw_cfg.update(cfg_over or {})
    cfg = normalize_config(raw_cfg)
    monkeypatch.setattr(clean, "load_config", lambda: cfg)

    @contextmanager
    def fake_privileges():
        yield []

    monkeypatch.setattr(clean, "clean_privileges", fake_privileges)
    def _rec(tag, ret):
        if calls is not None:
            calls.append(tag)
        return ret
    monkeypatch.setattr(clean, "purge_working_sets",
                        lambda: _rec("pw", (results or {}).get("working_sets", 0)))
    monkeypatch.setattr(clean, "flush_modified_list",
                        lambda: _rec("fm", (results or {}).get("modified", 0)))
    monkeypatch.setattr(clean, "purge_standby_list",
                        lambda: _rec("sb", (results or {}).get("standby", 0)))
    monkeypatch.setattr(clean, "purge_low_priority_standby",
                        lambda: _rec("lp", (results or {}).get("low_priority_standby", 0)))
    monkeypatch.setattr(clean, "clear_system_file_cache",
                        lambda: _rec("fc", bool((results or {}).get("file_cache", True))))
    monkeypatch.setattr(clean, "empty_process_working_sets",
                        lambda bl: _rec("epw", (results or {}).get("proc", (3, 1))))
    # 定向清理（v1.7.0）：Top 采样与「只清指定 PID」全部造假，绝不碰真实进程表
    rows = [("small.exe", 100 * 1024 ** 2, 9)] if top_rows is None else top_rows
    monkeypatch.setattr(clean, "top_processes_list", lambda n=15: list(rows))
    monkeypatch.setattr(clean, "empty_selected_working_sets",
                        lambda pids, bl: _rec("esw", esw_result))
    # 后台进程工作集清理（v1.11.0）：窗口枚举与逐进程清空全部造假
    monkeypatch.setattr(clean, "visible_window_pids", lambda: {4242, 99})
    monkeypatch.setattr(clean, "empty_background_working_sets",
                        lambda exclude, bl: _rec("ebw", bg_result))
    av0 = 8 * 1024 ** 3 if base_avail is None else base_avail
    monkeypatch.setattr(clean, "get_mem",
                        lambda: {"avail_phys": av0, "commit_pct": base_commit})
    if wait_after is None:
        def _wait(before):
            if calls is not None:
                calls.append("wait")
            return {"avail_phys": before["avail_phys"] + 1024, "commit_pct": 40.0}
    elif isinstance(wait_after, list):
        _it = iter(wait_after)

        def _wait(before):
            if calls is not None:
                calls.append("wait")
            try:
                return next(_it)
            except StopIteration:
                return wait_after[-1]
    else:
        def _wait(before):
            if calls is not None:
                calls.append("wait")
            return wait_after
    monkeypatch.setattr(clean, "_wait_avail_rise", _wait)
    monkeypatch.setattr(clean, "_bump_stats",
                        lambda freed, escalated=False, targeted=False, preventive=False,
                        short_relief=False, bg_trimmed=False, sticky=False,
                        deepen_rounds=0:
                            {"count": 9, "freed": freed})
    monkeypatch.setattr(clean, "log", lambda msg: None)   # 别往真实日志文件里写测试噪声


def test_do_clean_conservative_skips_working_sets(monkeypatch):
    _admin_env(monkeypatch)
    r = clean.do_clean("测试")

    assert r["ok"] is True
    assert r["level"] == "conservative"
    assert r["freed"] == 1024
    assert "进程工作集:已跳过(保守档)" in r["detail"]
    assert "清理Standby:成功" in r["detail"]
    assert "刷写修改页:成功" in r["detail"]
    assert r["stats"] == {"count": 9, "freed": 1024}


def test_do_clean_aggressive_empties_process_working_sets(monkeypatch):
    _admin_env(monkeypatch, level="aggressive")
    r = clean.do_clean("测试")

    assert r["level"] == "aggressive"
    assert "清空工作集:成功" in r["detail"]
    assert "进程工作集:3 个（白名单跳过 1）" in r["detail"]


def test_do_clean_reports_disabled_areas(monkeypatch):
    areas = {"standby": False, "modified": False, "file_cache": False, "working_sets": True}
    _admin_env(monkeypatch, level="aggressive", areas=areas)
    r = clean.do_clean("测试")

    # 全部核心区域被关闭：不算失败，明细如实写"已关闭"
    assert r["ok"] is True
    assert "清理Standby:已关闭" in r["detail"]
    assert "刷写修改页:已关闭" in r["detail"]
    assert "文件缓存:已关闭" in r["detail"]


def test_do_clean_fails_when_all_core_calls_fail(monkeypatch):
    _admin_env(monkeypatch, results={"modified": 0xC0000061, "standby": 0xC0000061})
    r = clean.do_clean("测试")

    assert r["ok"] is False
    assert "清理失败" in r["msg"]


def test_do_clean_partial_failure_still_counts_as_success(monkeypatch):
    """核心动作只失败了一部分（如系统不支持低优先级 standby）时不应判负。"""
    _admin_env(monkeypatch, results={"low_priority_standby": 0xC0000001})
    r = clean.do_clean("测试")

    assert r["ok"] is True
    assert "清理低优先级Standby:失败" in r["detail"]


def test_do_clean_normalizes_bad_level(monkeypatch):
    """传入非法档位时按保守处理，不会把 level 传成怪值。"""
    _admin_env(monkeypatch)
    assert clean.do_clean("测试", level="xxx")["level"] == "conservative"


# ---------------------------------------------------------------- 自动升档清理（v1.5.1）

def test_do_clean_escalates_when_still_pressured(monkeypatch):
    """保守清理后仍超阈值：自动补一次激进清理，level 报激进，释放量按 before→最终 after 合计。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 第一遍后仍受压
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},   # 升档后回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls)
    r = clean.do_clean("测试")

    assert r["escalated"] is True
    assert r["level"] == "aggressive"
    assert r["freed"] == 4096
    assert "升档重效(激进)" in r["detail"]
    assert "清空工作集:成功" in r["detail"]       # 第二遍（激进）真的跑了
    assert calls.count("epw") == 1                # 只在升档的第二遍清进程工作集
    assert calls.count("wait") == 2               # 保守一遍 + 升档再等一次


def test_do_clean_no_escalate_when_disabled(monkeypatch):
    """开关关掉（escalate_clean=False）：即便仍受压也只做保守清理，不升档。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 95.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, escalate=False, wait_after=wait_after, calls=calls)
    r = clean.do_clean("测试")

    assert r["escalated"] is False
    assert r["level"] == "conservative"
    assert calls.count("epw") == 0
    assert calls.count("wait") == 1


def test_do_clean_no_escalate_when_relieved(monkeypatch):
    """保守清理已释放到位（不再超阈值）：不升档。"""
    base = 8 * 1024 ** 3
    wait_after = [{"avail_phys": base + 2048, "phys_pct": 60.0, "commit_pct": 40.0}]
    calls = []
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls)
    r = clean.do_clean("测试")

    assert r["escalated"] is False
    assert r["level"] == "conservative"
    assert calls.count("epw") == 0
    assert calls.count("wait") == 1


def test_do_clean_no_escalate_when_already_aggressive(monkeypatch):
    """本就是激进档：不会再补第二遍（not aggressive 为假，升档分支不进）。"""
    base = 8 * 1024 ** 3
    wait_after = [{"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 95.0}]
    calls = []
    _admin_env(monkeypatch, level="aggressive", wait_after=wait_after, calls=calls)
    r = clean.do_clean("测试")

    assert r["escalated"] is False
    assert r["level"] == "aggressive"
    assert calls.count("epw") == 1     # 第一遍激进已清一次，不会再来一次
    assert calls.count("wait") == 1


def test_do_clean_escalates_when_below_min_avail_floor(monkeypatch):
    """配了下限的场景：保守清理后可用物理仍低于 min_avail_mb 也升档。

    百分比都没超（远低于阈值），只有下限兜底这一条路——这正是「低内存触发」
    用户要的效果：别保守一遍就收工。
    """
    base = 8 * 1024 ** 3
    calls = []
    # 9000MB ≈ 8.79GiB：两遍之后的可用量都够不着，纯靠下限触发升档
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 60.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 55.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, min_avail=9000)
    r = clean.do_clean("测试")

    assert r["escalated"] is True
    assert r["level"] == "aggressive"
    assert calls.count("epw") == 1
    assert calls.count("wait") == 2


def test_do_clean_no_escalate_when_above_min_avail_floor(monkeypatch):
    """下限够得着、百分比也没超：不因下限配置误升档。"""
    base = 9 * 1024 ** 3
    wait_after = [{"avail_phys": base + 1024, "phys_pct": 60.0, "commit_pct": 40.0}]
    calls = []
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, min_avail=1000)
    r = clean.do_clean("测试")

    assert r["escalated"] is False
    assert r["level"] == "conservative"
    assert calls.count("epw") == 0

def test_do_clean_escalation_feeds_stats_counter(monkeypatch):
    """升档被采纳时 _bump_stats 收到 escalated=True：统计行才知道这次是「补刀」清的。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 第一遍后仍受压
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},   # 升档后回落
    ]
    seen = []
    _admin_env(monkeypatch, wait_after=wait_after)
    monkeypatch.setattr(
        clean, "_bump_stats",
        lambda freed, escalated=False, targeted=False, preventive=False,
               short_relief=False, bg_trimmed=False, sticky=False, deepened=False:
            seen.append(escalated) or {"count": 9, "freed": freed})
    r = clean.do_clean("测试")

    assert r["escalated"] is True
    assert seen == [True]


def test_do_clean_no_escalation_feeds_stats_counter(monkeypatch):
    """保守一遍就到位：escalated=False，不虚增升档计数。"""
    base = 8 * 1024 ** 3
    wait_after = [{"avail_phys": base + 2048, "phys_pct": 60.0, "commit_pct": 40.0}]
    seen = []
    _admin_env(monkeypatch, wait_after=wait_after)
    monkeypatch.setattr(
        clean, "_bump_stats",
        lambda freed, escalated=False, targeted=False, preventive=False,
               short_relief=False, bg_trimmed=False, sticky=False, deepened=False:
            seen.append(escalated) or {"count": 9, "freed": freed})
    r = clean.do_clean("测试")

    assert r["escalated"] is False
    assert seen == [False]


# ---------------------------------------------------------------- 定向清理内存大户（v1.7.0）

def _big_rows():
    """假造的 Top 采样：两个够着 1GB 阈值的大户 + 一个够不着的。"""
    return [("big.exe", 2 * 1024 ** 3, 4242), ("mid.exe", 1500 * 1024 ** 2, 88),
            ("small.exe", 100 * 1024 ** 2, 9)]


def test_do_clean_targets_big_working_sets_when_still_pressured(monkeypatch):
    """保守清理后仍超阈值：先只清工作集最大的几个大户，不直接升档全清。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 保守一遍后仍受压
        {"avail_phys": base + 2048, "phys_pct": 60.0, "commit_pct": 40.0},   # 定向后回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_big_rows())
    r = clean.do_clean("测试")

    assert [n for n, _, _ in r["targeted"]] == ["big.exe", "mid.exe"]
    assert "定向清理大户(" in r["detail"] and "big.exe" in r["detail"]
    assert r["escalated"] is False and r["level"] == "conservative"
    assert calls.count("esw") == 1        # 只对挑出来的大户精确清
    assert calls.count("epw") == 0        # 没走到全量清进程工作集
    assert calls.count("wait") == 2       # 保守一遍 + 定向后再等一次


def test_do_clean_target_then_escalates_when_still_pressured(monkeypatch):
    """定向大户后仍不达标：才轮到升档全清——阶梯顺序不能反。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 仍受压 -> 定向
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},   # 定向后仍受压 -> 升档
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},   # 升档后回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_big_rows())
    r = clean.do_clean("测试")

    assert [n for n, _, _ in r["targeted"]] == ["big.exe", "mid.exe"]
    assert r["escalated"] is True and r["level"] == "aggressive"
    assert calls.count("esw") == 1 and calls.count("epw") == 1
    assert calls.count("wait") == 3


def test_do_clean_no_target_clean_when_disabled(monkeypatch):
    """开关关掉：仍受压也只按原逻辑升档，不做定向清理。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_big_rows(),
               cfg_over={"target_clean": False})
    r = clean.do_clean("测试")

    assert r["targeted"] == []
    assert calls.count("esw") == 0
    assert "定向清理大户" not in r["detail"]


def test_do_clean_no_target_clean_without_candidates(monkeypatch):
    """没有够着阈值的大户：定向这级直接跳过，只剩升档这一级。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls)   # 默认全是小个进程
    r = clean.do_clean("测试")

    assert r["targeted"] == []
    assert calls.count("esw") == 0
    assert r["escalated"] is True


def test_do_clean_no_target_clean_when_already_aggressive(monkeypatch):
    """本就是激进档（已全量清过）：定向这级没有存在意义，不再重复。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [{"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0}]
    _admin_env(monkeypatch, level="aggressive", wait_after=wait_after, calls=calls,
               top_rows=_big_rows())
    r = clean.do_clean("测试")

    assert r["targeted"] == []
    assert calls.count("esw") == 0
    assert calls.count("epw") == 1


def test_do_clean_no_target_clean_when_relieved(monkeypatch):
    """保守清理已释放到位：定向这级不触发。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [{"avail_phys": base + 2048, "phys_pct": 60.0, "commit_pct": 40.0}]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_big_rows())
    r = clean.do_clean("测试")

    assert r["targeted"] == []
    assert calls.count("esw") == 0
    assert calls.count("wait") == 1


def test_do_clean_target_feeds_stats_counter(monkeypatch):
    """定向被采纳时 _bump_stats 收到 targeted=True：统计行才知道这次是精确清的。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    seen = []

    def _fake_bump(freed, escalated=False, targeted=False, preventive=False,
                   short_relief=False, bg_trimmed=False, sticky=False, deepened=False):
        seen.append(targeted)
        return {"count": 9, "freed": freed}

    _admin_env(monkeypatch, wait_after=wait_after, top_rows=_big_rows())
    monkeypatch.setattr(clean, "_bump_stats", _fake_bump)
    r = clean.do_clean("测试")

    assert r["targeted"]
    assert seen == [True]


# ---------------------------------------- 定向加深（v1.13.0：升档前最后一道温和手段）

def _wide_rows():
    """假造的 Top 采样：三个够 1GB 大户线 + 三个够第一轮加深线(512MB)的中等进程。

    g/h 分别落在更深两轮的下限上（256MB / 128MB），用来验证加深逐轮放宽时
    「只挑还没碰过的进程」：第一轮只多出 d/e/f，第二轮才轮到 g，第三轮才轮到 h。
    """
    return [("a.exe", 4 * 1024 ** 3, 1), ("b.exe", 3 * 1024 ** 3, 2),
            ("c.exe", 2 * 1024 ** 3, 3), ("d.exe", 1500 * 1024 ** 2, 4),
            ("e.exe", 1000 * 1024 ** 2, 5), ("f.exe", 800 * 1024 ** 2, 6),
            ("g.exe", 600 * 1024 ** 2, 7), ("h.exe", 200 * 1024 ** 2, 8)]


def test_do_clean_deepens_target_when_still_pressured(monkeypatch):
    """定向清到了东西但压力没完全按住：把网撒宽一轮再清，不必升档全清。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 保守后仍受压
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},   # 定向后仍受压
        {"avail_phys": base + 3072, "phys_pct": 60.0, "commit_pct": 40.0},   # 加深后回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows())
    r = clean.do_clean("测试")

    assert [n for n, _, _ in r["targeted"]] == ["a.exe", "b.exe", "c.exe",
                                                "d.exe", "e.exe", "f.exe"]
    assert "定向加深(" in r["detail"] and "d.exe" in r["detail"]
    assert r["deepened"] == 1
    assert r["deepen_freed"] == 1024
    assert r["targeted_freed"] == 2048          # 定向 1024 + 加深 1024
    assert r["escalated"] is False and r["level"] == "conservative"
    assert calls.count("esw") == 2              # 定向一轮 + 加深一轮
    assert calls.count("epw") == 0              # 加深按住了，没走到全量清
    assert calls.count("wait") == 3             # 保守 + 定向 + 加深各等一次


def test_do_clean_deepen_then_escalates_when_still_pressured(monkeypatch):
    """加深后仍不达标：才轮到升档全清——加深是升档前的一道，不是替代。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 3072, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows())
    r = clean.do_clean("测试")

    assert r["deepened"] == 1 and r["escalated"] is True
    assert calls.count("esw") == 2 and calls.count("epw") == 1
    assert calls.count("wait") == 4
    assert calls.index("epw") > calls.index("esw")


def test_do_clean_no_deepen_when_disabled(monkeypatch):
    """stage_deepen 关掉：定向之后直接考虑升档，不撒宽第二轮。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows(),
               cfg_over={"stage_deepen": False})
    r = clean.do_clean("测试")

    assert "定向加深" not in r["detail"]
    assert r["deepened"] == 0 and r["deepen_freed"] == 0
    assert calls.count("esw") == 1 and calls.count("epw") == 1


def test_do_clean_no_deepen_when_target_freed_nothing(monkeypatch):
    """定向没释放出东西（零回升）：没有加深的立足点，直接走后面的级。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 零回升
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows())
    r = clean.do_clean("测试")

    assert "定向清理大户(" in r["detail"]        # 定向确实跑了且采纳成功
    assert "定向加深" not in r["detail"]          # 但没释放出东西，不加深
    assert r["deepened"] == 0
    assert calls.count("esw") == 1 and calls.count("epw") == 1


def test_do_clean_no_deepen_when_relieved_after_target(monkeypatch):
    """定向后压力已回落：加深这一道不需要出现，升档更不需要。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows())
    r = clean.do_clean("测试")

    assert r["targeted_freed"] > 0
    assert "定向加深" not in r["detail"]
    assert r["deepened"] == 0 and r["escalated"] is False
    assert calls.count("esw") == 1 and calls.count("wait") == 2


def test_do_clean_no_deepen_when_already_aggressive(monkeypatch):
    """激进档本就全量清过：整条阶梯（含加深）都没有存在意义。"""
    calls = []
    _admin_env(monkeypatch, level="aggressive", calls=calls, top_rows=_wide_rows())
    r = clean.do_clean("测试")

    assert r["deepened"] == 0
    assert "定向加深" not in r["detail"]
    assert calls.count("esw") == 0 and calls.count("epw") == 1


def test_do_clean_deepen_excludes_already_cleaned_pids(monkeypatch):
    """加深只挑上一轮没碰过的进程：同一个 PID 不会被清第二次。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 3072, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    esw_pids = []
    _admin_env(monkeypatch, wait_after=wait_after, top_rows=_wide_rows())
    monkeypatch.setattr(clean, "empty_selected_working_sets",
                        lambda pids, bl: (esw_pids.append(list(pids)), (1, 0))[1])
    r = clean.do_clean("测试")

    assert esw_pids == [[1, 2, 3], [4, 5, 6]]        # 加深一轮只碰 d/e/f
    assert r["deepened"] == 1


def test_do_clean_deepen_feeds_stats_counter(monkeypatch):
    """加深被采纳时 _bump_stats 收到 deepen_rounds=1：统计行与建议都靠它。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 3072, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    seen = []

    def _fake_bump(freed, escalated=False, targeted=False, preventive=False,
                   short_relief=False, bg_trimmed=False, sticky=False, deepen_rounds=0):
        seen.append(deepen_rounds)
        return {"count": 9, "freed": freed}

    _admin_env(monkeypatch, wait_after=wait_after, top_rows=_wide_rows())
    monkeypatch.setattr(clean, "_bump_stats", _fake_bump)
    r = clean.do_clean("测试")

    assert r["deepened"] == 1
    assert seen == [1]


# ------------------------------------- 加深多轮 + 降压余量（v1.14.0：温和手段用到极限）


def test_do_clean_deepen_runs_multiple_rounds_up_to_cap(monkeypatch):
    """stage_deepen_rounds=3：每轮只挑还没碰过的进程，一路撒到轮数上限。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 保守后仍受压
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},   # 定向后仍受压
        {"avail_phys": base + 3072, "phys_pct": 92.0, "commit_pct": 40.0},   # 第 2 轮仍受压
        {"avail_phys": base + 4096, "phys_pct": 92.0, "commit_pct": 40.0},   # 第 3 轮仍受压
        {"avail_phys": base + 5120, "phys_pct": 60.0, "commit_pct": 40.0},   # 跑满后才回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows(),
               cfg_over={"stage_deepen_rounds": 3})
    r = clean.do_clean("测试")

    # 三轮加深分别只挑到 g.exe / h.exe：候选数与下限每轮各放宽一次
    assert [n for n, _, _ in r["targeted"]] == ["a.exe", "b.exe", "c.exe", "d.exe",
                                                "e.exe", "f.exe", "g.exe", "h.exe"]
    assert r["deepen_rounds"] == 3
    assert r["deepened"] == 3                # 每轮各清到 1 个
    assert r["deepen_freed"] == 3 * 1024
    assert r["targeted_freed"] == 4 * 1024   # 定向 1024 + 加深 3×1024
    assert calls.count("esw") == 4           # 定向一轮 + 加深三轮
    assert calls.count("epw") == 0           # 加深就按住了，没升档
    assert calls.count("wait") == 5          # 定向 + 三轮加深各等一次（保守那次另算）
    assert "第 2 轮, g.exe" in r["detail"] and "第 3 轮, h.exe" in r["detail"]


def test_do_clean_deepen_stops_midway_when_relieved(monkeypatch):
    """多轮加深中途按住：后面的轮次不再跑，升档也不需要。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 3072, "phys_pct": 60.0, "commit_pct": 40.0},   # 第 2 轮后按住
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows(),
               cfg_over={"stage_deepen_rounds": 3})
    r = clean.do_clean("测试")

    assert r["deepen_rounds"] == 1 and r["deepened"] == 1
    assert "第 2 轮" not in r["detail"] and "第 3 轮" not in r["detail"]
    assert r["escalated"] is False and calls.count("wait") == 3


def test_do_clean_deepen_round_stops_when_it_frees_nothing(monkeypatch):
    """某一加深轮没清动：就地收尾，照常走后面的后台级与升档。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},   # 升档后回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows(),
               cfg_over={"stage_deepen_rounds": 3})
    esw_pids = []

    def _esw(pids, bl):
        esw_pids.append(list(pids))
        # 第一发（定向）成功，之后（加深轮）都失败
        return (1, 0) if len(esw_pids) == 1 else (0, 0)

    monkeypatch.setattr(clean, "empty_selected_working_sets", _esw)
    r = clean.do_clean("测试")

    assert esw_pids == [[1, 2, 3], [4, 5, 6]]      # 只试了一轮加深就收尾
    assert r["deepen_rounds"] == 0 and r["deepened"] == 0
    assert "定向加深(第 1 轮): 未成功" in r["detail"]
    assert r["escalated"] is True                   # 加深没按住，仍照常升档


def test_do_clean_deepen_stats_count_rounds(monkeypatch):
    """加深按轮累计：跑满 3 轮时 _bump_stats 收到 deepen_rounds=3。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 3072, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 5120, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    seen = []

    def _fake_bump(freed, escalated=False, targeted=False, preventive=False,
                   short_relief=False, bg_trimmed=False, sticky=False, deepen_rounds=0):
        seen.append(deepen_rounds)
        return {"count": 9, "freed": freed}

    _admin_env(monkeypatch, wait_after=wait_after, top_rows=_wide_rows(),
               cfg_over={"stage_deepen_rounds": 3})
    monkeypatch.setattr(clean, "_bump_stats", _fake_bump)
    r = clean.do_clean("测试")

    assert r["deepen_rounds"] == 3
    assert seen == [3]


def test_do_clean_headroom_keeps_ladder_going(monkeypatch):
    """降压余量 5 点：82% 也算没按住，定向与加深该跑还是跑。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 82.0, "commit_pct": 40.0},   # 余量内算受压
        {"avail_phys": base + 2048, "phys_pct": 81.0, "commit_pct": 40.0},
        {"avail_phys": base + 3072, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows(),
               cfg_over={"target_headroom_pct": 5})
    r = clean.do_clean("测试")

    assert "定向清理大户(" in r["detail"] and "定向加深(" in r["detail"]
    assert r["deepen_rounds"] == 1 and r["escalated"] is False
    assert calls.count("esw") == 2 and calls.count("wait") == 3


def test_do_clean_no_headroom_stops_after_conservative(monkeypatch):
    """同样的读数、余量为 0：82% 已低于 85% 阈值，阶梯一级都不跑。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [{"avail_phys": base + 1024, "phys_pct": 82.0, "commit_pct": 40.0}]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_wide_rows())
    r = clean.do_clean("测试")

    assert r["targeted"] == [] and r["deepen_rounds"] == 0
    assert "定向清理大户" not in r["detail"] and "定向加深" not in r["detail"]
    assert calls.count("esw") == 0 and calls.count("wait") == 1


def test_target_clean_candidates_filters_and_truncates():
    """候选挑选是纯函数：只取够阈值的、按 top 截断；关开关/零阈值/空采样都返回空。"""
    rows = [("a.exe", 4 * 1024 ** 3, 1), ("b.exe", 2 * 1024 ** 3, 2),
            ("c.exe", 1024 * 1024 ** 2, 3), ("d.exe", 100 * 1024 ** 2, 4)]

    got = [n for n, _, _ in clean._target_clean_candidates(rows, normalize_config({}))]
    assert got == ["a.exe", "b.exe", "c.exe"]      # 默认 ≥1024MB、前 3 个
    top1 = normalize_config({"target_clean_top": 1})
    got = [n for n, _, _ in clean._target_clean_candidates(rows, top1)]
    assert got == ["a.exe"]

    assert clean._target_clean_candidates(rows, normalize_config({"target_clean": False})) == []
    assert clean._target_clean_candidates(rows, {"target_clean": True,
                                                 "target_clean_min_mb": 0}) == []
    assert clean._target_clean_candidates(rows, {"target_clean": True,
                                                 "target_clean_min_mb": 1024,
                                                 "target_clean_top": 0}) == []
    assert clean._target_clean_candidates([], normalize_config({})) == []


def test_run_target_clean_passes_only_candidate_pids(monkeypatch):
    """只把候选 PID 交给 empty_selected_working_sets，并在同一特权窗口内完成。"""
    seen = []

    @contextmanager
    def fake_privileges():
        seen.append("priv")
        yield []

    monkeypatch.setattr(clean, "clean_privileges", fake_privileges)
    monkeypatch.setattr(clean, "empty_selected_working_sets",
                        lambda pids, bl: seen.append((list(pids), bl)) or (2, 1))

    n, skipped = clean._run_target_clean([("a.exe", 1, 11), ("b.exe", 2, 22)], ["chrome"])

    assert (n, skipped) == (2, 1)
    assert seen == ["priv", ([11, 22], ["chrome"])]


# ------------------------------------------------ 只清指定进程的工作集（v1.7.0 动作层）

class _FakeKernel:
    """记录 OpenProcess/CloseHandle 的假 kernel32，绝不碰真实进程。"""

    def __init__(self):
        self.opened = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append(pid)
        return 1000 + pid

    def CloseHandle(self, h):
        self.closed.append(h)


def test_empty_selected_working_sets_only_touches_given_pids(monkeypatch):
    """只对传入 PID 开句柄、用完即关：不遍历整个进程表。"""
    k = _FakeKernel()
    monkeypatch.setattr(actions, "kernel32", k)
    monkeypatch.setattr(actions, "psapi", SimpleNamespace(EmptyWorkingSet=lambda h: True))
    monkeypatch.setattr(actions, "psutil",
                        SimpleNamespace(Process=lambda pid: _proc_named("p.exe")))

    assert actions.empty_selected_working_sets([11, 12], []) == (2, 0)
    assert k.opened == [11, 12]
    assert k.closed == [1011, 1012]


def test_empty_selected_working_sets_skips_blacklist_and_system_pids(monkeypatch):
    """白名单按进程名重新核对（PID 可能被复用），Idle/System 直接跳过。"""
    k = _FakeKernel()
    monkeypatch.setattr(actions, "kernel32", k)
    monkeypatch.setattr(actions, "psapi", SimpleNamespace(EmptyWorkingSet=lambda h: True))
    monkeypatch.setattr(actions, "psutil", SimpleNamespace(
        Process=lambda pid: _proc_named("csrss.exe" if pid == 13 else "p.exe")))

    assert actions.empty_selected_working_sets([0, 4, 13, 14], []) == (1, 3)
    assert k.opened == [14]


def test_empty_selected_working_sets_tolerates_single_failure(monkeypatch):
    """单个进程已退出或清空失败不影响其余，只回报成功的个数。"""
    k = _FakeKernel()
    monkeypatch.setattr(actions, "kernel32", k)
    monkeypatch.setattr(actions, "psapi", SimpleNamespace(
        EmptyWorkingSet=lambda h: h == 1000 + 14))

    def _proc(pid):
        if pid == 12:
            raise OSError("process gone")
        return _proc_named("p.exe")

    monkeypatch.setattr(actions, "psutil", SimpleNamespace(Process=_proc))

    assert actions.empty_selected_working_sets([11, 12, 14], []) == (1, 0)
    assert k.opened == [11, 14]


def _proc_named(name):
    """假 psutil.Process：name() 必须是方法（真实 psutil 如此），否则 .name() 会炸。"""
    return SimpleNamespace(name=lambda: name)

# ------------------------------------------------ 趋势预防式清理（v1.8.0）

def _hist(slope_pm, end=80.0, step=10.0, n=8):
    """造 (t, phys, commit) 采样：物理按每分钟 slope_pm 个百分点匀速上涨。

    提交一起涨但只有物理的一半，确保多数用例里 eta 更小的是物理口径。
    end 是最后一个采样点（即「当前」）的物理占用，趋势往前倒推。
    """
    per_step = slope_pm * step / 60.0
    t0 = 1_000_000.0
    rows = []
    for i in range(n):
        phys = end - (n - 1 - i) * per_step
        rows.append((t0 + i * step, phys, phys * 0.5 + 20.0))
    return rows


def test_predictive_due_returns_none_when_disabled():
    """开关关掉时再明显的趋势也不提前清理（纯函数，走默认配置）。"""
    cfg = normalize_config({"predict_clean": False, "predict_window_min": 5})
    assert clean.predictive_due(_hist(1.5), cfg) is None


def test_predictive_due_needs_enough_samples():
    """采样点不够（默认 6 个）时静止趋势判断：刚启动的噪声不作数。"""
    cfg = normalize_config({"predict_window_min": 5})
    assert clean.predictive_due(_hist(1.5, n=5), cfg) is None
    assert clean.predictive_due([], cfg) is None


def test_predictive_due_needs_minimum_time_span():
    """点够但总共只跨几秒：一分钟的抖动不叫趋势。"""
    cfg = normalize_config({"predict_window_min": 5})
    dense = [(1_000_000.0 + i, 70.0 + i * 0.3, 60.0) for i in range(8)]
    assert clean.predictive_due(dense, cfg) is None


def test_predictive_due_ignores_flat_or_falling():
    """平稳和下跌都不该触发预防式清理：那是超阈值清理才管的事。"""
    cfg = normalize_config({"predict_window_min": 5})
    assert clean.predictive_due(_hist(0.0), cfg) is None
    assert clean.predictive_due(_hist(-1.0), cfg) is None


def test_predictive_due_below_min_slope_is_ignored():
    """缓涨（0.5%/分钟）低于最小有效斜率：交给预警气泡，别提前动。"""
    cfg = normalize_config({"predict_window_min": 5})
    assert clean.predictive_due(_hist(0.5), cfg) is None


def test_predictive_due_hits_when_eta_within_window():
    """79% 起、1.5%/分钟、阈值 85%、窗口 5 分钟：预计 4 分钟触阈 → 命中物理口径。"""
    cfg = normalize_config({"predict_window_min": 5})
    got = clean.predictive_due(_hist(1.5, end=79.0), cfg)
    assert got is not None
    assert got["metric"] == "物理"
    assert abs(got["slope_pm"] - 1.5) < 0.15
    assert 200 <= got["eta"] <= 300


def test_predictive_due_respects_window():
    """同一斜率，窗口缩到 1 分钟：预计 4 分钟才触阈 → 不提前打扰。"""
    cfg = normalize_config({"predict_window_min": 1})
    assert clean.predictive_due(_hist(1.5, end=79.0), cfg) is None


def test_predictive_due_skips_already_over_threshold():
    """已经越线是超阈值清理的活，预防不抢（物理 90% ≥ 85%）。"""
    cfg = normalize_config({"predict_window_min": 5})
    assert clean.predictive_due(_hist(1.5, end=90.0), cfg) is None


def test_predictive_due_picks_smaller_eta_metric():
    """提交涨得更快、且先触阈时，按提交口径预警。"""
    cfg = normalize_config({"predict_window_min": 5})
    rows = [(1_000_000.0 + i * 10.0, 60.0 + i * 0.3, 80.0 + i * 0.5) for i in range(8)]
    got = clean.predictive_due(rows, cfg)
    assert got is not None
    assert got["metric"] == "提交"


def test_predictive_due_applies_headroom():
    """预防与清理共用同一个降压余量：5 点余量下 1 分钟窗口就命中，关掉就不命中。"""
    rows = _hist(1.5, end=79.0)
    hr = normalize_config({"predict_window_min": 1, "target_headroom_pct": 5})
    got = clean.predictive_due(rows, hr)
    assert got is not None and got["metric"] == "物理"
    # 同样的斜率、余量关掉：还要 4 分钟才触阈，1 分钟窗口不提前打扰
    assert clean.predictive_due(
        rows, normalize_config({"predict_window_min": 1})) is None


def test_slope_per_sec_fits_rising_line():
    """纯函数直接测：完美线性上升时斜率等于每秒涨幅，点不够/时间戳全同返回 0。"""
    rows = _hist(1.5, n=8, step=10.0)      # 0.25%/10s = 0.025%/s
    assert abs(clean._slope_per_sec(rows, 1) - 0.025) < 1e-9
    assert clean._slope_per_sec(rows[:1], 1) == 0.0
    same_t = [(1000.0, 1.0, 1.0), (1000.0, 2.0, 2.0)]
    assert clean._slope_per_sec(same_t, 1) == 0.0


def test_bump_stats_counts_preventive(monkeypatch, tmp_path):
    """preventive 只在传入 True 时累计；一次都没预防过的配置不落这个键。"""
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text(json.dumps({"stats": {"count": 1, "freed": 0}}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    assert clean._bump_stats(10, preventive=True)["preventive"] == 1
    assert clean._bump_stats(10)["preventive"] == 1, "没预防过不能凭空累计"


def test_do_clean_passes_preventive_to_stats(monkeypatch):
    """preventive=True 要一路透传到 _bump_stats：统计行和自调优建议都靠它。"""
    seen = []

    def _fake_bump(freed, escalated=False, targeted=False, preventive=False,
                   short_relief=False, bg_trimmed=False, sticky=False, deepened=False):
        seen.append(preventive)
        return {"count": 9, "freed": freed}

    _admin_env(monkeypatch)
    monkeypatch.setattr(clean, "_bump_stats", _fake_bump)
    r = clean.do_clean("预防式", preventive=True)

    assert r["ok"] is True
    assert seen == [True]


# ---------------------------------------------------------------- 清理效果闭环（v1.9.0）


def test_bump_stats_counts_short_relief(monkeypatch, tmp_path):
    """short_relief 只在传入 True 时累计；一次都没短效过的配置不落这个键。"""
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text(json.dumps({"stats": {"count": 1, "freed": 0}}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    assert clean._bump_stats(10, short_relief=True)["short_relief"] == 1
    assert clean._bump_stats(10)["short_relief"] == 1, "没短效过不能凭空累计"


def test_do_clean_passes_low_relief_to_stats(monkeypatch):
    """low_relief=True 要一路透传到 _bump_stats：统计行和自调优建议都靠它。"""
    seen = []

    def _fake_bump(freed, escalated=False, targeted=False, preventive=False,
                   short_relief=False, bg_trimmed=False, sticky=False, deepened=False):
        seen.append(short_relief)
        return {"count": 9, "freed": freed}

    _admin_env(monkeypatch)
    monkeypatch.setattr(clean, "_bump_stats", _fake_bump)
    r = clean.do_clean("短效", low_relief=True)

    assert r["ok"] is True
    assert seen == [True]

# ------------------------------------ 后台进程工作集清理 / 泄漏识别（v1.11.0）


def test_do_clean_bg_trim_runs_between_targeted_and_escalate(monkeypatch):
    """定向大户仍不达标：清后台进程工作集，顺序在 targeted 之后、升档之前。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},  # 保守后仍受压
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},  # 定向后仍受压
        {"avail_phys": base + 3072, "phys_pct": 92.0, "commit_pct": 40.0},  # 后台清理后仍受压
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},  # 升档后回落
    ]
    calls = []
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_big_rows(),
               bg_trim=True, bg_result=(2, 0))
    r = clean.do_clean("测试")

    assert calls.index("ebw") > calls.index("esw"), "后台清理排在定向大户之后"
    assert calls.index("ebw") < calls.index("epw"), "后台清理排在升档之前"
    assert calls.count("ebw") == 1
    assert calls.count("wait") == 4
    assert r["bg_trim"] == 2
    assert "后台进程工作集:2 个" in r["detail"]


def test_do_clean_bg_trim_skipped_when_disabled(monkeypatch):
    """bg_trim 关掉：定向之后直接考虑升档，不碰后台进程。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    calls = []
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_big_rows(),
               bg_trim=False)
    r = clean.do_clean("测试")

    assert "ebw" not in calls
    assert r["bg_trim"] == 0
    assert "后台进程工作集" not in r["detail"]


def test_do_clean_bg_trim_excludes_targeted_pids(monkeypatch):
    """后台级去重（v1.14.0）：定向已经逐个清过的进程，后台这一级不再重复碰。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 3072, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    exclude_seen = []

    def _ebw(exclude, bl):
        exclude_seen.append(set(exclude))
        return (2, 0)

    _admin_env(monkeypatch, wait_after=wait_after, top_rows=_big_rows(),
               bg_trim=True, cfg_over={"stage_deepen": False})
    monkeypatch.setattr(clean, "empty_background_working_sets", _ebw)
    r = clean.do_clean("测试")

    # 窗口枚举已有 4242/99，定向清过的是 big(4242)/mid(88)：三者都不该再被后台碰
    assert {n for n, _, _ in r["targeted"]} == {"big.exe", "mid.exe"}
    assert r["bg_trim"] == 2
    assert exclude_seen == [{4242, 99, 88}]


def test_do_clean_bg_trim_skips_when_window_enum_untrustworthy(monkeypatch):
    """枚举不到窗口时宁可不做：绝不能把「枚举失败」当成「都没有窗口」全清。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    calls = []
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_big_rows(),
               bg_trim=True)
    monkeypatch.setattr(clean, "visible_window_pids", lambda: None)
    r = clean.do_clean("测试")

    assert "ebw" not in calls
    assert "后台进程工作集:跳过(无法枚举窗口)" in r["detail"]
    assert r["bg_trim"] == 0


def test_do_clean_bg_trim_skipped_in_aggressive(monkeypatch):
    """激进档本来就全清进程工作集：后台清理这一级没有存在意义。"""
    calls = []
    _admin_env(monkeypatch, level="aggressive", calls=calls, top_rows=_big_rows(),
               bg_trim=True)
    clean.do_clean("测试")

    assert "ebw" not in calls


def test_do_clean_bg_trim_skipped_when_first_round_relieves(monkeypatch):
    """保守清理一轮就到位：后续阶梯一级都不该触发。"""
    base = 8 * 1024 ** 3
    wait_after = [{"avail_phys": base + 2048, "phys_pct": 60.0, "commit_pct": 40.0}]
    calls = []
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls, top_rows=_big_rows(),
               bg_trim=True)
    r = clean.do_clean("测试")

    assert "ebw" not in calls
    assert r["bg_trim"] == 0


def test_do_clean_passes_bg_trimmed_to_stats(monkeypatch):
    """bg_trimmed=True 要一路透传到 _bump_stats：统计行与通知都靠它。"""
    seen = []
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 3072, "phys_pct": 60.0, "commit_pct": 40.0},
    ]

    def _fake_bump(freed, escalated=False, targeted=False, preventive=False,
                   short_relief=False, bg_trimmed=False, sticky=False, deepened=False):
        seen.append(bg_trimmed)
        return {"count": 9, "freed": freed}

    _admin_env(monkeypatch, wait_after=wait_after, top_rows=_big_rows(), bg_trim=True)
    monkeypatch.setattr(clean, "_bump_stats", _fake_bump)
    r = clean.do_clean("测试")

    assert r["bg_trim"] == 2
    assert seen == [True]


def test_do_clean_targets_leaks_even_below_min_mb(monkeypatch):
    """泄漏进程不受大户下限约束：还没长成就得优先下刀（定向候选第一顺位）。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},
    ]
    esw_pids = []
    _admin_env(monkeypatch, wait_after=wait_after, calls=[])
    monkeypatch.setattr(clean, "empty_selected_working_sets",
                        lambda pids, bl: (esw_pids.extend(pids), (1, 0))[1])
    # Top 采样默认全是 100MB 小进程，够不到 1024MB 大户线
    r = clean.do_clean("测试", growth_rows=[("leaky.exe", 200 * 1024 ** 2, 7)])

    assert esw_pids == [7]
    assert [n for n, _, _ in r["targeted"]] == ["leaky.exe"]
    assert "定向清理大户(leaky.exe" in r["detail"]


def _growth_history(n=6, step=100.0, t0=1000.0):
    """假造 Guard.proc_history：leaky 每次采样涨 128MB，creeper 涨 8MB，other 不动。"""
    hist = []
    for i in range(n):
        hist.append((t0 + i * step, {
            7: ("leaky.exe", (900 + 128 * i) * 1024 ** 2),
            8: ("other.exe", 200 * 1024 ** 2),
            9: ("creeper.exe", (300 + 8 * i) * 1024 ** 2),
        }))
    return hist


def test_growth_slopes_fits_only_rising_processes():
    """斜率拟合只认持续上涨的进程；平稳进程直接剔除，结果按斜率降序。"""
    rows = clean.growth_slopes(_growth_history())
    by_name = {name: (rss, slope, growth) for _, name, rss, slope, growth in rows}
    assert "other.exe" not in by_name, "平稳进程没有上升斜率"
    assert "leaky.exe" in by_name and "creeper.exe" in by_name
    _, _, growth = by_name["leaky.exe"]
    assert growth == 5 * 128 * 1024 ** 2, "净增长取末次减首次"
    assert rows[0][1] == "leaky.exe", "斜率最大的排最前"
    slopes = [r[3] for r in rows]
    assert slopes == sorted(slopes, reverse=True)


def test_growth_slopes_needs_enough_samples_and_span():
    """采样点数不够或时间跨度太短：一律不拟合（抖动不是趋势）。"""
    assert clean.growth_slopes(_growth_history(n=4)) == []
    assert clean.growth_slopes(_growth_history(step=10.0)) == []


def test_growth_slopes_ignores_processes_not_present_throughout():
    """中途启动或退出的进程不是趋势，是噪声：不进入拟合。"""
    hist = _growth_history()
    snap = dict(hist[-1][1])
    del snap[8]
    hist[-1] = (hist[-1][0], snap)
    names = {name for _, name, _, _, _ in clean.growth_slopes(hist)}
    assert "other.exe" not in names
    assert {"leaky.exe", "creeper.exe"} <= names


def test_leak_candidates_requires_slope_and_growth():
    """斜率与净增长双越线才算疑似泄漏：只沾一条边的（creeper）不算。"""
    got = clean.leak_candidates(_growth_history())
    assert got == [("leaky.exe", (900 + 5 * 128) * 1024 ** 2, 7)]


def test_leak_candidates_truncates_and_orders_by_slope():
    """最多报 _LEAK_MAX_ROWS 个、按斜率降序：别把通知/建议列表刷满。"""
    hist = []
    for i in range(6):
        hist.append((1000.0 + i * 100.0, {
           100 + k: ("p%d.exe" % k, (500 + (16 + 4 * k) * i) * 1024 ** 2)
            for k in range(7)
        }))
    got = clean.leak_candidates(hist)
    slopes = {name: slope for _, name, _, slope, _ in clean.growth_slopes(hist)}
    names = [n for n, _, _ in got]
    assert len(got) == clean._LEAK_MAX_ROWS == 5
    assert names == sorted(names, key=lambda n: slopes[n], reverse=True)
    assert names[0] == "p6.exe", "斜率最大的排最前"


def test_target_clean_candidates_prioritizes_leaks_and_dedupes():
    """泄漏进程排最前、不受大户下限约束，并按 PID 去重。"""
    rows = [("big.exe", 4 * 1024 ** 3, 1), ("mid.exe", 2 * 1024 ** 3, 2)]
    extras = [("leaky.exe", 300 * 1024 ** 2, 3), ("big.exe", 4 * 1024 ** 3, 1)]
    got = [n for n, _, _ in clean._target_clean_candidates(rows, normalize_config({}), extras)]
    assert got == ["leaky.exe", "big.exe", "mid.exe"]


def test_target_clean_candidates_extras_respect_top_truncation():
    """extras 也受 top 截断：候选列表不会被泄漏进程撑爆。"""
    got = clean._target_clean_candidates(
        [("big.exe", 4 * 1024 ** 3, 1)], {"target_clean": True, "target_clean_top": 1},
        [("leaky.exe", 300 * 1024 ** 2, 3)])
    assert [n for n, _, _ in got] == ["leaky.exe"]


def test_target_clean_candidates_extras_only_when_min_mb_zero():
    """大户下限关成 0：只返回 extras，不再从 Top 采样里挑。"""
    got = clean._target_clean_candidates(
        [("big.exe", 4 * 1024 ** 3, 1)], {"target_clean": True, "target_clean_min_mb": 0,
                                        "target_clean_top": 2},
        [("leaky.exe", 10 * 1024 ** 2, 3)])
    assert [n for n, _, _ in got] == ["leaky.exe"]


def test_target_clean_candidates_widen_doubles_top_and_halves_min():
    """widen=True（加深）：候选数翻倍、大户下限减半且有 1MB 地板；零下限只翻 extras。"""
    rows = _wide_rows()
    plain = [n for n, _, _ in
             clean._target_clean_candidates(rows, normalize_config({}))]
    assert plain == ["a.exe", "b.exe", "c.exe"]          # 默认 >=1024MB、前 3 个
    wide = [n for n, _, _ in
            clean._target_clean_candidates(rows, normalize_config({}), widen=True)]
    assert wide == ["a.exe", "b.exe", "c.exe", "d.exe", "e.exe", "f.exe"]

    # 下限减半但不低于 1MB 地板：700KB 的中等进程不会被加深纳进来
    tiny = {"target_clean": True, "target_clean_min_mb": 1, "target_clean_top": 3}
    got = [n for n, _, _ in clean._target_clean_candidates(
        [("a.exe", 4 * 1024 ** 3, 1), ("b.exe", 700 * 1024, 2),
         ("c.exe", 300 * 1024, 3)], tiny, widen=True)]
    assert got == ["a.exe"]

    # 下限为 0：widen 只把 extras 的截断翻倍，绝不清扫整个进程表
    zero = {"target_clean": True, "target_clean_min_mb": 0, "target_clean_top": 1}
    extras = [("leak1.exe", 10 * 1024 ** 2, 51), ("leak2.exe", 5 * 1024 ** 2, 52)]
    got = [n for n, _, _ in clean._target_clean_candidates(
        rows, zero, extras, widen=True)]
    assert got == ["leak1.exe", "leak2.exe"]              # top 由 1 翻倍到 2

    assert clean._target_clean_candidates(
        rows, normalize_config({"target_clean": False}), widen=True) == []

    # v1.14.0 逐轮放宽：轮数是独立的乘幂——候选数每轮再翻倍、下限每轮再减半
    wide2 = [n for n, _, _ in clean._target_clean_candidates(
        rows, normalize_config({}), widen=True, round_=2)]
    assert wide2 == ["a.exe", "b.exe", "c.exe", "d.exe", "e.exe", "f.exe", "g.exe"]
    wide3 = [n for n, _, _ in clean._target_clean_candidates(
        rows, normalize_config({}), widen=True, round_=3)]
    assert wide3[-1] == "h.exe", "第 3 轮下限降到 128MB，200MB 的 h 才进得来"
    # round_=0 / None 这类脏值按第 1 轮算：别把候选数乘成 0 或负数
    for bad in (0, None):
        assert [n for n, _, _ in clean._target_clean_candidates(
            rows, normalize_config({}), widen=True, round_=bad)] == wide


# ------------------------------------ 加深轮数上限 + 降压余量（v1.14.0 纯函数）


def test_deepen_rounds_clamps_to_one_and_three():
    """轮数上限 clamp 1..3：默认 1 即 v1.13.0 的单轮加深，脏值也回 1。"""
    assert clean._deepen_rounds(normalize_config({})) == 1
    assert clean._deepen_rounds(normalize_config({"stage_deepen_rounds": 3})) == 3
    assert clean._deepen_rounds(normalize_config({"stage_deepen_rounds": 9})) == 3
    assert clean._deepen_rounds(normalize_config({"stage_deepen_rounds": 0})) == 1
    assert clean._deepen_rounds({"stage_deepen_rounds": "x"}) == 1
    assert clean._deepen_rounds({}) == 1


def test_headroom_pct_clamps_and_ignores_dirty_values():
    """降压余量 clamp 0..20；脏值/缺省一律 0（关），别因配置抽风变成负余量。"""
    assert clean._headroom_pct(normalize_config({})) == 0
    assert clean._headroom_pct(normalize_config({"target_headroom_pct": 5})) == 5
    assert clean._headroom_pct(normalize_config({"target_headroom_pct": 99})) == 20
    assert clean._headroom_pct(normalize_config({"target_headroom_pct": -3})) == 0
    assert clean._headroom_pct({"target_headroom_pct": "x"}) == 0
    assert clean._headroom_pct({}) == 0


def test_effective_threshold_subtracts_headroom():
    """两条百分比阈值让出余量；None 与非数字原样返回，调用方照旧跳过对应判断。"""
    cfg = normalize_config({"phys_threshold": 85, "commit_threshold": 90,
                            "target_headroom_pct": 5})
    assert clean._effective_threshold(cfg, "phys_threshold") == 80
    assert clean._effective_threshold(cfg, "commit_threshold") == 85
    # 余量 0 时与阈值相同：默认配置下行为与旧版完全一致
    assert clean._effective_threshold(normalize_config({}), "phys_threshold") == 85
    assert clean._effective_threshold({"phys_threshold": None,
                                       "target_headroom_pct": 5},
                                      "phys_threshold") is None
    assert clean._effective_threshold({"phys_threshold": "85",
                                       "target_headroom_pct": 5},
                                      "phys_threshold") == "85"


def test_still_pressured_applies_headroom():
    """82% 在默认档不算受压；给出 5 点余量后阈值降到 80，就算还没按住。"""
    m = {"phys_pct": 82.0}
    assert clean._still_pressured(m, normalize_config({})) is False
    assert clean._still_pressured(m, normalize_config({"target_headroom_pct": 5})) is True
    # 余量只让百分比线：min_avail_mb 是绝对量，照旧独立兜底
    low = {"phys_pct": 10.0, "avail_phys": 64 * 1024 ** 2}
    assert clean._still_pressured(low, normalize_config({"min_avail_mb": 128})) is True


# ---------------------------------------- 持续压力粘滞激进 + 阶段跳过（v1.12.0）

def test_do_clean_sticky_goes_aggressive_and_skips_ladder(monkeypatch):
    """粘滞轮不再从保守档重跑：直接激进，整条阶梯一级都不跑。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0}
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls,
               top_rows=_big_rows(), bg_trim=True)
    r = clean.do_clean("测试", sticky=True)

    assert r["sticky"] is True
    assert r["level"] == "aggressive"
    assert "粘滞激进(持续高压)" in r["detail"]
    assert calls.count("epw") == 1        # 只激进第一遍清进程工作集
    assert calls.count("esw") == 0        # 阶梯的定向级整级跳过
    assert calls.count("ebw") == 0        # 阶梯的后台级整级跳过
    assert calls.count("wait") == 1       # 不补第二遍、也不逐级再等
    assert r["targeted"] == [] and r["bg_trim"] == 0
    assert r["escalated"] is False and r["escalated_freed"] == 0

def test_do_clean_sticky_reports_still_pressured(monkeypatch):
    """粘滞轮清理完仍越线：still 回 True，托盘据此保持粘滞态。"""
    base = 8 * 1024 ** 3
    wait_after = {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0}
    _admin_env(monkeypatch, wait_after=wait_after)
    r = clean.do_clean("测试", sticky=True)

    assert r["still"] is True

def test_do_clean_sticky_reports_relieved(monkeypatch):
    """粘滞轮清理完已回落：still 回 False，托盘据此退出粘滞、回到保守档起重。"""
    base = 8 * 1024 ** 3
    wait_after = {"avail_phys": base + 2048, "phys_pct": 60.0, "commit_pct": 40.0}
    _admin_env(monkeypatch, wait_after=wait_after)
    r = clean.do_clean("测试", sticky=True)

    assert r["still"] is False

def test_do_clean_sticky_feeds_stats_counter(monkeypatch):
    """粘滞执行时 _bump_stats 收到 sticky=True：统计行「（粘滞 N 次）」靠它。"""
    base = 8 * 1024 ** 3
    seen = []

    def _fake_bump(freed, escalated=False, targeted=False, preventive=False,
                   short_relief=False, bg_trimmed=False, sticky=False, deepened=False):
        seen.append(sticky)
        return {"count": 9, "freed": freed}

    _admin_env(monkeypatch, wait_after={"avail_phys": base + 1024,
                                             "phys_pct": 60.0, "commit_pct": 40.0})
    monkeypatch.setattr(clean, "_bump_stats", _fake_bump)
    clean.do_clean("测试", sticky=True)

    assert seen == [True]

def test_do_clean_skip_targeted_stage_still_trims_background(monkeypatch):
    """阶段自学习跳过了定向级：后台清理照跑，升档兜底也不受影响。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 仍受压
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},   # 后台后仍受压
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},   # 升档后回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls,
               top_rows=_big_rows(), bg_trim=True, bg_result=(2, 0))
    r = clean.do_clean("测试", skip={"targeted"})

    assert r["targeted"] == [] and calls.count("esw") == 0
    assert r["bg_trim"] == 2 and r["bg_freed"] == 1024
    assert r["targeted_freed"] == 0
    assert calls.count("ebw") == 1 and calls.count("epw") == 1
    assert r["escalated"] is True

def test_do_clean_skip_bg_stage_still_escalates(monkeypatch):
    """阶段自学习跳过了后台级：定向大户照跑，仍不达标才升档。"""
    base = 8 * 1024 ** 3
    calls = []
    wait_after = [
        {"avail_phys": base + 1024, "phys_pct": 92.0, "commit_pct": 40.0},   # 仍受压
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},   # 定向后仍受压
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},   # 升档后回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, calls=calls,
               top_rows=_big_rows(), bg_trim=True)
    r = clean.do_clean("测试", skip={"bg"})

    assert "ebw" not in calls and r["bg_trim"] == 0 and r["bg_freed"] == 0
    assert [n for n, _, _ in r["targeted"]] == ["big.exe", "mid.exe"]
    assert calls.count("esw") == 1 and calls.count("epw") == 1
    assert r["escalated"] is True

def test_do_clean_reports_per_stage_release(monkeypatch):
    """分阶段释放量按各级 before→after 拆开算：合计即「保守第一遍之后」的增量。"""
    base = 8 * 1024 ** 3
    wait_after = [
        {"avail_phys": base + 2048, "phys_pct": 92.0, "commit_pct": 40.0},   # 仍受压
        {"avail_phys": base + 4096, "phys_pct": 60.0, "commit_pct": 40.0},   # 定向后回落
    ]
    _admin_env(monkeypatch, wait_after=wait_after, top_rows=_big_rows())
    r = clean.do_clean("测试")

    assert r["targeted_freed"] == 2048
    assert r["bg_freed"] == 0 and r["escalated_freed"] == 0
    assert r["freed"] == 4096
    # 阶梯各级加起来就是「保守第一遍之后」又释放的部分，另 2048 来自第一遍本身
    assert r["targeted_freed"] + r["bg_freed"] + r["escalated_freed"] == 2048
