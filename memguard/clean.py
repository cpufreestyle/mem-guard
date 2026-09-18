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
    purge_low_priority_standby,
    purge_standby_list,
    purge_working_sets,
)
from .config import CLEAN_LEVELS, gb, load_config, log, save_config
from .privileges import clean_privileges
from .winapi import get_mem, is_admin


def _status(rc: int) -> str:
    """把 NTSTATUS 渲染为可读文案。"""
    return "成功" if rc == 0 else f"失败(0x{rc & 0xffffffff:X})"


def do_clean(reason: str = "手动", level: str | None = None, user_blacklist=None) -> dict:
    """执行一次清理，返回结果统计（特权在返回前恢复，用完即关）。

    level: "conservative"（默认，只清 standby/修改页/文件缓存）或
    "aggressive"（在此基础上额外清空各进程工作集）；None 时读取配置里的 clean_level。
    具体清理哪些区域由配置 clean_areas 控制（对标 WinMemoryCleaner 的勾选项）；
    全部核心区域被关闭时不会误报失败。清理成功后更新累计统计（stats）并持久化。
    """
    if not is_admin():
        return {"ok": False, "msg": "需要管理员权限才能清理内存"}

    cfg = load_config()
    lvl = str(level or cfg.get("clean_level") or "conservative").strip().lower()
    if lvl not in CLEAN_LEVELS:
        lvl = "conservative"
    aggressive = lvl == "aggressive"
    bl = cfg.get("user_blacklist") if user_blacklist is None else user_blacklist
    areas = cfg.get("clean_areas") or {}

    with clean_privileges() as failed_privs:
        before = get_mem()
        detail = [f"档位:{'激进' if aggressive else '保守'}"]
        if failed_privs:
            detail.append("特权启用失败:" + ",".join(failed_privs))

        core = []  # 参与整体成败判断的核心 NT 操作返回码

        if aggressive:
            if areas.get("working_sets", True):
                r_ws = purge_working_sets()
                core.append(r_ws)
                detail.append(f"清空工作集:{_status(r_ws)}")
            else:
                detail.append("清空工作集:已关闭")

        if areas.get("modified", True):
            r_fm = flush_modified_list()
            core.append(r_fm)
            detail.append(f"刷写修改页:{_status(r_fm)}")

        if areas.get("standby", True):
            r_sb = purge_standby_list()
            core.append(r_sb)
            detail.append(f"清理Standby:{_status(r_sb)}")

        # 低优先级 standby：影响面更小的一步（部分系统不支持该命令，会如实展示失败码）
        if areas.get("low_priority_standby", True):
            r_lp = purge_low_priority_standby()
            detail.append(f"清理低优先级Standby:{_status(r_lp)}")

        if aggressive:
            n, skipped = empty_process_working_sets(bl)
            detail.append(f"进程工作集:{n} 个" + (f"（白名单跳过 {skipped}）" if skipped else ""))
        else:
            detail.append("进程工作集:已跳过(保守档)")

        if areas.get("file_cache", True):
            fc = clear_system_file_cache()
            detail.append(f"文件缓存:{'成功' if fc else '失败'}")
        else:
            detail.append("文件缓存:已关闭")

        # 核心 NT 操作全部失败 -> 视为整体失败，直接返回可读原因
        if core and all(rc != 0 for rc in core):
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
        # 累计统计（对标 Mem Reduct）：只统计真正成功的清理，随配置持久化
        try:
            st = cfg.get("stats") or {}
            st["count"] = int(st.get("count", 0)) + 1
            st["freed"] = int(st.get("freed", 0)) + max(freed, 0)
            cfg["stats"] = st
            save_config(cfg)
            result["stats"] = dict(st)
        except Exception:
            pass
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
