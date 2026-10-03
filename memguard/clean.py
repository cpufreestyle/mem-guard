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
from .config import CLEAN_LEVELS, _CONFIG_LOCK, gb, load_config, log, save_config
from .privileges import clean_privileges
from .winapi import get_mem, is_admin, process_working_sets

# 快路径句柄被拒的进程数超过该上限就不再用 psutil 补齐（见 top_processes_list）
_FILL_MAX = 24


def _status(rc: int) -> str:
    """把 NTSTATUS 渲染为可读文案。"""
    return "成功" if rc == 0 else f"失败(0x{rc & 0xffffffff:X})"


# 清理后等系统把回收的页计入可用物理内存：最多等一个窗口；连续 _RECLAIM_SETTLE 秒
# 没有回升就提前收尾——那说明这次清理没释放出可用内存，再等也不会变（旧实现会
# 把整个窗口等满，手动清理时能明显感觉到托盘卡住）。
_RECLAIM_WINDOW = 1.5
_RECLAIM_SAMPLE = 0.15
_RECLAIM_SETTLE = 0.45


def _wait_avail_rise(before: dict) -> dict:
    """轮询等待可用物理内存回升，返回最后一次采样。

    一回升就返回（和"干等"相比反馈最快）；迟迟不回升则最多多等 _RECLAIM_SETTLE 秒，
    不会把整个窗口等满。
    """
    deadline = time.time() + _RECLAIM_WINDOW
    flat_since = time.time()
    after = get_mem()
    while time.time() < deadline:
        if after["avail_phys"] > before["avail_phys"]:
            return after
        if time.time() - flat_since >= _RECLAIM_SETTLE:
            break
        time.sleep(_RECLAIM_SAMPLE)
        after = get_mem()
    return after


def _bump_stats(freed: int, escalated: bool = False) -> dict:
    """累计清理次数与释放量并落盘（对标 Mem Reduct 的统计），返回写入口径。

    在 _CONFIG_LOCK 内重新读盘再改：do_clean 开头读的那份配置可能已被菜单勾选
    或用户手改覆盖掉，直接写回会把别人的改动一起回滚。

    escalated=True 时同步累计「自动升档次数」，用于统计行展示保守清理不达
    标后补激进清理的触发频次（normalize_config 只持久化非 0 值）。
    """
    with _CONFIG_LOCK:
        cfg = load_config()
        st = cfg.get("stats") or {}
        st["count"] = int(st.get("count", 0)) + 1
        st["freed"] = int(st.get("freed", 0)) + max(freed, 0)
        if escalated:
            st["escalated"] = int(st.get("escalated", 0)) + 1
        cfg["stats"] = st
        save_config(cfg)
        return dict(st)


def _run_clean_actions(aggressive: bool, areas: dict, blacklist) -> tuple:
    """在「用完即关」的特权窗口内执行全部清理动作。

    返回 (detail, core, failed_privs)：detail 是人看的明细文案，core 是参与整体
    成败判断的核心 NT 操作返回码（0=成功），failed_privs 是启用失败的特权名。
    只编排动作，不做成败判定与统计（见 do_clean）。
    """
    with clean_privileges() as failed_privs:
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
        else:
            detail.append("刷写修改页:已关闭")

        if areas.get("standby", True):
            r_sb = purge_standby_list()
            core.append(r_sb)
            detail.append(f"清理Standby:{_status(r_sb)}")
        else:
            detail.append("清理Standby:已关闭")

        # 低优先级 standby：影响面更小的一步（部分系统不支持该命令，会如实展示失败码）
        if areas.get("low_priority_standby", True):
            r_lp = purge_low_priority_standby()
            detail.append(f"清理低优先级Standby:{_status(r_lp)}")

        if aggressive:
            n, skipped = empty_process_working_sets(blacklist)
            detail.append(f"进程工作集:{n} 个" + (f"（白名单跳过 {skipped}）" if skipped else ""))
        else:
            detail.append("进程工作集:已跳过(保守档)")

        if areas.get("file_cache", True):
            fc = clear_system_file_cache()
            detail.append(f"文件缓存:{'成功' if fc else '失败'}")
        else:
            detail.append("文件缓存:已关闭")

        return detail, core, failed_privs


