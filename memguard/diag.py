# -*- coding: utf-8 -*-
"""
诊断导出：把内存状态 / 进程 / 日志 / 配置打包为 zip，便于排障时提交。

依赖 config / winapi / clean / autostart；被 tray 菜单调用。
"""
from __future__ import annotations

import os
import zipfile
from datetime import datetime

from .autostart import autostart_enabled
from . import idle
from .adapters import summary_lines as adapter_summary
from .clean import top_processes_list
from .config import BASE_DIR, CONFIG_PATH, LOG_PATH, __version__, gb, load_config
from .privileges import CLEAN_PRIVILEGES
from .winapi import get_mem, is_admin, privilege_state


def export_diagnostics(status_lines=None) -> str:
    """打包内存状态/进程/日志/配置为 zip，返回路径；失败返回 'ERR:...'。"""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = os.path.join(BASE_DIR, f"diagnostics_{ts}.zip")
        s = get_mem()
        cfg = load_config()
        st = cfg.get("stats") or {}
        areas = cfg.get("clean_areas") or {}
        info = [
            f"version={__version__}",
            f"admin={is_admin()}",
            f"物理 {s['phys_pct']:.1f}%  ({gb(s['used_phys'])} / {gb(s['total_phys'])})",
            f"提交 {s['commit_pct']:.1f}%  (可用 {gb(s['avail_commit'])} / {gb(s['total_commit'])})",
            f"autostart={autostart_enabled(use_cache=False)}",
            f"stats: 清理 {int(st.get('count', 0))} 次 / 累计释放 {gb(int(st.get('freed', 0)))}",
            "clean_areas: " + " ".join(f"{k}={'on' if v else 'off'}" for k, v in areas.items()),
        ]
        for p in CLEAN_PRIVILEGES:
            info.append(f"priv {p}={privilege_state(p)}")
        # 自适应痕迹：自动调优到底动过哪几条、爬到第几级、开关是开是关。
        # 少了这一段，现场报告只能看到 config 里的一把散键和日志里的零散行，
        # 回答不了「这个阈值为什么被自动改了」。
        # 守护状态（v1.24.0）：全局热键当前键位（含被占用）/ 全屏让路 / 前台进程保护
        # / 用户空闲秒数。现场报告少了这一段，就只能看到配置里的一把散键。
        info.append("--- 守护状态 ---")
        lines = [str(x) for x in (status_lines or []) if str(x).strip()]
        info.extend(lines or ["（调用方未提供：托盘未运行时手动导出会缺这三行）"])
        idle_sec = idle.idle_seconds()
        info.append("用户空闲: " + ("未知" if idle_sec is None else f"{idle_sec:.0f} 秒")
                    + f"（notify_idle_sec={cfg.get('notify_idle_sec', 300)}）")
        info.append("--- 自适应决策痕迹 ---")
        info.extend(adapter_summary(cfg))
        info.append("--- 物理占用 Top20 ---")
        for name, rss, pid in top_processes_list(20):
            info.append(f"{name}  {rss / 1024 ** 3:.2f} GB  PID {pid}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("diagnostics.txt", "\n".join(info))
            if os.path.exists(CONFIG_PATH):
                z.write(CONFIG_PATH, "mem_guard.json")
            if os.path.exists(LOG_PATH):
                z.write(LOG_PATH, "mem_guard.log")
        return zip_path
    except Exception as e:
        return f"ERR:{e}"
