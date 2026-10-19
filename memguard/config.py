# -*- coding: utf-8 -*-
"""
配置与日志：版本、路径、默认配置、配置校验、落盘日志。

只依赖标准库与 memguard.adapters（自适应适配器注册表），被其它模块广泛引用。
"""
from __future__ import annotations

import fnmatch

import json
import os
import sys
import threading
from datetime import datetime

from .adapters import ADAPTERS, COUNTER, clamp_bounds

__version__ = "1.24.0"

# GitHub 仓库（owner/repo），供托盘「检查更新」查询最新 Release
REPO_SLUG = "cpufreestyle/mem-guard"

# 打包成 exe 时（PyInstaller --onefile），__file__ 指向临时解压目录，
# 日志/配置应落在 exe 真正所在目录，故 frozen 时改用 sys.executable 的目录；
# 源码运行时（python mem_guard.py）用入口脚本所在目录，与历史行为一致。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(BASE_DIR, "mem_guard.json")
LOG_PATH = os.path.join(BASE_DIR, "mem_guard.log")

DEFAULT_CONFIG = {
    "phys_threshold": 85,    # 物理内存使用率阈值(%)
    "commit_threshold": 90,  # 提交内存使用率阈值(%)，这是"内存不足"的真正指标
    "interval": 10,          # 检测间隔(秒)
    "cooldown": 300,         # 两次自动清理之间的冷却时间(秒)
    "auto_clean": True,      # 是否开启自动清理
    "escalate_clean": True,  # 保守清理后仍超阈值、或仍低于可用内存下限时，自动补一次激进清理（最多一次、带开关）
    # ---- 定向清理内存大户（v1.7.0）：仍超阈值时先只按工作集挑最大的几个进程精确清 ----
    "target_clean": True,     # 开：仍超阈值时先精确清大户口，比直接全量升档打扰小；仍不达标才升档
    "target_clean_min_mb": 1024,  # 工作集达到该值(MB) 的进程才算「内存大户」
    "target_clean_top": 3,     # 单次最多精确清空前 N 个工作集最大的进程
    # ---- 后台进程工作集清理（v1.11.0）：定向仍不达标时，清空没有可见窗口的后台进程 ----
    "bg_trim": True,          # 开：仍超阈值时清空后台进程工作集，跳过有可见窗口的，前台零感知
    # ---- 趋势预防式清理（v1.8.0）：按历史采样拟合上升斜率，预判即将触阈就提前清 ----
    "predict_clean": True,     # 开：预计 predict_window_min 分钟内触及阈值，就提前清一次（仍走保守→定向→升档阶梯）
    "predict_window_min": 5,   # 预计触阈时间窗口(分钟)：只有窗口内会触阈才预防，越小平稳期越不打扰
    # ---- 档位自调优（v1.6.0）：保守档频繁升档说明「先温和再彻底」白跑一遍，自动改激进 ----
    "auto_level_adapt": True,  # 累计清理中 escalated 过半且≥3 次时，自动把清理力度切为激进（一次性）
    "level_adapt_done": False, # 已自动切过激进的闩：挡住重复覆盖，用户手动切回保守也不再自动评估
    # ---- 清理效果闭环（v1.9.0）：度量「距上次自动清理多久」，短效=没 hold 住 ----
    "effect_track": True,     # 开：自动清理时度量与上次自动清理的间隔，偏短则累计 short_relief 并提示
    "effect_min_relief_sec": 600,  # 与上次自动清理间隔小于该秒数视为「效果偏短」（清理压力很快复发）
    # ---- 自适应冷却（v1.10.0）：短效复发时允许更快复查，连续 hold 住后恢复用户值 ----
    "adaptive_cooldown": True,   # 开：短效复发后把下次最小间隔压到 max(cooldown*factor, floor)，连续达标恢复 cooldown
    "adaptive_cooldown_floor": 30,  # 压缩后的最小间隔下限(秒)，clamp 5..3600；压缩只减不增，永不高于用户 cooldown
    # ---- 持续压力粘滞激进 + 阶梯阶段自学习（v1.12.0）：高压不再每轮从保守档重跑 ----
    "sticky_aggressive": True,  # 开：上一轮走完整条阶梯仍没压住，下一轮自动清理直接按激进档执行
    "stage_learn": True,        # 开：某阶梯阶段连续多次没释放出东西就跳过它，省一轮时间
    "stage_learn_strikes": 3,   # 连续几次该阶段释放量为 0 才跳过，clamp 2..10
    # ---- 定向加深（v1.13.0）：定向清理释放到了东西但压力没完全按住时，把网撒宽一轮再清 ----
    "stage_deepen": True,       # 开：定向清理有效但压力仍在时，把大户数翻倍、大户下限减半再清一轮；仍不达标才升档
    # ---- 温和阶梯用尽（v1.14.0）：加深可多轮 + 降压余量，把升档前的温和手段用到极限 ----
    "stage_deepen_rounds": 1,   # 定向加深最多撒宽几轮：每轮候选数再翻倍、大户下限再减半（仍只挑没清过的进程），clamp 1..3
    "target_headroom_pct": 0,   # 降压余量(百分点)：判定「仍受压」时物理/提交阈值先让出这么多，清到留有余量才算按住；clamp 0..20，0=关
    # ---- 加深收益自适应 + 定向覆盖面自调优（v1.15.0）：还要不要继续，交给度量回答 ----
    "stage_deepen_diminish_pct": 50,  # 加深单轮释放不足上轮这么多(%)就收尾：边际收益衰减，别为撒宽而撒宽；clamp 0..100，0=关
    "target_top_adapt": True,        # 开：定向大户榜长期钉在同几个进程时，把单次大户数 +1 撒宽覆盖面
    "target_top_adapt_done": 0,      # 已自动加过大户数的次数（0=从未）：仍窄就继续加到顶（爬山），到顶即停
    # ---- 清理提前量自调优（v1.16.0）：效果连续偏短时把判定阈值提前几个百分点 ----
    "headroom_adapt": True,          # 开：效果连续偏短时把物理/提交阈值自动提前 5 个百分点（一次性）
    "headroom_adapt_done": False,    # 已自动提前过的闩：挡住重复加码，用户可手动改 target_headroom_pct
    # ---- 低内存绝对下限自调优（v1.17.0）：连番短效说明 min_avail_mb 这条线本身偏低 ----
    "min_avail_adapt": True,         # 开：低内存下限触发的清理连续偏短时，把可用内存下限抬高一级（一次性）
    "min_avail_adapt_done": False,   # 已自动抬高过低内存下限的闩：挡住重复加码，用户可手动改 min_avail_mb
    # ---- 定向大户下限自调优（v1.19.0）：榜上有人却够不着下限，说明这条线本身偏高 ----
    "min_mb_adapt": True,          # 开：长期在榜的进程够不着 target_clean_min_mb 时，把大户下限减半（一次性）
    "min_mb_adapt_done": False,    # 已自动降过大户下限的闩：挡住重复降码，用户可手动改 target_clean_min_mb
    # ---- 定向加深轮数自调优（v1.20.0）：加深被轮数卡住时把上限 +1（可爬到 clean._DEEPEN_MAX_ROUNDS）----
    "deepen_rounds_adapt": True,   # 开：加深把 stage_deepen_rounds 跑满仍在释放且仍受压时，轮数上限 +1
    "deepen_rounds_adapt_done": 0, # 已自动爬到的轮数（0=从未）：支持 1→2→3 逐级爬坡，到顶即停
    # ---- 预防窗口自调优（v1.21.0）：预防式清理没防住压力时，把预测窗口放宽一档 ----
    "predict_window_adapt": True,  # 开：预防式清理后仍很快复发（没防住）时，把 predict_window_min 加宽
    "predict_window_adapt_done": 0, # 已自动爬到的窗口分钟数（0=从未）：可逐级加到 _PREDICT_WINDOW_MAX，到顶即停
    # ---- 触发阈值自适应（v1.22.0）：温和阶梯用尽仍效果偏短时，把两条触发线各降一档 ----
    "threshold_adapt": True,  # 开：超阈值触发的清理连续偏短且温和手段用尽时，phys/commit 阈值各降一级
    "threshold_adapt_done": 0, # 已自动下调的次数（0=从未）：最多下调两次，到兜底下限即停
    "debounce_sec": 0,       # 内存持续超阈值的宽限秒数(防抖)，0=立即触发
    # conservative=仅清 standby list/修改页/文件缓存（温和，对前台几乎无影响，默认）
    # aggressive  =额外清空各进程工作集（释放更多，但前台程序下次访问需重新读盘，可能卡顿）
    "clean_level": "conservative",
    "user_blacklist": [],    # 额外跳过清空工作集的进程名，如 ["chrome", "code.exe"]（不区分大小写）
    "warn_margin": 15,       # 距阈值还差多少个百分点时先弹预警(0=关闭预警)
    "advice_refresh_sec": 60,  # 后台刷新「优化建议条数」的最小间隔(秒)，越低越跟手但越费 CPU
    # ---- 人不在就不打扰（v1.24.0）：超过这个空闲秒数的通知只记账，回来时汇总成一条 ----
    "notify_idle_sec": 300,
    # ---- 清理触发方式（对标 Mem Reduct / WinMemoryCleaner）----
    "min_avail_mb": 0,       # 可用物理内存低于该值(MB)也触发清理；0=关闭
    "scheduled_minutes": 0,  # 每隔 N 分钟主动清理一次（不看内存占用）；0=关闭
    "clean_on_start": False, # 启动后先清理一次
    # ---- 清理区域开关（对标 WinMemoryCleaner 的勾选项）----
    "clean_areas": {
        "standby": True,              # 清理 standby 列表
        "low_priority_standby": True, # 优先清理 standby 中的低优先级部分（更温和）
        "modified": True,             # 刷写 modified page 列表
        "file_cache": True,           # 清空系统文件缓存工作集
        "working_sets": True,         # 清空系统工作集（仅激进档生效）
    },
    # ---- 自动更新（v1.5.0；最小打扰：只后台检查+每版本一条气泡，安装需显式开）----
    "auto_update": True,        # 后台静默检查 GitHub Release
    "update_check_hours": 12,    # 检查间隔(小时)，钳制 1..168
    "auto_install": False,      # 发现新版自动下载并静默安装（完成后自动重启）
    "last_update_check": 0,     # 上次检查时间戳(epoch 秒)，程序维护
    "update_notified_tag": "",  # 已气泡提醒过的版本 tag，去重
    "verify_update_sha256": True,  # 下载更新包后先比对 Release 的 sha256 清单；默认开，关掉就不再校验
    # ---- 全局热键（v1.24.0，对标 Mem Reduct / ISLC：一键清理不必点右键菜单）----
    "hotkey_enabled": True,    # 总开关；把某个键位留空即单独关掉那一条
    "hotkey_clean": "ctrl+alt+m",     # 立即清理一次
    "hotkey_overview": "ctrl+alt+o",  # 打开内存概览窗口
    "hotkey_auto": "ctrl+alt+p",      # 切换自动清理开关
    # ---- 前台 / 全屏让路（v1.24.0，对标 ISLC 用户的「游戏不卡」诉求）----
    "fs_pause": True,    # 检测到全屏应用时暂停自动清理（手动清理不受限）
    "fs_protect": True,  # 前台进程不被清空工作集（激进档也跳过它）
    # ---- 累计统计（由 do_clean 维护并持久化）----
    "stats": {"count": 0, "freed": 0},
}

