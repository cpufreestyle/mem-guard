# -*- coding: utf-8 -*-
"""clean / actions：清理动作常量绑定回归、非管理员分支、Top 进程统计。

关键回归点：`Memory*` 三个命令常量若漏导入，非管理员路径永不触发不会暴露，
真正的管理员清理才会在调用时抛 NameError。这里通过拦截底层调用并断言常量
被正确引用，在不真正清理内存的前提下挡住该类回归。
"""
from memguard import actions, clean
from memguard.winapi import (
    MemoryEmptyWorkingSets,
    MemoryFlushModifiedList,
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
