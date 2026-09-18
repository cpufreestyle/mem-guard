# -*- coding: utf-8 -*-
"""
清理编排与统计：do_clean 编排各清理动作并汇总结果；另含 Top 进程统计。

依赖：
    - config：档位、配置读取、日志、格式化
    - winapi：内存读取、管理员判断
    - privileges：特权启用与用后恢复
    - actions：单个清理动作
"""
from __future__ import annotations

import time

import psutil

from .actions import (
    clear_system_file_cache,
    empty_process_working_sets,
    flush_modified_list,
    purge_standby_list,
    purge_working_sets,
)
from .config import CLEAN_LEVELS, gb, load_config, log
from .privileges import clean_privileges
from .winapi import get_mem, is_admin


def do_clean(reason: str = "手动", level: str | None = None, user_blacklist=None) -> dict:
    """执行一次清理，返回结果统计（特权在返回前恢复，用完即关）。

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

    with clean_privileges() as failed_privs:
        before = get_mem()
        detail = [f"档位:{'激进' if aggressive else '保守'}"]
        if failed_privs:
            detail.append("特权启用失败:" + ",".join(failed_privs))

        r_ws = 0
        if aggressive:
            r_ws = purge_working_sets()
            detail.append(f"清空工作集:{'成功' if r_ws == 0 else f'失败(0x{r_ws & 0xffffffff:X})'}")

        r_fm = flush_modified_list()
        detail.append(f"刷写修改页:{'成功' if r_fm == 0 else f'失败(0x{r_fm & 0xffffffff:X})'}")

        r_sb = purge_standby_list()
        detail.append(f"清理Standby:{'成功' if r_sb == 0 else f'失败(0x{r_sb & 0xffffffff:X})'}")

        if aggressive:
            n, skipped = empty_process_working_sets(bl)
            detail.append(f"进程工作集:{n} 个" + (f"（白名单跳过 {skipped}）" if skipped else ""))
        else:
            detail.append("进程工作集:已跳过(保守档)")

        fc = clear_system_file_cache()
        detail.append(f"文件缓存:{'成功' if fc else '失败'}")

        # 核心 NT 操作全部失败 -> 视为整体失败，直接返回可读原因
        core = [r_fm, r_sb] + ([r_ws] if aggressive else [])
        if all(rc != 0 for rc in core):
            why = ("特权启用失败: " + ", ".join(failed_privs)) if failed_privs else "特权未启用或权限不足"
            log(f"{reason}清理失败 | {why} | {', '.join(detail)}")
            return {"ok": False, "msg": f"清理失败（{why}）"}

        # 等系统把回收的页计入可用内存：轮询最多 1.5s，一旦可用物理回升立即返回，
        # 比原来固定 sleep(1.5) 更快给出反馈（手动清理时尤其明显）。
        deadline = time.time() + 1.5
        after = get_mem()
        while time.time() < deadline and after["avail_phys"] <= before["avail_phys"]:
            time.sleep(0.15)
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
