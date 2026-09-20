# -*- coding: utf-8 -*-
"""winapi：内存读取结构、权限查询缓存、单实例锁、进程快照快路径。"""
import os

import psutil

from memguard import winapi
from memguard.winapi import acquire_single_instance, get_mem, is_admin


def test_get_mem_shape():
    s = get_mem()
    for key in ("total_phys", "avail_phys", "used_phys", "phys_pct",
                "total_commit", "avail_commit", "used_commit", "commit_pct"):
        assert key in s
    assert s["total_phys"] > 0
    assert 0 <= s["phys_pct"] <= 100
    assert 0 <= s["commit_pct"] <= 100


def test_is_admin_is_bool_and_stable():
    assert isinstance(is_admin(), bool)
    assert is_admin() == is_admin()   # 进程内缓存，结果稳定


def test_acquire_single_instance_returns_bool():
    assert isinstance(acquire_single_instance(), bool)


def test_process_working_sets_reports_self():
    """快路径必须能读到自己：结构体偏移错位时这里最先失败（读不到或数值离谱）。"""
    rows = {(pid): (name, rss) for name, rss, pid in winapi.process_working_sets()}
    assert os.getpid() in rows, "快照应包含当前进程"
    name, rss = rows[os.getpid()]
    assert name.lower().endswith(".exe")
    own = psutil.Process(os.getpid()).memory_info().rss
    assert abs(rss - own) < max(own * 0.2, 8 * 1024 * 1024), f"{rss} vs {own}"


def test_process_working_sets_reports_many_with_a_working_set():
    rows = winapi.process_working_sets()
    assert len(rows) > 10
    assert sum(1 for _, rss, _ in rows if rss > 0) > 10
