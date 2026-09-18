# -*- coding: utf-8 -*-
"""
配置与日志：版本、路径、默认配置、配置校验、落盘日志。

只依赖标准库，被其它模块（winapi/clean/tray/cli）广泛引用，故保持零项目内依赖。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

__version__ = "1.4.0"

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
    "debounce_sec": 0,       # 内存持续超阈值的宽限秒数(防抖)，0=立即触发
    # conservative=仅清 standby list/修改页/文件缓存（温和，对前台几乎无影响，默认）
    # aggressive  =额外清空各进程工作集（释放更多，但前台程序下次访问需重新读盘，可能卡顿）
    "clean_level": "conservative",
    "user_blacklist": [],    # 额外跳过清空工作集的进程名，如 ["chrome", "code.exe"]（不区分大小写）
    "warn_margin": 15,       # 距阈值还差多少个百分点时先弹预警(0=关闭预警)
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
    # ---- 累计统计（由 do_clean 维护并持久化）----
    "stats": {"count": 0, "freed": 0},
}

CLEAN_LEVELS = ("conservative", "aggressive")

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
    cfg["auto_clean"] = bool(cfg.get("auto_clean", True))
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
    # 累计统计：非负整数
    raw_stats = cfg.get("stats")
    raw_stats = raw_stats if isinstance(raw_stats, dict) else {}
    cfg["stats"] = {
        "count": _clamp_int(raw_stats.get("count"), 0, 10 ** 9, 0),
        "freed": _clamp_int(raw_stats.get("freed"), 0, 10 ** 18, 0),
    }
    return cfg


def load_config() -> dict:
    raw = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
    except Exception as e:
        log(f"配置读取失败，改用默认配置: {e}")
    return normalize_config(raw)


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(normalize_config(cfg), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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
