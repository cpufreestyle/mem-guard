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
from .clean import top_processes_list
from .config import BASE_DIR, CONFIG_PATH, LOG_PATH, __version__, gb
from .privileges import CLEAN_PRIVILEGES
from .winapi import get_mem, is_admin, privilege_state


def export_diagnostics() -> str:
    """打包内存状态/进程/日志/配置为 zip，返回路径；失败返回 'ERR:...'。"""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = os.path.join(BASE_DIR, f"diagnostics_{ts}.zip")
        s = get_mem()
        info = [
            f"version={__version__}",
            f"admin={is_admin()}",
            f"物理 {s['phys_pct']:.1f}%  ({gb(s['used_phys'])} / {gb(s['total_phys'])})",
            f"提交 {s['commit_pct']:.1f}%  (可用 {gb(s['avail_commit'])} / {gb(s['total_commit'])})",
            f"autostart={autostart_enabled()}",
        ]
        for p in CLEAN_PRIVILEGES:
            info.append(f"priv {p}={privilege_state(p)}")
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
