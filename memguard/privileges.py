# -*- coding: utf-8 -*-
"""
特权管理：清理所需高危特权的启用与「用完即恢复」。

用上下文管理器保证：无论清理成功、失败还是抛异常，都会把「清理前处于禁用状态」的特权
恢复为禁用，避免 SeDebugPrivilege 等高危特权常驻。
"""
from __future__ import annotations

from contextlib import contextmanager

from .winapi import disable_privilege, enable_privilege, privilege_state

# 清理需要的高危特权（用完后会恢复其原始状态，避免常驻）
CLEAN_PRIVILEGES = (
    "SeDebugPrivilege",
    "SeProfileSingleProcessPrivilege",
    "SeIncreaseQuotaPrivilege",
    "SeIncreaseBasePriorityPrivilege",
)


@contextmanager
def clean_privileges():
    """启用清理所需特权，退出时恢复「原本未启用」的特权。

    yield 出「启用失败」的特权名列表，供调用方给出可读原因，
    避免只看到黑盒的 0xC0000061。
    """
    prior = {p: privilege_state(p) for p in CLEAN_PRIVILEGES}
    failed = [p for p in CLEAN_PRIVILEGES if not enable_privilege(p)]
    try:
        yield failed
    finally:
        for p, st in prior.items():
            if st is not None and not (st & 0x2):
                disable_privilege(p)
