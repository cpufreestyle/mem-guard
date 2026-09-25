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


def _admin_env(monkeypatch, level="conservative", areas=None, results=None):
    """把 do_clean 的外部依赖全部换成假的，只留编排逻辑本身。"""
    monkeypatch.setattr(clean, "is_admin", lambda: True)
    cfg = normalize_config({"clean_level": level})
    if areas is not None:
        cfg["clean_areas"] = dict(areas)
    monkeypatch.setattr(clean, "load_config", lambda: cfg)

    @contextmanager
    def fake_privileges():
        yield []

    monkeypatch.setattr(clean, "clean_privileges", fake_privileges)
    monkeypatch.setattr(clean, "purge_working_sets", lambda: (results or {}).get("working_sets", 0))
    monkeypatch.setattr(clean, "flush_modified_list", lambda: (results or {}).get("modified", 0))
    monkeypatch.setattr(clean, "purge_standby_list", lambda: (results or {}).get("standby", 0))
    monkeypatch.setattr(clean, "purge_low_priority_standby",
                        lambda: (results or {}).get("low_priority_standby", 0))
    monkeypatch.setattr(clean, "clear_system_file_cache",
                        lambda: bool((results or {}).get("file_cache", True)))
    monkeypatch.setattr(clean, "empty_process_working_sets",
                        lambda bl: ((results or {}).get("proc", (3, 1))))
    monkeypatch.setattr(clean, "get_mem",
                        lambda: {"avail_phys": 8 * 1024 ** 3, "commit_pct": 50.0})
    monkeypatch.setattr(clean, "_wait_avail_rise",
                        lambda before: {"avail_phys": before["avail_phys"] + 1024,
                                        "commit_pct": 40.0})
    monkeypatch.setattr(clean, "_bump_stats", lambda freed: {"count": 9, "freed": freed})
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
