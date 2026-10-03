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
               calls=None, min_avail=None):
    """把 do_clean 的外部依赖全部换成假的，只留编排逻辑本身。

    escalate=None 沿用 normalize_config 的默认（True）；wait_after 支持单个 dict
    （每次 _wait_avail_rise 都返回它）或按调用次序取样的 list——后者用来覆盖
    「再等一次」的升档路径。calls 若为 list，则把每次动作/等待调用记进去，便于
    断言升档确实触发了完整的第二遍。base_avail/base_commit 控制 before 采样。
    min_avail 设置「低内存触发」下限（MB），用于下限兜底的升档路径。
    """
    monkeypatch.setattr(clean, "is_admin", lambda: True)
    cfg = normalize_config({"clean_level": level})
    if areas is not None:
        cfg["clean_areas"] = dict(areas)
    if escalate is not None:
        cfg["escalate_clean"] = bool(escalate)
    if min_avail is not None:
        cfg["min_avail_mb"] = int(min_avail)
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
                        lambda freed, escalated=False: {"count": 9, "freed": freed})
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
        lambda freed, escalated=False: seen.append(escalated) or {"count": 9, "freed": freed})
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
        lambda freed, escalated=False: seen.append(escalated) or {"count": 9, "freed": freed})
    r = clean.do_clean("测试")

    assert r["escalated"] is False
    assert seen == [False]
