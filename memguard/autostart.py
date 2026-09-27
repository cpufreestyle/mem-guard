# -*- coding: utf-8 -*-
"""
开机自启：以计划任务方式注册 / 查询 / 卸载（登录触发、最高权限、无 UAC 弹窗）。

只依赖标准库 + config；被 tray（菜单开关）与 diag（诊断导出）调用。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from .config import BASE_DIR

AUTOSTART_TASK = "MemGuard"
_CREATE_NO_WINDOW = 0x08000000


def _run_silent(cmd: list) -> bool:
    """静默运行外部命令（不弹控制台窗口），返回是否成功。"""
    try:
        # errors="replace"：中文系统下 schtasks 输出为 GBK，若按 Unicode 严格解码会在
        # subprocess 的 reader 线程里抛 UnicodeDecodeError——该异常不经过主线程的
        # except（线程内异常无法被外层捕获），只会在日志留下 traceback。此处只用
        # returncode，输出内容无需精确解析，故坏字节直接替换即可。
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
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


# 查询计划任务要启动 schtasks 子进程（几十毫秒）。托盘菜单每次展开都会读取勾选状态，
# 若每次真查会明显卡顿，故缓存结果，仅在安装/卸载成功后失效。
_autostart_cache: bool | None = None


def autostart_enabled(use_cache: bool = True) -> bool:
    """查询计划任务 MemGuard 是否已注册。

    use_cache=True（默认）复用上次结果，避免菜单反复展开时反复启动 schtasks 子进程；
    诊断导出等需要实时值时可传 False。
    """
    global _autostart_cache
    if use_cache and _autostart_cache is not None:
        return _autostart_cache
    _autostart_cache = _run_silent(["schtasks", "/Query", "/TN", AUTOSTART_TASK])
    return _autostart_cache


def _invalidate_autostart_cache() -> None:
    global _autostart_cache
    _autostart_cache = None


def install_autostart() -> bool:
    """注册登录自启计划任务（最高权限，无 UAC 弹窗）。

    打包成 exe 后直接把 exe 自身注册进去，不依赖 Python 与外部脚本；
    源码运行时优先复用 install_autostart.ps1，否则退回 schtasks + pythonw。
    """
    ok = _install_autostart_impl()
    if ok:
        _invalidate_autostart_cache()
    return ok


def _task_xml(exe: str) -> str:
    r"""生成登录自启的 schtasks XML：最高权限、Command 为完整 exe 路径。

    不走 schtasks /TR：它会把含空格的路径按第一个空格拆开（例如 "D:\ai share\...\mem_guard.exe"
    会被存成 Command=D:\ai + Arguments=share\...），任务开机根本起不来。XML 导入完整保留路径；
    这与 install_autostart.ps1 用 New-ScheduledTaskAction 的稳健做法一致。
    """
    safe = exe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo><Description>MemGuard autostart (logon, elevated)</Description></RegistrationInfo>\n'
        '  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>\n'
        '  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>'
        '<RunLevel>HighestAvailable</RunLevel></Principal></Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <Enabled>true</Enabled>\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
        '  </Settings>\n'
        '  <Actions Context="Author"><Exec><Command>' + safe + '</Command></Exec></Actions>\n'
        '</Task>\n'
    )


def _install_autostart_impl() -> bool:
    if getattr(sys, "frozen", False):
        # frozen 下 BASE_DIR 取自 exe 所在目录；用 exe 自身 XML 导入，完整保留含空格路径
        exe = os.path.abspath(sys.executable)
        fd, xml_file = tempfile.mkstemp(prefix="memguard_task_", suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as f:
                f.write(_task_xml(exe))
            return _run_silent(["schtasks", "/Create", "/TN", AUTOSTART_TASK,
                                "/XML", xml_file, "/F"])
        except Exception:
            return False
        finally:
            try:
                os.remove(xml_file)
            except OSError:
                pass
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
    ok = _remove_autostart_impl()
    if ok:
        _invalidate_autostart_cache()
    return ok


def _remove_autostart_impl() -> bool:
    ps1 = os.path.join(BASE_DIR, "install_autostart.ps1")
    if os.path.exists(ps1):
        return _run_silent(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", ps1, "-Mode", "uninstall"])
    return _run_silent(["schtasks", "/Delete", "/TN", AUTOSTART_TASK, "/F"])
