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
    empty_background_working_sets,
    empty_process_working_sets,
    empty_selected_working_sets,
    flush_modified_list,
    purge_low_priority_standby,
    purge_standby_list,
    purge_working_sets,
)
from .config import CLEAN_LEVELS, _CONFIG_LOCK, gb, load_config, log, save_config
from .privileges import clean_privileges
from .winapi import get_mem, is_admin, process_working_sets, visible_window_pids

# 快路径句柄被拒的进程数超过该上限就不再用 psutil 补齐（见 top_processes_list）
_FILL_MAX = 24

# 趋势预防式清理（v1.8.0）：按历史采样拟合斜率，预判多久后触及阈值
_PREDICT_MIN_SAMPLES = 6      # 至少这么多个采样点才谈「趋势」，刚启动的噪声不作数
_PREDICT_MIN_SPAN = 60.0      # 采样至少要覆盖这么长时间(秒)，否则一分钟的抖动也叫趋势
_PREDICT_MAX_SAMPLES = 60     # 最多回看这么多个点（默认 10s 间隔约等于 10 分钟）
_PREDICT_MIN_SLOPE = 0.02     # 最小有效斜率(%/秒，即 1.2%/分钟)：低于它视为平稳

# 泄漏进程增长感知（v1.11.0）：按历史采样拟合每个进程的 RSS 上升斜率
_LEAK_MIN_SAMPLES = 5        # 至少这么多采样点才拟合，避免进程刚启动的毛刺
_LEAK_MIN_SPAN = 300.0       # 采样至少要覆盖这么长时间(秒)，五分钟内的抖动不算泄漏
_LEAK_MIN_SLOPE = 128 * 1024  # 每秒至少涨这么多字节(≈7.5MB/分钟)才算异常
_LEAK_MIN_GROWTH = 64 * 1024 ** 2  # 且净增长至少这么多(64MB)：排除高位平台期的正常占用
_LEAK_MAX_ROWS = 5           # 最多报这么多个，别把通知/建议列表刷满
GROWTH_MAX_SAMPLES = 40      # Guard.proc_history 采样上限（默认 10s 间隔≈6.5 分钟），tray 共用


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


def _bump_stats(freed: int, escalated: bool = False, targeted: bool = False,
                preventive: bool = False, short_relief: bool = False,
                bg_trimmed: bool = False, sticky: bool = False) -> dict:
    """累计清理次数与释放量并落盘（对标 Mem Reduct 的统计），返回写入口径。

    在 _CONFIG_LOCK 内重新读盘再改：do_clean 开头读的那份配置可能已被菜单勾选
    或用户手改覆盖掉，直接写回会把别人的改动一起回滚。

    escalated=True 时同步累计「自动升档次数」，用于统计行展示保守清理不达
    标后补激进清理的触发频次（normalize_config 只持久化非 0 值）。
    targeted=True 时同步累计「定向清理次数」，用于统计行展示保守清理后
    精确清大户的触发频次。
    preventive=True 时同步累计「趋势预防式清理次数」：v1.8.0 起按上升斜率
    提前清理的触发频次（normalize_config 只持久化非 0 值）。
    short_relief=True 时同步累计「清理效果偏短次数」：v1.9.0 起距上次自动清理
    不足 effect_min_relief_sec 说明上次清理没 hold 住、压力很快复发（口径同上）。
    bg_trimmed=True 时同步累计「后台进程工作集清理次数」：v1.11.0 起清空后台
    进程工作集的触发频次，供统计行与通知展示（normalize_config 只持久化非 0 值）。
    sticky=True 时同步累计「粘滞激进次数」：v1.12.0 起持续高压时按激进执行的
    触发频次，供统计行与通知展示（normalize_config 同样只持久化非 0 值）。
    """
    with _CONFIG_LOCK:
        cfg = load_config()
        st = cfg.get("stats") or {}
        st["count"] = int(st.get("count", 0)) + 1
        st["freed"] = int(st.get("freed", 0)) + max(freed, 0)
        if escalated:
            st["escalated"] = int(st.get("escalated", 0)) + 1
        if targeted:
            st["targeted"] = int(st.get("targeted", 0)) + 1
        if preventive:
            st["preventive"] = int(st.get("preventive", 0)) + 1
        if short_relief:
            st["short_relief"] = int(st.get("short_relief", 0)) + 1
        if bg_trimmed:
            st["bg_trimmed"] = int(st.get("bg_trimmed", 0)) + 1
        if sticky:
            st["sticky"] = int(st.get("sticky", 0)) + 1
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


