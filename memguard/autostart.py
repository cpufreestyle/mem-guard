# -*- coding: utf-8 -*-
"""
开机自启：以计划任务方式注册 / 查询 / 卸载（登录触发、最高权限、无 UAC 弹窗）。

只依赖标准库 + config；被 tray（菜单开关）与 diag（诊断导出）调用。
"""
from __future__ import annotations

import os
import subprocess
import sys

from .config import BASE_DIR

AUTOSTART_TASK = "MemGuard"
_CREATE_NO_WINDOW = 0x08000000


def _run_silent(cmd: list) -> bool:
    """静默运行外部命令（不弹控制台窗口），返回是否成功。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=_CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def _pythonw_path():
    import shutil
    for cand in ("pythonw.exe", "python.exe"):
        p = shutil.which(cand)
        if p:
            return p
    return None


def autostart_enabled() -> bool:
    """查询计划任务 MemGuard 是否已注册。"""
    return _run_silent(["schtasks", "/Query", "/TN", AUTOSTART_TASK])


def install_autostart() -> bool:
    """注册登录自启计划任务（最高权限，无 UAC 弹窗）。

    打包成 exe 后直接把 exe 自身注册进去，不依赖 Python 与外部脚本；
    源码运行时优先复用 install_autostart.ps1，否则退回 schtasks + pythonw。
    """
    if getattr(sys, "frozen", False):
        # frozen 下 BASE_DIR 取自 exe 所在目录，无需设置任务的工作目录
        exe = os.path.abspath(sys.executable)
        return _run_silent(["schtasks", "/Create", "/TN", AUTOSTART_TASK,
                            "/TR", f'"{exe}"', "/SC", "ONLOGON",
                            "/RL", "HIGHEST", "/F"])
    ps1 = os.path.join(BASE_DIR, "install_autostart.ps1")
    if os.path.exists(ps1):
        return _run_silent(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", ps1, "-Mode", "install"])
    pyw = _pythonw_path()
    if not pyw:
        return False
    tr = f'"{pyw}" "{os.path.join(BASE_DIR, "mem_guard.py")}"'
    return _run_silent(["schtasks", "/Create", "/TN", AUTOSTART_TASK, "/TR", tr,
                        "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"])


def remove_autostart() -> bool:
    """删除开机自启计划任务。"""
    ps1 = os.path.join(BASE_DIR, "install_autostart.ps1")
    if os.path.exists(ps1):
        return _run_silent(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", ps1, "-Mode", "uninstall"])
    return _run_silent(["schtasks", "/Delete", "/TN", AUTOSTART_TASK, "/F"])
