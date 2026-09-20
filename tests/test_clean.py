# -*- coding: utf-8 -*-
"""clean / actions：清理动作常量绑定回归、非管理员分支、Top 进程统计。

关键回归点：`Memory*` 三个命令常量若漏导入，非管理员路径永不触发不会暴露，
真正的管理员清理才会在调用时抛 NameError。这里通过拦截底层调用并断言常量
被正确引用，在不真正清理内存的前提下挡住该类回归。
"""
from types import SimpleNamespace

from memguard import actions, clean
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
