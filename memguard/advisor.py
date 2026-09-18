# -*- coding: utf-8 -*-
"""
优化建议引擎：基于实时内存状态与配置，给出可操作的「实用建议」。

只依赖标准库 + config / winapi / clean，被 tray（菜单展示）与 cli（自检）调用；
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


def analyze(cfg: dict, mem: dict | None = None) -> list:
    """返回建议列表，每条为 {"level", "title", "text"}。

    mem 可外部传入（便于测试或复用实时采样）；省略时现场调用 get_mem()。
    任何单条建议计算失败都不应阻断其它建议，故逐段 try 容错。
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
        })
    if not cfg.get("warn_margin"):
        items.append({
            "level": LEVEL_TIP,
            "title": "预警未开启",
            "text": "warn_margin=0 表示关闭超阈前预警。开启（如 15）可在内存接近阈值时"
                    "提前弹气泡提醒，便于手动干预。",
        })
    cooldown = cfg.get("cooldown", 300)
    if cooldown and cooldown <= 60:
        items.append({
            "level": LEVEL_TIP,
            "title": "清理冷却较短",
            "text": f"cooldown={cooldown}s 较短，内存反复边缘抖动时可能较频繁触发清理；"
                    f"可适当调大（如 300s）以减少打扰。",
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

    # ---- 4. 进程 / 白名单 ----
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
