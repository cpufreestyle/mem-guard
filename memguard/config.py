# -*- coding: utf-8 -*-
"""
配置与日志：版本、路径、默认配置、配置校验、落盘日志。

只依赖标准库，被其它模块（winapi/clean/tray/cli）广泛引用，故保持零项目内依赖。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime

__version__ = "1.18.0"

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
    "target_top_adapt": True,        # 开：定向大户榜长期钉在同几个进程时，把单次大户数 +1 撒宽覆盖面（一次性）
    "target_top_adapt_done": False,  # 已自动加过大户数的闩：挡住重复加码，用户可手动改 target_clean_top
    # ---- 清理提前量自调优（v1.16.0）：效果连续偏短时把判定阈值提前几个百分点 ----
    "headroom_adapt": True,          # 开：效果连续偏短时把物理/提交阈值自动提前 5 个百分点（一次性）
    "headroom_adapt_done": False,    # 已自动提前过的闩：挡住重复加码，用户可手动改 target_headroom_pct
    # ---- 低内存绝对下限自调优（v1.17.0）：连番短效说明 min_avail_mb 这条线本身偏低 ----
    "min_avail_adapt": True,         # 开：低内存下限触发的清理连续偏短时，把可用内存下限抬高一级（一次性）
    "min_avail_adapt_done": False,   # 已自动抬高过低内存下限的闩：挡住重复加码，用户可手动改 min_avail_mb
    "debounce_sec": 0,       # 内存持续超阈值的宽限秒数(防抖)，0=立即触发
    # conservative=仅清 standby list/修改页/文件缓存（温和，对前台几乎无影响，默认）
    # aggressive  =额外清空各进程工作集（释放更多，但前台程序下次访问需重新读盘，可能卡顿）
    "clean_level": "conservative",
    "user_blacklist": [],    # 额外跳过清空工作集的进程名，如 ["chrome", "code.exe"]（不区分大小写）
    "warn_margin": 15,       # 距阈值还差多少个百分点时先弹预警(0=关闭预警)
    "advice_refresh_sec": 60,  # 后台刷新「优化建议条数」的最小间隔(秒)，越低越跟手但越费 CPU
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
    # ---- 累计统计（由 do_clean 维护并持久化）----
    "stats": {"count": 0, "freed": 0},
}

CLEAN_LEVELS = ("conservative", "aggressive")

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


def _clamp_int(value, low: int, high: int, default: int) -> int:
    """把配置值钳制到 [low, high]；非法值回落到默认值。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def normalize_config(raw) -> dict:
    """校验并钳制配置：越界/脏值不会让程序跑飞，未知键原样保留。"""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["phys_threshold"] = _clamp_int(cfg.get("phys_threshold"), 50, 99, DEFAULT_CONFIG["phys_threshold"])
    cfg["commit_threshold"] = _clamp_int(cfg.get("commit_threshold"), 50, 99, DEFAULT_CONFIG["commit_threshold"])
    cfg["interval"] = _clamp_int(cfg.get("interval"), 2, 3600, DEFAULT_CONFIG["interval"])
    cfg["cooldown"] = _clamp_int(cfg.get("cooldown"), 0, 86400, DEFAULT_CONFIG["cooldown"])
    cfg["debounce_sec"] = _clamp_int(cfg.get("debounce_sec"), 0, 3600, DEFAULT_CONFIG["debounce_sec"])
    cfg["warn_margin"] = _clamp_int(cfg.get("warn_margin"), 0, 50, DEFAULT_CONFIG["warn_margin"])
    cfg["advice_refresh_sec"] = _clamp_int(
        cfg.get("advice_refresh_sec"), 15, 600, DEFAULT_CONFIG["advice_refresh_sec"])
    cfg["auto_clean"] = bool(cfg.get("auto_clean", True))
    cfg["escalate_clean"] = bool(cfg.get("escalate_clean", True))
    cfg["target_clean"] = bool(cfg.get("target_clean", True))
    cfg["target_clean_min_mb"] = _clamp_int(
        cfg.get("target_clean_min_mb"), 128, 32768, DEFAULT_CONFIG["target_clean_min_mb"])
    cfg["target_clean_top"] = _clamp_int(
        cfg.get("target_clean_top"), 1, TARGET_CLEAN_TOP_MAX, DEFAULT_CONFIG["target_clean_top"])
    cfg["bg_trim"] = bool(cfg.get("bg_trim", True))
    cfg["predict_clean"] = bool(cfg.get("predict_clean", True))
    cfg["predict_window_min"] = _clamp_int(
        cfg.get("predict_window_min"), 1, 60, DEFAULT_CONFIG["predict_window_min"])
    cfg["auto_level_adapt"] = bool(cfg.get("auto_level_adapt", True))
    cfg["level_adapt_done"] = bool(cfg.get("level_adapt_done", False))
    cfg["effect_track"] = bool(cfg.get("effect_track", True))
    cfg["effect_min_relief_sec"] = _clamp_int(
        cfg.get("effect_min_relief_sec"), 60, 86400, DEFAULT_CONFIG["effect_min_relief_sec"])
    cfg["adaptive_cooldown"] = bool(cfg.get("adaptive_cooldown", True))
    cfg["adaptive_cooldown_floor"] = _clamp_int(
        cfg.get("adaptive_cooldown_floor"), 5, 3600,
        DEFAULT_CONFIG["adaptive_cooldown_floor"])
    cfg["sticky_aggressive"] = bool(cfg.get("sticky_aggressive", True))
    cfg["stage_learn"] = bool(cfg.get("stage_learn", True))
    cfg["stage_learn_strikes"] = _clamp_int(
        cfg.get("stage_learn_strikes"), 2, 10, DEFAULT_CONFIG["stage_learn_strikes"])
    cfg["stage_deepen"] = bool(cfg.get("stage_deepen", True))
    cfg["stage_deepen_rounds"] = _clamp_int(
        cfg.get("stage_deepen_rounds"), 1, 3, DEFAULT_CONFIG["stage_deepen_rounds"])
    cfg["target_headroom_pct"] = _clamp_int(
        cfg.get("target_headroom_pct"), 0, 20, DEFAULT_CONFIG["target_headroom_pct"])
    cfg["stage_deepen_diminish_pct"] = _clamp_int(
        cfg.get("stage_deepen_diminish_pct"), 0, 100,
        DEFAULT_CONFIG["stage_deepen_diminish_pct"])
    cfg["target_top_adapt"] = bool(cfg.get("target_top_adapt", True))
    cfg["target_top_adapt_done"] = bool(cfg.get("target_top_adapt_done", False))
    cfg["headroom_adapt"] = bool(cfg.get("headroom_adapt", True))
    cfg["headroom_adapt_done"] = bool(cfg.get("headroom_adapt_done", False))
    cfg["min_avail_adapt"] = bool(cfg.get("min_avail_adapt", True))
    cfg["min_avail_adapt_done"] = bool(cfg.get("min_avail_adapt_done", False))
    cfg["clean_on_start"] = bool(cfg.get("clean_on_start", False))
    lvl = str(cfg.get("clean_level", "conservative")).strip().lower()
    cfg["clean_level"] = lvl if lvl in CLEAN_LEVELS else "conservative"
    cfg["user_blacklist"] = sorted(_blacklist_stems(cfg.get("user_blacklist")))
    # 清理触发方式
    cfg["min_avail_mb"] = _clamp_int(cfg.get("min_avail_mb"), 0, 10 ** 7, 0)
    cfg["scheduled_minutes"] = _clamp_int(cfg.get("scheduled_minutes"), 0, 24 * 60, 0)
    # 清理区域开关：只接受已知键的布尔值，缺失的沿用默认
    raw_areas = cfg.get("clean_areas")
    raw_areas = raw_areas if isinstance(raw_areas, dict) else {}
    cfg["clean_areas"] = {
        k: bool(raw_areas.get(k, DEFAULT_CONFIG["clean_areas"][k])) for k in CLEAN_AREA_KEYS
    }
    # 自动更新：间隔钳制在 1 小时..1 周；两个开关容错为 bool；时间戳容忍脏值
    cfg["update_check_hours"] = _clamp_int(
        cfg.get("update_check_hours"), 1, 168, DEFAULT_CONFIG["update_check_hours"])
    cfg["auto_update"] = bool(cfg.get("auto_update", True))
    cfg["auto_install"] = bool(cfg.get("auto_install", False))
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
