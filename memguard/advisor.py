# -*- coding: utf-8 -*-
"""
优化建议引擎：基于实时内存状态与配置，给出可操作的「实用建议」。

只依赖标准库 + config / winapi / clean，被 tray（菜单展示）、ui（建议窗口一键应用）
与 cli（自检）调用；
自身不含 UI、不含网络，纯本地分析。模块依赖落在 clean 之后：
    config ← winapi ← clean ← advisor ← tray ← cli
"""
from __future__ import annotations

from .clean import top_processes_list
from .config import _norm_proc_name, gb
from .winapi import get_mem, is_admin

# 激进档下常被清空工作集、导致卡顿的常见程序（建议加入 user_blacklist）
_AGGRESSIVE_PRONE = {
    "chrome", "msedge", "firefox", "brave", "opera", "code", "vscode",
    "devenv", "idea", "pycharm", "cursor", "explorer", "dwm", "rider",
    "webview", "spotify", "steam", "electron",
}

LEVEL_WARN = "warn"
LEVEL_TIP = "tip"
LEVEL_INFO = "info"
_LEVEL_ORDER = {LEVEL_WARN: 0, LEVEL_TIP: 1, LEVEL_INFO: 2}
# 激进档被判定为「杀鸡用牛刀」的余量：物理/提交占用距阈值都这么多个百分点以上才算宽裕
_AGGRESSIVE_SLACK = 20


def frequent_escalation(cfg: dict) -> bool:
    """保守档是否「频繁升档」：累计清理里 escalated 过半且至少 3 次。

    v1.5.1 起 stats.escalated 记录保守清理后仍需补激进的次数；本判据被 tray 的
    自动改激进（v1.6.0 自调优）与 analyze 的 3b 建议共用，两处必须同源，否则
    「已自动切了激进」与「还建议改激进」会各说各话。档位已是激进时返回 False
    ——没有「再升一档」的空间，先清到位就不存在频繁补刀。
    """
    st = cfg.get("stats") or {}
    clean_cnt = int(st.get("count", 0) or 0)
    esc_cnt = int(st.get("escalated", 0) or 0)
    if clean_cnt <= 0 or esc_cnt < 3 or esc_cnt * 2 < clean_cnt:
        return False
    return str(cfg.get("clean_level", "conservative")).strip().lower() != "aggressive"


def frequent_prevention(cfg: dict) -> bool:
    """趋势预防式清理是否「频繁」：累计清理里 preventive 过半且至少 3 次。

    v1.8.0 起 stats.preventive 记录按上升斜率提前清理的次数；预防式清理都发生在
    真触阈之前，若它频繁到过半，说明这台机器内存持续快速上涨，「提前温和一次」
    也只是拖延，不如一次清到位。与 frequent_escalation 同源同口径，档位已是
    激进时返回 False——没有更大力度的空间可建议。
    """
    st = cfg.get("stats") or {}
    clean_cnt = int(st.get("count", 0) or 0)
    prev_cnt = int(st.get("preventive", 0) or 0)
    if clean_cnt <= 0 or prev_cnt < 3 or prev_cnt * 2 < clean_cnt:
        return False
    return str(cfg.get("clean_level", "conservative")).strip().lower() != "aggressive"


def frequent_short_relief(cfg: dict) -> bool:
    """保守档清理是否「频繁短效」：累计清理里 short_relief 过半且至少 3 次。

    v1.9.0 起 stats.short_relief 记录距上次自动清理不足 effect_min_relief_sec
    的次数——上次清理没 hold 住、内存压力很快复发。若短效频繁到过半，说明这台
    机器上保守档每次清完都在短时间内又被顶回去，「先温和」纯属白跑一遍，不如
    一次清到位。与 frequent_escalation 同源同口径，档位已是激进时返回 False
    ——没有更大力度的空间可建议。被 tray 的自动改激进（v1.9.0 起自调优第二条
    判据）与 analyze 的 3b 建议共用，两处必须同源。
    """
    st = cfg.get("stats") or {}
    clean_cnt = int(st.get("count", 0) or 0)
    srt_cnt = int(st.get("short_relief", 0) or 0)
    if clean_cnt <= 0 or srt_cnt < 3 or srt_cnt * 2 < clean_cnt:
        return False
    return str(cfg.get("clean_level", "conservative")).strip().lower() != "aggressive"