def _still_pressured(m: dict, cfg: dict) -> bool:
    """清理后是否仍超阈值：物理或提交使用率任一仍越线、或可用物理仍低于
    「低内存触发」下限（min_avail_mb，0=关闭），就算没清到位。

    用 .get 老老实实取值——部分采样桩/回退路径不含 phys_pct，缺项视为未越线，
    别在这里 KeyError。阈值缺失同样跳过对应判断（配置总是 normalize 过的，多为兜底）。
    """
    pt = cfg.get("phys_threshold")
    ct = cfg.get("commit_threshold")
    phys = m.get("phys_pct")
    commit = m.get("commit_pct")
    if pt is not None and phys is not None and phys >= pt:
        return True
    if ct is not None and commit is not None and commit >= ct:
        return True
    floor = cfg.get("min_avail_mb") or 0
    avail = m.get("avail_phys")
    if floor and avail is not None and avail < floor * 1024 * 1024:
        return True
    return False


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

    # before 必须在动手前采样，否则"释放了多少"无从算起
    before = get_mem()
    detail, core, failed_privs = _run_clean_actions(aggressive, areas, bl)

    # 核心 NT 操作全部失败 -> 视为整体失败，直接返回可读原因
    if core and all(rc != 0 for rc in core):
        why = ("特权启用失败: " + ", ".join(failed_privs)) if failed_privs else "特权未启用或权限不足"
        log(f"{reason}清理失败 | {why} | {', '.join(detail)}")
        return {"ok": False, "msg": f"清理失败（{why}）"}

    after = _wait_avail_rise(before)
    escalated = False

    # 自动升档：保守清理后若仍超阈值，立即补一次激进清理（最多一次、带开关、最小打扰）
    if (not aggressive and bool(cfg.get("escalate_clean", True))
            and _still_pressured(after, cfg)):
        detail2, core2, _ = _run_clean_actions(True, areas, bl)
        # 第二遍核心操作全部失败就不记升档，只在日志里留痕，保留第一次的保守结果
        if core2 and all(rc != 0 for rc in core2):
            log(f"{reason}升档未采纳 | 二次激进清理核心操作全部失败 | " + ", ".join(detail2))
        else:
            # 采纳升档：再等一次把激进清理的回补计入；整体释放量按 before->最终 after 合计
            after = _wait_avail_rise(after)
            escalated = True
            lvl = "aggressive"
            detail.append("升档重效(激进)")
            detail.extend(detail2)
            if _still_pressured(after, cfg):
                # 最多一次，不再补第二次；只留痕，让日志能解释「刚清完怎么又超了」
                log(f"{reason}升档后仍超阈值（最多一次，不再补） | "
                    f"可用物理 {gb(after['avail_phys'])} | "
                    f"物理 {after.get('phys_pct', '-')}% / 提交 {after.get('commit_pct', '-')}%")

    freed = after["avail_phys"] - before["avail_phys"]
    result = {
        "ok": True,
        "freed": freed,
        "before": before,
        "after": after,
        "level": lvl,
        "escalated": escalated,
        "detail": ", ".join(detail),
    }
    # 累计统计：只统计真正成功的清理，随配置持久化；写盘失败不影响本次清理结果
    try:
        result["stats"] = _bump_stats(freed, escalated)
    except Exception:
        pass
    log(f"{reason}清理 | 可用物理 {gb(before['avail_phys'])} -> {gb(after['avail_phys'])} "
        f"(释放 {gb(max(freed, 0))}) | 提交 {before['commit_pct']:.0f}% -> {after['commit_pct']:.0f}% | {result['detail']}")
    return result


def top_processes_list(n: int = 10) -> list:
    """返回物理内存占用最高的 n 个进程 [(name, rss_bytes, pid), ...]。

    主用 Toolhelp32 快路径（一次全量约 16ms）；其中句柄被拒绝的受保护进程
    （工作集为 0）只对这几个 PID 用 psutil 定点补齐，整体失败才回退到 psutil
    全量扫描（约 1s，仅兜底）。
    """
    try:
        rows = process_working_sets()
    except Exception:
        return _top_processes_via_psutil(n)

    by_pid = {pid: [name, rss] for name, rss, pid in rows}
    denied = [p for p, (_, rss) in by_pid.items() if not rss]
    # 管理员下句柄被拒的通常只有个位数（PPL 保护进程），补齐它们才划算；
    # 非管理员下别人会话的进程会成片被拒，逐个 psutil 补齐会把整体从十几毫秒拖回秒级，
    # 这时宁可少几行也不补。
    if len(denied) <= _FILL_MAX:
        for pid in denied:
            try:
                by_pid[pid][1] = psutil.Process(pid).memory_info().rss
            except Exception:
                continue    # 进程已退出或完全不可读，跳过这一行
    out = [(nm, rss, pid) for pid, (nm, rss) in by_pid.items() if rss]
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:n]


def _top_processes_via_psutil(n: int = 10) -> list:
    """psutil 全量扫描：快路径不可用时的兜底，口径与历史实现一致。"""
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