def _target_clean_candidates(rows, cfg, extra_rows=None) -> list:
    """从 Top 进程采样里挑出「工作集大户」：≥ target_clean_min_mb，取前 target_clean_top 个。

    rows 是 top_processes_list 的返回 [(name, rss_bytes, pid), ...]（已按工作集降序）；
    extra_rows 是 leak_candidates() 的返回：疑似泄漏的进程排在最前且不受 min_mb
    下限约束（泄漏初期可能还没到「大户」级别，但那正是最该掐的时候），仍受 top
    截断并按 PID 去重。开关关闭或 top ≤0 时返回 []；min_mb ≤0 时只返回 extras。
    纯函数，便于单测。
    """
    if not cfg.get("target_clean", True):
        return []
    min_bytes = int(cfg.get("target_clean_min_mb") or 0) * 1024 * 1024
    top = int(cfg.get("target_clean_top") or 0)
    if top <= 0:
        return []
    out = []
    seen = set()
    for row in extra_rows or []:
        try:
            name, rss, pid = row
        except (TypeError, ValueError):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        out.append((name, rss, pid))
        if len(out) >= top:
            return out
    if min_bytes <= 0:
        return out
    for row in rows or []:
        if len(out) >= top:
            break
        try:
            name, rss, pid = row
        except (TypeError, ValueError):
            continue
        if int(rss) >= min_bytes and pid not in seen:
            seen.add(pid)
            out.append((name, rss, pid))
    return out


def _run_target_clean(cands, blacklist) -> tuple:
    """在特权窗口内只清空候选大户的工作集，返回 (已清理数, 跳过数)。"""
    with clean_privileges():
        return empty_selected_working_sets([pid for _, _, pid in cands], blacklist)


def _run_bg_trim(exclude_pids, blacklist) -> tuple:
    """在特权窗口内清空后台进程工作集，返回 (已清理数, 跳过数)。"""
    with clean_privileges():
        return empty_background_working_sets(exclude_pids, blacklist)


def _slope_per_sec(samples, idx: int) -> float:
    """最小二乘拟合 samples 第 idx 列随时钟(第 0 列，epoch 秒)的斜率，单位/秒。

    采样点至少 2 个、且时间戳不能全相同（sxx<=0 时无斜率可言）。"""
    n = len(samples)
    if n < 2:
        return 0.0
    t0 = samples[0][0]
    xs = [row[0] - t0 for row in samples]
    ys = [float(row[idx]) for row in samples]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx


def predictive_due(history, cfg: dict):
    """趋势预防判定：预计窗口内触及阈值就返回触阈倒计时，否则 None（纯函数）。

    history 是 (t, phys_pct, commit_pct) 采样列表（新在后，即 Guard.history）；
    cfg 读 predict_clean / predict_window_min / 两个阈值。采样不足、跨度太短、
    读数为平稳(斜率低于 _PREDICT_MIN_SLOPE)、已越线(那是超阈值清理的活)、
    或预计触阈时间超出窗口，一律 None——任何一条不满足都不该提前打扰。
    返回 {"eta": 秒, "slope_pm": %/分钟, "metric": "物理"|"提交"}，eta 取两者较小。
    """
    if not cfg.get("predict_clean", True):
        return None
    samples = list(history or [])[-_PREDICT_MAX_SAMPLES:]
    if len(samples) < _PREDICT_MIN_SAMPLES:
        return None
    if samples[-1][0] - samples[0][0] < _PREDICT_MIN_SPAN:
        return None
    try:
        window = float(int(cfg.get("predict_window_min", 5) or 0)) * 60.0
    except (TypeError, ValueError):
        return None
    if window <= 0:
        return None
    best = None
    for idx, threshold, metric in ((1, cfg.get("phys_threshold"), "物理"),
                                   (2, cfg.get("commit_threshold"), "提交")):
        if threshold is None:
            continue
        slope = _slope_per_sec(samples, idx)
        if slope < _PREDICT_MIN_SLOPE:
            continue    # 涨得太慢：交给预警气泡和超阈值清理，别提前动
        now_pct = float(samples[-1][idx])
        if now_pct >= threshold:
            continue    # 已经越线：预防不抢超阈值触发源的活
        eta = (threshold - now_pct) / slope
        if eta <= window and (best is None or eta < best["eta"]):
            best = {"eta": eta, "slope_pm": slope * 60.0, "metric": metric}
    return best


def _pid_series(history) -> dict:
    """把 Guard.proc_history 的采样摊平成 {pid: (name, [(t, rss_bytes), ...])}。

    只有每个采样点都出现的进程才进入结果——中途启动或退出的进程不是趋势，是噪声。
    单条采样脏了(结构不符)只跳过那一层，别让一次读写异常掀翻整个识别。
    """
    total = len(history or [])
    series = {}
    for sample in history or []:
        try:
            t, snap = sample
        except (TypeError, ValueError):
            continue
        for pid, item in (snap or {}).items():
            try:
                name, rss = item
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            series.setdefault(pid, []).append((float(t), name, int(rss)))
    out = {}
    for pid, pts in series.items():
        if len(pts) != total:
            continue    # 中途启动或退出：不是趋势，是噪声
        out[pid] = (pts[0][1], [(p[0], p[2]) for p in pts])
    return out