def frequent_deepen(cfg: dict) -> bool:
    """定向加深是否「频繁」：累计清理里 deepen 过半且至少 3 次。

    v1.13.0 起 stats.deepen 记录「定向清理清到了东西、但压力没完全按住」从而把挑选网
    撒宽一轮的次数；v1.14.0 起加深可多轮，这里记的是「轮数」（一次加深 2 轮 +2）。
    加深是升档前的最后一道温和手段：若它频繁到过半，说明这台机器上「按工作集逐个挑」
    每轮都得再撒宽才够，与前三个判据同源同口径；档位已是激进时返回 False——激进档
    整条阶梯都被跳过，没有「再加深」的空间。
    """
    st = cfg.get("stats") or {}
    clean_cnt = int(st.get("count", 0) or 0)
    dpn_cnt = int(st.get("deepen", 0) or 0)
    if clean_cnt <= 0 or dpn_cnt < 3 or dpn_cnt * 2 < clean_cnt:
        return False
    return str(cfg.get("clean_level", "conservative")).strip().lower() != "aggressive"


def aggressive_not_needed(cfg: dict, mem: dict | None = None) -> bool:
    """激进档是否「杀鸡用牛刀」：档位激进但内存长期宽裕，建议切回保守（v1.10.0）。

    与 frequent_escalation 等同源同口径：被 tray 与 analyze 共用，两处必须说一样的话。
    三条同时满足才劝：档位是激进、物理与提交占用都距阈值 >= _AGGRESSIVE_SLACK 个百分点，
    且 min_avail_mb 关闭或可用物理仍高于其下限。采样缺失一律 False——宁可闭嘴也别瞎劝。
    mem 省略时才现场取一次采样。
    """
    if str(cfg.get("clean_level", "conservative")).strip().lower() != "aggressive":
        return False
    try:
        m = mem if mem is not None else get_mem()
    except Exception:
        return False
    if not isinstance(m, dict) or "phys_pct" not in m:
        return False
    phys_pct = m.get("phys_pct", 0)
    commit_pct = m.get("commit_pct", 0)
    if int(cfg.get("phys_threshold", 85)) - phys_pct < _AGGRESSIVE_SLACK:
        return False
    if int(cfg.get("commit_threshold", 90)) - commit_pct < _AGGRESSIVE_SLACK:
        return False
    min_avail = int(cfg.get("min_avail_mb", 0) or 0)
    if min_avail and m.get("avail_phys", 0) < min_avail * 1024 * 1024:
        return False
    return True


def advice_actions(items: list) -> list:
    """从建议列表抽出「可一键应用」的动作：按出现顺序去重，返回 [{"label", "changes"}]。

    changes 只带本次要改的键（增量），由调用方交给 config.update_config 读-改-写
    合并；若同一个键被多条建议同时惦记（例如既建议开自动清理又建议关），只保留
    最先出现的那条，避免一次点击互相打架。没有 action 的建议原样忽略。
    """
    out: list = []
    seen: set = set()
    for it in items:
        act = it.get("action") if isinstance(it, dict) else None
        if not act:
            continue
        changes = act.get("changes") or {}
        keys = tuple(sorted(changes))
        if not keys or keys in seen:
            continue
        seen.add(keys)
        out.append({"label": act.get("label", "应用"), "changes": dict(changes)})
    return out


