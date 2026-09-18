# -*- coding: utf-8 -*-
"""winapi：内存读取结构、权限查询缓存、单实例锁。"""
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