CLEAN_LEVELS = ("conservative", "aggressive")

# 全局热键动作名 → 默认键位（小写规范形 "ctrl+alt+m"，解析见 hotkeys.parse_hotkey）
HOTKEY_DEFAULTS = {
    "clean": "ctrl+alt+m",
    "overview": "ctrl+alt+o",
    "auto": "ctrl+alt+p",
}

# 单次定向清理最多精确清空的进程数（target_clean_top 的钳制上限）
TARGET_CLEAN_TOP_MAX = 10

# 清理区域开关的合法键（菜单与配置校验共用）
CLEAN_AREA_KEYS = ("standby", "low_priority_standby", "modified", "file_cache", "working_sets")

# 不对其做 EmptyWorkingSet 的进程，清空这些进程的工作集会导致系统不稳定或界面闪烁
CLEAN_BLACKLIST = {
    "system", "system idle process", "idle", "registry", "memory compression",
    "csrss", "smss", "wininit", "winlogon", "services", "lsass", "lsaiso",
    "dwm", "fontdrvhost", "sihost", "ctfmon",
}


def _norm_proc_name(name: str) -> str:
    """进程名归一化：小写并去掉 .exe 后缀，便于黑名单双向匹配。

    注意 psutil 在 Windows 返回的名字带 .exe（如 csrss.exe），
    而 CLEAN_BLACKLIST 里写的是裸名，必须归一化后才能匹配上。
    """
    n = (name or "").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def _blacklist_stems(items) -> set:
    """把黑名单（进程名列表，可带可不带 .exe）统一为无后缀小写名集合。"""
    out = set()
    for it in items or ():
        if isinstance(it, str):
            s = _norm_proc_name(it)
            if s:
                out.add(s)
    return out