def analyze(cfg: dict, mem: dict | None = None, top: list | None = None,
            leaks: list | None = None) -> list:
    """返回建议列表，每条为 {"level", "title", "text"}。

    mem / top 可外部传入（便于测试，或让监控线程复用同一份采样，避免每轮刷新都扫全进程）；
    省略时现场取值。任何单条建议计算失败都不应阻断其它建议，故逐段 try 容错。
    leaks 是 clean.leak_candidates() 的返回（疑似泄漏进程），传入时多一条泄漏
    提醒；省略（cli / 一次性自检）时不出这条建议。
    """
    items: list = []
    try:
        s = mem if mem is not None else get_mem()
    except Exception as e:
        return [{
            "level": LEVEL_INFO,
            "title": "内存状态读取失败",
            "text": f"无法读取内存状态（{e!r}），建议引擎暂不可用。请在 Windows 上运行本工具。",
        }]

    phys_pct = s["phys_pct"]
    commit_pct = s["commit_pct"]
    total_phys = s["total_phys"] or 1

    # ---- 1. 页面文件 / 虚拟内存（项目初心：commit 耗尽才是「内存不足」根因）----
    pagefile_total = max(s["total_commit"] - s["total_phys"], 0)
    pagefile_avail = max(s["avail_commit"] - s["avail_phys"], 0)
    if pagefile_avail / 1024 ** 3 < 1.0 and commit_pct > 80:
        items.append({
            "level": LEVEL_WARN,
            "title": "虚拟内存（页面文件）余量极低",
            "text": (f"可用提交仅剩 {gb(pagefile_avail)}，提交内存已达 {commit_pct:.0f}%，"
                     f"极易触发「内存不足」弹窗。建议开启系统托管页面文件，或在其它盘建立"
                     f"固定大小页面文件（见仓库 set_pagefile.ps1）。"),
        })
    elif pagefile_total / total_phys < 0.5 and commit_pct > 70:
        items.append({
            "level": LEVEL_TIP,
            "title": "页面文件偏小",
            "text": (f"页面文件约为物理内存的 {pagefile_total / total_phys * 100:.0f}%，"
                     f"提交内存上限偏低。若常遇「内存不足」，建议把页面文件设为系统托管"
                     f"或 ≥ 物理内存（{gb(total_phys)}）。"),
        })

    # ---- 2. 权限 ----
    if not is_admin():
        items.append({
            "level": LEVEL_WARN,
            "title": "未以管理员身份运行",
            "text": "自动清理不会执行（仅记录日志与提示）。请右键「以管理员身份运行」"
                    "，或使用「启动 MemGuard（管理员）.bat」。",
        })

    # ---- 3. 配置合理性 ----
    if not cfg.get("auto_clean"):
        items.append({
            "level": LEVEL_INFO,
            "title": "自动清理已关闭",
            "text": "超阈值时不会自动清理，仅记录日志与弹提醒；如需自动处理请勾选「自动清理」。",
            "action": {"label": "开启自动清理", "changes": {"auto_clean": True}},
        })
    if not cfg.get("warn_margin"):
        items.append({
            "level": LEVEL_TIP,
            "title": "预警未开启",
            "text": "warn_margin=0 表示关闭超阈前预警。开启（如 15）可在内存接近阈值时"
                    "提前弹气泡提醒，便于手动干预。",
            "action": {"label": "开启预警(15%)", "changes": {"warn_margin": 15}},
        })
    cooldown = cfg.get("cooldown", 300)
    if cooldown and cooldown <= 60:
        items.append({
            "level": LEVEL_TIP,
            "title": "清理冷却较短",
            "text": f"cooldown={cooldown}s 较短，内存反复边缘抖动时可能较频繁触发清理；"
                    f"可适当调大（如 300s）以减少打扰。",
            "action": {"label": "冷却调为 300s", "changes": {"cooldown": 300}},
        })
    if cfg.get("commit_threshold", 90) <= cfg.get("phys_threshold", 85):
        items.append({
            "level": LEVEL_TIP,
            "title": "阈值关系建议",
            "text": "提交阈值(commit_threshold)建议高于物理阈值(phys_threshold)：commit 上限含"
                    "页面文件，通常能比物理内存更高，二者倒挂会削弱监控意义。",
        })
    level = str(cfg.get("clean_level", "conservative")).strip().lower()
    if level == "aggressive" and cfg.get("phys_threshold", 85) >= 95:
        items.append({
            "level": LEVEL_WARN,
            "title": "激进档 + 高物理阈值",
            "text": "清理力度=激进 且 物理阈值≥95%，意味着要等到几乎满才触发、且一次性大量清空"
                    "工作集，前台易卡顿。建议改用「保守」或把阈值降到 85–92。",
        })

    # ---- 3b. 升档统计自调优（v1.5.1）：让实测统计反推配置，不必等用户自己悟 ----
    st = cfg.get("stats") or {}
    clean_cnt = int(st.get("count", 0) or 0)
    esc_cnt = int(st.get("escalated", 0) or 0)
    if frequent_escalation(cfg):
        items.append({
            "level": LEVEL_TIP,
            "title": "保守档频繁升档，建议直接改用激进",
            "text": (f"已累计清理 {clean_cnt} 次，其中 {esc_cnt} 次保守清理后仍未达标、"
                     f"自动补了一次激进清理（占 {esc_cnt * 100 // max(clean_cnt, 1)}%）。"
                     f"每次「先温和再彻底」等于多跑一遍、多打扰一次；建议把「清理力度」改为激进，"
                     f"一次清到位（建议窗口可一键应用）。"),
            "action": {"label": "改用激进档", "changes": {"clean_level": "aggressive"}},
        })
    prev_cnt = int(st.get("preventive", 0) or 0)
    if frequent_prevention(cfg):
        items.append({
            "level": LEVEL_TIP,
            "title": "预防式清理频繁触发，建议直接改用激进",
            "text": (f"已累计清理 {clean_cnt} 次，其中 {prev_cnt} 次是趋势预测到即将触阈、"
                     f"提前清理的（占 {prev_cnt * 100 // max(clean_cnt, 1)}%）。"
                     f"预防式清理只是把触发点往前挪，内存仍在持续上涨；建议把「清理力度」改为激进，"
                     f"一次清到位（建议窗口可一键应用）。"),
            "action": {"label": "改用激进档", "changes": {"clean_level": "aggressive"}},
        })
    srt_cnt = int(st.get("short_relief", 0) or 0)
    if frequent_short_relief(cfg):
        items.append({
            "level": LEVEL_TIP,
            "title": "保守清理效果不佳，建议直接改用激进",
            "text": (f"已累计清理 {clean_cnt} 次，其中 {srt_cnt} 次距上次自动清理不足 "
                     f"效果下限（效果偏短）——上次清完没多久内存又被顶回去。"
                     f"「先温和再彻底」在这台机器上只是拖延，建议把「清理力度」改为激进，"
                     f"一次清到位（建议窗口可一键应用）。"),
            "action": {"label": "改用激进档", "changes": {"clean_level": "aggressive"}},
        })
    dpn_cnt = int(st.get("deepen", 0) or 0)
    if frequent_deepen(cfg):
        items.append({
            "level": LEVEL_TIP,
            "title": "定向清理频繁加深，建议直接改用激进",
            "text": (f"已累计清理 {clean_cnt} 次，其中 {dpn_cnt} 次定向清理确实清到了东西、"
                     f"但压力没完全按住，于是把挑选网撒宽又清了一轮"
                     f"（占 {dpn_cnt * 100 // max(clean_cnt, 1)}%）。"
                     f"「先挑大户、再撒宽」在这台机器上每次都要走到第二轮；建议把「清理力度」"
                     f"改为激进，一次清到位（建议窗口可一键应用）。"),
            "action": {"label": "改用激进档", "changes": {"clean_level": "aggressive"}},
        })
    # ---- 3c. 反向建议（v1.10.0）：激进档杀鸡用牛刀，内存宽裕时劝退 ----
    if aggressive_not_needed(cfg, s):
        items.append({
            "level": LEVEL_TIP,
            "title": "激进档但内存长期宽裕，建议切回保守",
            "text": (f"当前物理 {phys_pct:.0f}% / 提交 {commit_pct:.0f}%，距阈值都有 "
                     f"{_AGGRESSIVE_SLACK} 个百分点以上的余量。激进档每次都要清空各进程"
                     f"工作集，前台程序下次访问得重新读盘、可能卡顿；内存长期宽裕时保守"
                     f"档已够用，建议切回保守（建议窗口可一键应用）。"),
            "action": {"label": "切回保守档", "changes": {"clean_level": "conservative"}},
        })
    if not cfg.get("escalate_clean", True) and phys_pct >= cfg.get("phys_threshold", 85):
        items.append({
            "level": LEVEL_TIP,
            "title": "自动升档已关闭但内存已超阈值",
            "text": ("当前物理占用已达阈值，却关闭了「清理未达标自动升档」，保守清理后不会再补激进清理。"
                     "若常出现刚清完又超阈值，建议开启（右键托盘 -> 清理未达标自动升档）。"),
            "action": {"label": "开启自动升档", "changes": {"escalate_clean": True}},
        })

    # ---- 4. 进程 / 白名单 ----
    if top is None:     # 调用方没给采样才现场扫描（要实时数据的场景才付这个代价）
        try:
            top = top_processes_list(15)
        except Exception:
            top = []
    if top:
        hi = max(1.5 * 1024 ** 3, s["total_phys"] * 0.12)
        big = [(n, r) for n, r, _ in top if r > hi]
        if big:
            names = "、".join(f"{n}（{gb(r)}）" for n, r in big[:3])
            items.append({
                "level": LEVEL_TIP,
                "title": "存在高内存占用进程",
                "text": (f"{names} 占用偏高。若是常驻后台服务且内存持续上涨，疑似内存泄漏，"
                         f"可尝试重启该进程；若需保留其工作集，把进程名加入 user_blacklist。"),
            })
        if level == "aggressive":
            bl = set(cfg.get("user_blacklist") or ())
            running = sorted({
                n for n, _, _ in top
                if _norm_proc_name(n) in _AGGRESSIVE_PRONE and _norm_proc_name(n) not in bl
            })
            if running:
                items.append({
                    "level": LEVEL_TIP,
                    "title": "激进档建议加白名单",
                    "text": ("检测到 " + "、".join(running[:4]) +
                             " 等程序在运行；激进档会清空其工作集导致卡顿，"
                             "建议把常用程序加入 user_blacklist。"),
                })

    # ---- 4b. 泄漏进程识别（v1.11.0）：按历史上升斜率点名疑似泄漏的进程 ----
    if leaks:
        names = "、".join(f"{n}（{gb(r)}）" for n, r, _ in leaks[:3])
        items.append({
            "level": LEVEL_TIP,
            "title": f"{len(leaks)} 个进程内存持续上涨（疑似泄漏）",
            "text": (f"{names} 的占用仍在持续走高。若是常驻后台服务或已关掉的窗口，"
                     f"重启该进程最直接；内存吃紧时 MemGuard 也会优先定向清理其工作集。"),
        })

    # ---- 5. 健康正向 ----
    if phys_pct < 55 and commit_pct < 70 and is_admin():
        items.append({
            "level": LEVEL_INFO,
            "title": "内存充裕",
            "text": f"当前物理 {phys_pct:.0f}% / 提交 {commit_pct:.0f}%，无需干预；"
                    f"MemGuard 以静默方式守护中。",
        })

    # 按等级排序（warn 优先），稳定排序保持同类原顺序
    items.sort(key=lambda it: _LEVEL_ORDER.get(it["level"], 9))
    return items


def format_advice(items: list) -> str:
    """把建议列表渲染为多行文本（供 MessageBox / 日志回退展示）。"""
    if not items:
        return "未发现明显可优化项，当前配置与内存状态良好。"
    tag = {LEVEL_WARN: "注意", LEVEL_TIP: "建议", LEVEL_INFO: "提示"}
    lines = []
    for it in items:
        lines.append(f"[{tag.get(it['level'], '·')}] {it['title']}")
        lines.append("  " + it["text"])
        lines.append("")
    return "\n".join(lines).strip()
