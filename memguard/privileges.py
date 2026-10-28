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

# 结束进程只需要这一条（v1.29.0）：管理员令牌下它能打开 SYSTEM / 其他账户进程
# 的终止句柄，和任务管理器同一个口径。
KILL_PRIVILEGE = "SeDebugPrivilege"


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


@contextmanager
def kill_privilege():
    """结束进程时临时启用 SeDebugPrivilege，退出时恢复原状态。

    与 clean_privileges 的差别只在范围：只碰这一条特权，不把另外三条清理特权
    带进「结束进程」这条用户随时会点的路径。非管理员令牌下启用必然失败，此时
    静默降级——调用方仍会拿到 OpenProcess 的 ERROR_ACCESS_DENIED，并在话术里
    告诉用户需要管理员身份。
    """
    prior = privilege_state(KILL_PRIVILEGE)
    enabled = enable_privilege(KILL_PRIVILEGE)
    try:
        yield enabled
    finally:
        if enabled and prior is not None and not (prior & 0x2):
            disable_privilege(KILL_PRIVILEGE)