CLEAN_BLACKLIST_STEMS = _blacklist_stems(CLEAN_BLACKLIST)


def name_in_blacklist(name, stems) -> bool:
    """进程名是否命中黑名单；命中支持 * / ? 通配符（对标 Mem Reduct 的排除列表）。

    通配符单独挑出来 fnmatch：普通名字仍走集合精确匹配（热路径上多一个 any 都不必），
    带 * / ? 的条目才逐条比对。白名单条目同样经 _blacklist_stems 归一（去 .exe、小写），
    所以 "*CHROME*" 与 "chrome" 两种写法都对 chrome.exe 生效。
    """
    n = _norm_proc_name(name)
    if n in stems:
        return True
    return any(("*" in s or "?" in s) and fnmatch.fnmatchcase(n, s) for s in stems)

def _clamp_int(value, low: int, high: int, default: int) -> int:
    """把配置值钳制到 [low, high]；非法值回落到默认值。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


# ---- 声明式钳制表：normalize_config 只 iterate 这两张表和 adapters 注册表 ----
# 新增一个「越界回落」的整数旋钮时只加一行，不用再在 normalize_config 里铺一段。
_FIELD_SPECS = (
    # (键, 下限, 上限)；默认值一律从 DEFAULT_CONFIG 取，写错区间也有默认值兜底
    ("phys_threshold", 50, 99),
    ("commit_threshold", 50, 99),
    ("interval", 2, 3600),
    ("cooldown", 0, 86400),
    ("debounce_sec", 0, 3600),
    ("warn_margin", 0, 50),
    ("advice_refresh_sec", 15, 600),
    ("notify_idle_sec", 0, 86400),
    ("target_clean_min_mb", 128, 32768),
    ("target_clean_top", 1, TARGET_CLEAN_TOP_MAX),
    ("predict_window_min", 1, 60),
    ("effect_min_relief_sec", 60, 86400),
    ("adaptive_cooldown_floor", 5, 3600),
    ("stage_learn_strikes", 2, 10),
    ("stage_deepen_rounds", 1, 3),
    ("target_headroom_pct", 0, 20),
    ("stage_deepen_diminish_pct", 0, 100),
    ("min_avail_mb", 0, 10 ** 7),
    ("scheduled_minutes", 0, 24 * 60),
    ("update_check_hours", 1, 168),
)


# 纯布尔开关：没有区间可言，脏值（None / 字符串 / 1）一律按 bool 语义归一。
# 自适应的开关键不在这里——它们跟着 ADAPTERS 一起走，见 normalize_config。
_BOOL_KEYS = (
    "auto_clean",
    "escalate_clean",
    "target_clean",
    "bg_trim",
    "predict_clean",
    "effect_track",
    "adaptive_cooldown",
    "sticky_aggressive",
    "stage_learn",
    "stage_deepen",
    "clean_on_start",
    "auto_update",
    "auto_install",
    "verify_update_sha256",
    "hotkey_enabled",
    "fs_pause",
    "fs_protect",
)


def normalize_config(raw) -> dict:
    """校验并钳制配置：越界/脏值不会让程序跑飞，未知键原样保留。"""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)
    # ---- 标量字段：钳制区间集中声明，越界 / 脏值一律回落默认值 ----
    for key, low, high in _FIELD_SPECS:
        cfg[key] = _clamp_int(cfg.get(key), low, high, DEFAULT_CONFIG[key])
    # ---- 布尔开关：老配置没有这个键时按默认开算，脏值按 bool 语义归一 ----
    for key in _BOOL_KEYS:
        cfg[key] = bool(cfg.get(key, DEFAULT_CONFIG[key]))
    # ---- 自适应闩：区间与复位口径全部由 adapters.ADAPTERS 推导 ----
    # 计数型闩（爬山：0=从未，到 cap 即停）钳 0..cap；一次性闩只取布尔值。
    # 老配置里 target_top_adapt_done 存的是 bool，_clamp_int 天然兼容
    # （True→1、False→0），不需要额外的迁移代码。
    for a in ADAPTERS:
        cfg[a.key] = bool(cfg.get(a.key, True))
        low, high, default = clamp_bounds(a)
        if a.kind == COUNTER:
            cfg[a.latch] = _clamp_int(cfg.get(a.latch), low, high, default)
        else:
            cfg[a.latch] = bool(cfg.get(a.latch, default))
    lvl = str(cfg.get("clean_level", "conservative")).strip().lower()
    cfg["clean_level"] = lvl if lvl in CLEAN_LEVELS else "conservative"
    cfg["user_blacklist"] = sorted(_blacklist_stems(cfg.get("user_blacklist")))
    # 清理区域开关：只接受已知键的布尔值，缺失的沿用默认
    raw_areas = cfg.get("clean_areas")
    raw_areas = raw_areas if isinstance(raw_areas, dict) else {}
    cfg["clean_areas"] = {
        k: bool(raw_areas.get(k, DEFAULT_CONFIG["clean_areas"][k])) for k in CLEAN_AREA_KEYS
    }
    # 全局热键键位：统一成小写规范形；显式空串保留（用户就是要关掉这一条）。
    # 合不合法不在配置层判断（键位解析在 hotkeys，config 只依赖 adapters），
    # 真到注册时由 hotkeys.action_specs 逐条校验并跳过，绝不静默乱注册。
    for _name in HOTKEY_DEFAULTS:
        _key = "hotkey_" + _name
        cfg[_key] = "+".join(p for p in (
            t.strip().lower() for t in str(cfg.get(_key, "") or "").split("+")) if p)
    # 自动更新的时间戳与已提醒 tag：间隔和开关已在 _FIELD_SPECS / _BOOL_KEYS 里钳过
    try:
        cfg["last_update_check"] = max(0.0, float(cfg.get("last_update_check") or 0))
    except (TypeError, ValueError):
        cfg["last_update_check"] = 0.0
    cfg["update_notified_tag"] = str(cfg.get("update_notified_tag") or "")
    # 累计统计：非负整数
    raw_stats = cfg.get("stats")
    raw_stats = raw_stats if isinstance(raw_stats, dict) else {}
    cfg["stats"] = {
        "count": _clamp_int(raw_stats.get("count"), 0, 10 ** 9, 0),
        "freed": _clamp_int(raw_stats.get("freed"), 0, 10 ** 18, 0),
    }
    # escalated（自动升档次数）只在非 0 时落键：从没升档过的配置保持原两键形状；
    # 而一旦升过档，这个计数必须活过 normalize，否则每次加载都被剥掉、永远显示 0
    if raw_stats.get("escalated"):
        cfg["stats"]["escalated"] = _clamp_int(raw_stats.get("escalated"), 0, 10 ** 9, 0)
    # targeted（定向清理次数）同样只在非 0 时落键：没定向过的配置保持原两键形状；
    # 而一旦定向过，这个计数必须活过 normalize，否则统计行永远显示 0
    if raw_stats.get("targeted"):
        cfg["stats"]["targeted"] = _clamp_int(raw_stats.get("targeted"), 0, 10 ** 9, 0)
    # preventive（趋势预防式清理次数）口径同上：非 0 才落键，没预防过的配置保持原形状
    if raw_stats.get("preventive"):
        cfg["stats"]["preventive"] = _clamp_int(raw_stats.get("preventive"), 0, 10 ** 9, 0)
    # short_relief（清理效果偏短次数）口径同上：距上次自动清理不足 effect_min_relief_sec
    # 说明上次清理没 hold 住、内存压力很快复发，非 0 才落键保持干净配置原形状
    if raw_stats.get("short_relief"):
        cfg["stats"]["short_relief"] = _clamp_int(raw_stats.get("short_relief"), 0, 10 ** 9, 0)
    # bg_trimmed（后台进程工作集清理次数）口径同上：非 0 才落键，没 trim 过的配置保持原形状
    if raw_stats.get("bg_trimmed"):
        cfg["stats"]["bg_trimmed"] = _clamp_int(raw_stats.get("bg_trimmed"), 0, 10 ** 9, 0)
    # sticky（持续压力粘滞激进次数）口径同上：非 0 才落键，没粘滞过的配置保持原形状
    if raw_stats.get("sticky"):
        cfg["stats"]["sticky"] = _clamp_int(raw_stats.get("sticky"), 0, 10 ** 9, 0)
    # deepen（定向加深次数）口径同上：非 0 才落键，没加深过的配置保持原形状
    if raw_stats.get("deepen"):
        cfg["stats"]["deepen"] = _clamp_int(raw_stats.get("deepen"), 0, 10 ** 9, 0)
    # low_mem（低内存下限触发次数）口径同上：非 0 才落键，没被绝对下限叫醒过的配置保持原形状
    if raw_stats.get("low_mem"):
        cfg["stats"]["low_mem"] = _clamp_int(raw_stats.get("low_mem"), 0, 10 ** 9, 0)
    # deepen_capped（加深被轮数上限卡住次数）口径同上：非 0 才落键，没被卡住的配置保持原形状
    if raw_stats.get("deepen_capped"):
        cfg["stats"]["deepen_capped"] = _clamp_int(raw_stats.get("deepen_capped"), 0, 10 ** 9, 0)
    # preventive_missed（预防式清理没防住次数）口径同上：v1.21.0 起预防式清理后压力
    # 仍很快复发（距上次清理比当时预测的 eta 还短）就记一次，作为放宽预测窗口的证据
    if raw_stats.get("preventive_missed"):
        cfg["stats"]["preventive_missed"] = _clamp_int(
            raw_stats.get("preventive_missed"), 0, 10 ** 9, 0)
    # fg_protected（v1.24.0 前台进程被保护的清理次数）口径同上：非 0 才落键
    if raw_stats.get("fg_protected"):
        cfg["stats"]["fg_protected"] = _clamp_int(
            raw_stats.get("fg_protected"), 0, 10 ** 9, 0)
    return cfg


def load_config() -> dict:
    raw = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                raw = json.load(f)
    except Exception as e:
        log(f"配置读取失败，改用默认配置: {e}")
    return normalize_config(raw)


# 配置写路径的锁：写盘是「读当前文件 - 改 - 写回」，菜单勾选、清理统计都会写，
# 不串行的话两个线程各拿一份旧配置互相覆盖（例：刚勾选的开关被一次自动清理写回旧值）。
# 用可重入锁：update_config 持锁后再调 save_config 不会自我死锁。
_CONFIG_LOCK = threading.RLock()


def save_config(cfg: dict) -> None:
    """把配置写盘（会经过 _CONFIG_LOCK，保证同一时刻只有一份写）。"""
    with _CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(normalize_config(cfg), f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def update_config(changes: dict) -> dict:
    """以磁盘当前配置为基准叠加变更并写回，返回合并后的新配置。

    所有配置写盘都应走这里而不是 save_config(内存里的字典)：内存副本可能是热重载前
    的，直接写回会覆盖用户手改的字段；读-改-写全程持锁也避免与并发写盘互相抵消。
    """
    with _CONFIG_LOCK:
        # 归一化后再返回/落盘：调用方（菜单）会把它直接赋回 guard.cfg，
        # 若带着越界值，勾选态与启动时的钳制口径就会不一致。
        cfg = normalize_config({**load_config(), **changes})
        save_config(cfg)
        return cfg


LOG_MAX_BYTES = 1 * 1024 * 1024  # 日志超过 1MB 时轮转为 mem_guard.log.1


def _rotate_log_if_needed() -> None:
    """日志超过上限时归档为 mem_guard.log.1（只保留一个备份）。"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            bak = LOG_PATH + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(LOG_PATH, bak)
    except Exception:
        pass


# 轮转检查（stat 文件大小）不必每次写日志都做，按写入次数节流即可；
# 最多多写几十行（几 KB）才触发轮转，上限仍有保障。
_LOG_ROTATE_EVERY = 64
_log_write_count = 0


def log(msg: str) -> None:
    global _log_write_count
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    _log_write_count += 1
    if _log_write_count % _LOG_ROTATE_EVERY == 1:
        _rotate_log_if_needed()
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f}GB"