def growth_slopes(history) -> list:
    """对全程在场的进程拟合 RSS 上升斜率，按斜率降序返回
    [(pid, name, rss_bytes, slope_bytes_per_sec, net_growth_bytes), ...]。

    history 是 (t, {pid: (name, rss_bytes)}) 采样列表（新在后，即 Guard.proc_history）。
    沿用 /_slope_per_sec/ 的最小二乘；样本数、时间跨度不足或斜率 ≤0 的进程一律
    剔除。纯函数，便于单测。
    """
    samples = list(history or [])
    if len(samples) < _LEAK_MIN_SAMPLES:
        return []
    if samples[-1][0] - samples[0][0] < _LEAK_MIN_SPAN:
        return []
    rows = []
    for pid, (name, pts) in _pid_series(samples).items():
        slope = _slope_per_sec(pts, 1)
        if slope <= 0:
            continue
        rows.append((pid, name, pts[-1][1], slope, pts[-1][1] - pts[0][1]))
    rows.sort(key=lambda r: r[3], reverse=True)
    return rows


def leak_candidates(history) -> list:
    """泄漏进程识别：斜率与净增长双双越线才算疑似泄漏，返回 [(name, rss, pid), ...]。

    单看斜率会把「本来就大、只是偶发分配」误判，单看增量会把良性冷启动误判，
    两个条件都要满足（详见 _LEAK_MIN_SLOPE / _LEAK_MIN_GROWTH）。结果按斜率
    降序、最多 _LEAK_MAX_ROWS 条，供定向清理优先下刀与 advisor 提醒使用。
    纯函数，便于单测。
    """
    out = []
    for pid, name, rss, slope, growth in growth_slopes(history):
        if slope < _LEAK_MIN_SLOPE or growth < _LEAK_MIN_GROWTH:
            continue
        out.append((name, rss, pid))
        if len(out) >= _LEAK_MAX_ROWS:
            break
    return out


