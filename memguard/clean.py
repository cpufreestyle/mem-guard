# -*- coding: utf-8 -*-
"""
清理逻辑：清理编排、进程工作集清空、Top 进程、配置相关统计。

依赖：
    - config：默认配置、黑名单、日志、格式化
    - winapi：内存读取、特权、底层清理调用
"""
from __future__ import annotations

import time

import psutil

from .config import (
    CLEAN_BLACKLIST_STEMS,
    CLEAN_LEVELS,
    _blacklist_stems,
    _norm_proc_name,
    gb,
    load_config,
    log,
)
from .winapi import (
    clear_file_cache,
    enable_privilege,
    get_mem,
    is_admin,
    kernel32,
    privilege_state,
    psapi,
    _purge_list,
)

# 清理需要的高危特权（用完后会恢复其原始状态，避免常驻）
CLEAN_PRIVILEGES = (
    "SeDebugPrivilege",
    "SeProfileSingleProcessPrivilege",
    "SeIncreaseQuotaPrivilege",
    "SeIncreaseBasePriorityPrivilege",
)


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


def do_clean(reason: str = "手动", level: str | None = None, user_blacklist=None) -> dict:
    """执行一次清理，返回结果统计。清理后会恢复特权开关（敏感特权用完即关）。

    level: "conservative"（默认，只清 standby/修改页/文件缓存）或
    "aggressive"（在此基础上额外清空各进程工作集）；None 时读取配置里的 clean_level。
    """
    if not is_admin():
        return {"ok": False, "msg": "需要管理员权限才能清理内存"}

    cfg = load_config()
    lvl = str(level or cfg.get("clean_level") or "conservative").strip().lower()
    if lvl not in CLEAN_LEVELS:
        lvl = "conservative"
    aggressive = lvl == "aggressive"
    bl = cfg.get("user_blacklist") if user_blacklist is None else user_blacklist

    # 记录特权初始状态；清理结束后把"原本未启用"的特权恢复为禁用
    prior = {p: privilege_state(p) for p in CLEAN_PRIVILEGES}
    # 显式检查特权启用结果：失败时给出可读原因，避免只看到黑盒 0xC0000061
    failed_privs = [p for p in CLEAN_PRIVILEGES if not enable_privilege(p)]

    try:
        before = get_mem()
        detail = [f"档位:{'激进' if aggressive else '保守'}"]
        if failed_privs:
            detail.append("特权启用失败:" + ",".join(failed_privs))

        r_ws = 0
        if aggressive:
            r_ws = _purge_list(MemoryEmptyWorkingSets)
            detail.append(f"清空工作集:{'成功' if r_ws == 0 else f'失败(0x{r_ws & 0xffffffff:X})'}")

        r_fm = _purge_list(MemoryFlushModifiedList)
        detail.append(f"刷写修改页:{'成功' if r_fm == 0 else f'失败(0x{r_fm & 0xffffffff:X})'}")

        r_sb = _purge_list(MemoryPurgeStandbyList)
        detail.append(f"清理Standby:{'成功' if r_sb == 0 else f'失败(0x{r_sb & 0xffffffff:X})'}")

        if aggressive:
            n, skipped = empty_process_working_sets(bl)
            detail.append(f"进程工作集:{n} 个" + (f"（白名单跳过 {skipped}）" if skipped else ""))
        else:
            detail.append("进程工作集:已跳过(保守档)")

        fc = clear_file_cache()
        detail.append(f"文件缓存:{'成功' if fc else '失败'}")

        # 核心 NT 操作全部失败 -> 视为整体失败，直接返回可读原因
        core = [r_fm, r_sb] + ([r_ws] if aggressive else [])
        if all(rc != 0 for rc in core):
            why = ("特权启用失败: " + ", ".join(failed_privs)) if failed_privs else "特权未启用或权限不足"
            log(f"{reason}清理失败 | {why} | {', '.join(detail)}")
            return {"ok": False, "msg": f"清理失败（{why}）"}

        time.sleep(1.5)  # 等系统把页回收计入可用内存
        after = get_mem()

        freed = after["avail_phys"] - before["avail_phys"]
        result = {
            "ok": True,
            "freed": freed,
            "before": before,
            "after": after,
            "level": lvl,
            "detail": ", ".join(detail),
        }
        log(f"{reason}清理 | 可用物理 {gb(before['avail_phys'])} -> {gb(after['avail_phys'])} "
            f"(释放 {gb(max(freed, 0))}) | 提交 {before['commit_pct']:.0f}% -> {after['commit_pct']:.0f}% | {result['detail']}")
        return result
    finally:
        # 仅禁用"清理前处于禁用状态"的特权，避免高危特权（如 SeDebugPrivilege）常驻
        for p, st in prior.items():
            if st is not None and not (st & 0x2):
                disable_privilege(p)


def top_processes_list(n: int = 10) -> list:
    """返回物理内存占用最高的 n 个进程 [(name, rss_bytes, pid), ...]。"""
    rows = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            rss = p.info["memory_info"].rss
            if rss:
                rows.append((p.info["name"] or "?", rss, p.pid))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:n]


def top_processes(n: int = 10) -> str:
    lines = [f"{i + 1:>2}. {name:<28} {rss / 1024 ** 3:>6.2f} GB   (PID {pid})"
             for i, (name, rss, pid) in enumerate(top_processes_list(n))]
    return "\n".join(lines)
