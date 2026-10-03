# -*- coding: utf-8 -*-
"""
清理动作：把底层 Win32 / Native API 调用封装为单个、可组合的动作。

每个动作返回原始 NTSTATUS（int，0 表示成功）或布尔值，便于上层编排与统计；
本模块不含编排逻辑（编排与结果统计见 clean.py）。

注意：Memory* 系列命令常量定义在 winapi.py，必须显式导入——
遗漏导入只会在真正执行清理（管理员路径）时抛 NameError，非管理员路径不会触发。
"""
from __future__ import annotations

import os

import psutil

from .config import CLEAN_BLACKLIST_STEMS, _blacklist_stems, _norm_proc_name
from .winapi import (
    MemoryEmptyWorkingSets,
    MemoryFlushModifiedList,
    MemoryPurgeLowPriorityStandbyList,
    MemoryPurgeStandbyList,
    clear_file_cache,
    kernel32,
    psapi,
    _purge_list,
)


def purge_working_sets() -> int:
    """清空系统工作集（MemoryEmptyWorkingSets），返回 NTSTATUS。"""
    return _purge_list(MemoryEmptyWorkingSets)


def flush_modified_list() -> int:
    """刷写修改页列表（MemoryFlushModifiedList），返回 NTSTATUS。"""
    return _purge_list(MemoryFlushModifiedList)


def purge_standby_list() -> int:
    """清理 standby list（MemoryPurgeStandbyList），返回 NTSTATUS。"""
    return _purge_list(MemoryPurgeStandbyList)


def purge_low_priority_standby() -> int:
    """只清理 standby list 中的低优先级部分（MemoryPurgeLowPriorityStandbyList）。

    对标 ISLC/WinMemoryCleaner 的「低优先级 standby」清理：影响面比清全部 standby 更小，
    适合作为更温和的第一步。系统不支持该命令时会返回非 0，由上层如实展示。
    """
    return _purge_list(MemoryPurgeLowPriorityStandbyList)


def clear_system_file_cache() -> bool:
    """清空系统文件缓存工作集，返回是否成功。"""
    return clear_file_cache()


def empty_process_working_sets(extra_blacklist=()) -> tuple:
    """逐个进程清空工作集（把不活跃的物理页移到 standby list）。

    extra_blacklist 为用户附加白名单（进程名，可带可不带 .exe），命中者跳过，
    避免清空浏览器/IDE 等工作集造成前台卡顿。返回 (已清理数, 白名单跳过数)。
    """
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    # psapi.EmptyWorkingSet / kernel32.OpenProcess 的类型声明见 winapi.py

    blocked = CLEAN_BLACKLIST_STEMS | _blacklist_stems(extra_blacklist)
    count = 0
    skipped = 0
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if _norm_proc_name(p.info.get("name")) in blocked or p.info["pid"] in (0, 4):
                skipped += 1
                continue
            h = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, p.info["pid"]
            )
            if h:
                try:
                    if psapi.EmptyWorkingSet(h):
                        count += 1
                finally:
                    kernel32.CloseHandle(h)
        except Exception:
            continue
    return count, skipped


def empty_background_working_sets(exclude_pids=(), extra_blacklist=()) -> tuple:
    """清空「没有可见顶层窗口」的后台进程工作集（定向与升档之间的第三级阶梯）。

    与 empty_process_working_sets 的差别是排除用户正在看的进程：exclude_pids 由
    调用方用 winapi.visible_window_pids() 现算，再叠加自身与黑名单，只对真正后台
    的进程下手——前台零感知，覆盖面又比定向大户全。返回 (已清理数, 跳过数)。
    """
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    # psapi.EmptyWorkingSet / kernel32.OpenProcess 的类型声明见 winapi.py

    blocked = CLEAN_BLACKLIST_STEMS | _blacklist_stems(extra_blacklist)
    exclude = {os.getpid()} | {int(pid) for pid in exclude_pids}
    count = 0
    skipped = 0
    for p in psutil.process_iter(["pid", "name"]):
        try:
            pid = p.info["pid"]
            if pid in (0, 4) or pid in exclude or _norm_proc_name(p.info.get("name")) in blocked:
                skipped += 1
                continue
            h = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid
            )
            if h:
                try:
                    if psapi.EmptyWorkingSet(h):
                        count += 1
                finally:
                    kernel32.CloseHandle(h)
        except Exception:
            continue
    return count, skipped


def empty_selected_working_sets(pids, extra_blacklist=()) -> tuple:
    """只清空指定 PID 列表对应进程的工作集（定向清理内存大户，波及面最小）。

    与 empty_process_working_sets 的「全量逐进程扫」不同，这里只对传入 PID 精确
    生效：调用方（do_clean 的第二级阶梯）已按工作集挑过大户，这里不再遍历整个
    进程表。每个 PID 仍会重新核对进程名是否命中白名单（采样后进程可能已退出、
    PID 可能被复用），跳过 Idle/System 与单个失败，返回 (已清理数, 跳过数)。
    """
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    # psapi.EmptyWorkingSet / kernel32.OpenProcess 的类型声明见 winapi.py

    blocked = CLEAN_BLACKLIST_STEMS | _blacklist_stems(extra_blacklist)
    count = 0
    skipped = 0
    for pid in pids:
        try:
            pid = int(pid)
            if pid in (0, 4):
                skipped += 1
                continue
            # 重新取进程名：上一步采样到现在进程可能已退出、PID 可能被复用
            if _norm_proc_name(psutil.Process(pid).name()) in blocked:
                skipped += 1
                continue
            h = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid
            )
            if h:
                try:
                    if psapi.EmptyWorkingSet(h):
                        count += 1
                finally:
                    kernel32.CloseHandle(h)
        except Exception:
            continue
    return count, skipped