def do_clean(reason: str = "手动", level: str | None = None, user_blacklist=None,
                preventive: bool = False, low_relief: bool = False,
                growth_rows=None, sticky: bool = False, skip=()) -> dict:
    """执行一次清理，返回结果统计（特权在返回前恢复，用完即关）。

    level: "conservative"（默认，只清 standby/修改页/文件缓存）或
    "aggressive"（在此基础上额外清空各进程工作集）；None 时读取配置里的 clean_level。
    具体清理哪些区域由配置 clean_areas 控制（对标 WinMemoryCleaner 的勾选项）；
    全部核心区域被关闭时不会误报失败。清理成功后更新累计统计（stats）并持久化。
    preventive=True 表示本次由「趋势预防式清理」触发（v1.8.0）：只多累计一个
    stats.preventive 计数供统计行与建议引擎使用，清理动作本身完全一样。
    low_relief=True 表示本次距上次自动清理不足 effect_min_relief_sec（v1.9.0
    效果闭环）：只多累计一个 stats.short_relief 计数供统计行、通知与 advisor
    自调优使用，清理动作本身完全一样。
    growth_rows 是 leak_candidates() 的返回（疑似泄漏进程）：与大户采样合并后
    作为定向清理的优先候选，泄漏初期还没长成大户也能第一时间掐住。
    sticky=True 表示「持续压力粘滞激进」（v1.12.0）：上一轮走完整条阶梯仍没压住
    内存压力，这轮不再从保守档重跑，直接按激进档执行并回传 sticky 标记，供托盘
    进入/退出粘滞态；阶梯各级本就只在 not aggressive 时跑，粘滞即天然跳过整条阶梯。
    skip 是本轮跳过的阶段名集合（「targeted」「bg」 的子集），来源托盘的阶段自
    学习：某一级连续多次没释放出东西就跳过它，只影响阶梯阶段、不影响升档本身。
    """
    if not is_admin():
        return {"ok": False, "msg": "需要管理员权限才能清理内存"}

    cfg = load_config()
    lvl = str(level or cfg.get("clean_level") or "conservative").strip().lower()
    if lvl not in CLEAN_LEVELS:
        lvl = "conservative"
    aggressive = lvl == "aggressive"
    if sticky:
        # v1.12.0 持续压力粘滞激进：上一轮使完整条阶梯仍没压住，这轮直接上最强力度；
        # 阶梯各级本就只在 not aggressive 时跑，粘滞即天然跳过整条阶梯
        aggressive = True
        lvl = "aggressive"
    bl = cfg.get("user_blacklist") if user_blacklist is None else user_blacklist
    areas = cfg.get("clean_areas") or {}

    # before 必须在动手前采样，否则"释放了多少"无从算起
    before = get_mem()
    detail, core, failed_privs = _run_clean_actions(aggressive, areas, bl)
    if sticky:
        detail.append("粘滞激进(持续高压)")

    # 核心 NT 操作全部失败 -> 视为整体失败，直接返回可读原因
    if core and all(rc != 0 for rc in core):
        why = ("特权启用失败: " + ", ".join(failed_privs)) if failed_privs else "特权未启用或权限不足"
        log(f"{reason}清理失败 | {why} | {', '.join(detail)}")
        return {"ok": False, "msg": f"清理失败（{why}）"}

    after = _wait_avail_rise(before)
    escalated = False
    targeted = []
    # v1.12.0 分阶段释放量：拆开报给阶段自学习（哪一级真的清到了东西）
    targeted_freed = 0
    bg_freed = 0
    escalated_freed = 0

    # 定向清理（v1.7.0 阶梯第一级）：保守清理后仍超阈值时，先只对工作集最大的几个
    # 进程精确清空——比直接全量升档的打扰小；仍不达标才轮到升档的「全清」
    if (not aggressive and "targeted" not in skip
            and bool(cfg.get("target_clean", True))
            and _still_pressured(after, cfg)):
        cands = []
        try:
            cands = _target_clean_candidates(top_processes_list(15), cfg, growth_rows)
        except Exception:
            cands = []
        if cands:
            pre = after["avail_phys"]
            n, skipped = _run_target_clean(cands, bl)
            names = "、".join(f"{name} {gb(rss)}" for name, rss, _ in cands)
            if n:
                # 采纳定向：再等一次把回补计入，供下面的升档判断复用最新 after
                after = _wait_avail_rise(after)
                targeted = list(cands)
                targeted_freed = max(after["avail_phys"] - pre, 0)
                detail.append(f"定向清理大户({names}): {n} 个"
                              + (f"（跳过 {skipped}）" if skipped else ""))
            else:
                detail.append(f"定向清理大户({names}): 未成功")

    # 后台进程工作集清理（v1.11.0 阶梯第二级）：定向大户仍不达标时，清空「没有可见
    # 顶层窗口」的后台进程工作集——比直接全量升档的打扰小（前台正在访问的页不动），
    # 覆盖面又比定向大户全；枚举不到窗口时宁可不做，绝不能当成「都没有窗口」
    bg_trimmed = 0
    if (not aggressive and "bg" not in skip
            and bool(cfg.get("bg_trim", True))
            and _still_pressured(after, cfg)):
        try:
            exclude = visible_window_pids()
        except Exception:
            exclude = None
        if exclude is None:
            detail.append("后台进程工作集:跳过(无法枚举窗口)")
            log(f"{reason}后台清理跳过 | 枚举窗口不可信(EnumWindows 失败或触顶)，宁可不做")
        else:
            pre = after["avail_phys"]
            try:
                bg_trimmed, skipped = _run_bg_trim(exclude, bl)
            except Exception:
                bg_trimmed, skipped = 0, 0
            if bg_trimmed:
                # 采纳后台清理：再等一次把回补计入，供下面的升档判断复用最新 after
                after = _wait_avail_rise(after)
                bg_freed = max(after["avail_phys"] - pre, 0)
                detail.append(f"后台进程工作集:{bg_trimmed} 个"
                              + (f"（跳过 {skipped}）" if skipped else ""))
            else:
                detail.append("后台进程工作集:未成功")

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
            pre = after["avail_phys"]
            escalated = True
            escalated_freed = max(after["avail_phys"] - pre, 0)
            lvl = "aggressive"
            detail.append("升档重效(激进)")
            detail.extend(detail2)
            if _still_pressured(after, cfg):
                # 最多一次，不再补第二次；只留痕，让日志能解释「刚清完怎么又超了」
                log(f"{reason}升档后仍超阈值（最多一次，不再补） | "
                    f"可用物理 {gb(after['avail_phys'])} | "
                    f"物理 {after.get('phys_pct', '-')}% / 提交 {after.get('commit_pct', '-')}%")

    still = _still_pressured(after, cfg)
    freed = after["avail_phys"] - before["avail_phys"]
    result = {
        "ok": True,
        "freed": freed,
        "before": before,
        "after": after,
        "level": lvl,
        "escalated": escalated,
        "targeted": targeted,
        "bg_trim": bg_trimmed,
        "targeted_freed": targeted_freed,
        "bg_freed": bg_freed,
        "escalated_freed": escalated_freed,
        "sticky": sticky,
        "still": still,
        "detail": ", ".join(detail),
    }
    # 累计统计：只统计真正成功的清理，随配置持久化；写盘失败不影响本次清理结果
    try:
        result["stats"] = _bump_stats(freed, escalated, bool(targeted), preventive,
                                     low_relief, bool(bg_trimmed), sticky)
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
