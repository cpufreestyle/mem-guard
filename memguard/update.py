# -*- coding: utf-8 -*-
"""
自动更新：查询 GitHub 最新 Release、比较版本，并按需下载静默安装 / 自替换。

只依赖标准库（urllib / subprocess / os / sys / re / tempfile）+ config，
托盘后台静默检查、菜单「立即更新」、CLI（--check-update / --update）共用本模块。

最小打扰原则：
- 检查失败、已是最新版本：只写日志，不弹任何窗；
- 发现新版本：每个版本只发一条托盘气泡（update_notified_tag 去重）；
- 真正下载安装只发生在用户点「立即更新」，或配置开了「下载后自动安装」。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .config import REPO_SLUG, __version__, log, update_config

# Release 资产命名，与 .github/workflows/release.yml 的上传清单保持一致：
# mem_guard.exe（便携单文件）与 MemGuard-Setup-x.y.z.exe（Inno 安装器）
SETUP_ASSET_RE = re.compile(r"^MemGuard-Setup-.*\.exe$", re.IGNORECASE)
PORTABLE_ASSET_RE = re.compile(r"^mem_guard\.exe$", re.IGNORECASE)

# 后台静默检查间隔的兜底值（小时）；实际以配置 update_check_hours 为准
DEFAULT_CHECK_HOURS = 12

# 下载超时（秒）：Release 资产走 GitHub CDN，弱网给宽一点
DOWNLOAD_TIMEOUT = 300


def _parse_version(v: str) -> tuple:
    """把 'v1.3.0' / '1.3.0' 解析为可比较的整数元组（非数字后缀按 0 处理）。"""
    nums = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums) if nums else (0,)


def _frozen_exe():
    """PyInstaller 单文件运行时返回当前 exe 绝对路径；源码运行返回 None。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return None


def pick_asset(assets, prefer: str = "setup"):
    """从 Release 资产里挑安装包：默认优先安装器，没有才退回便携单文件。

    assets 是 GitHub API 返回的资产列表（dict，含 name / browser_download_url）。
    """
    patterns = ((SETUP_ASSET_RE, PORTABLE_ASSET_RE) if prefer == "setup"
                else (PORTABLE_ASSET_RE, SETUP_ASSET_RE))
    for pat in patterns:
        for a in assets or ():
            name = str(a.get("name") or "")
            url = str(a.get("browser_download_url") or "")
            if pat.match(name) and url:
                return {"name": name, "url": url}
    return None


def is_installed_layout() -> bool:
    """当前 exe 是否处于安装器部署的目录（Inno 总在安装目录留一个 unins000.exe）。"""
    exe = _frozen_exe()
    if not exe:
        return False
    return os.path.exists(os.path.join(os.path.dirname(exe), "unins000.exe"))


def portable_boot_cmd(pid: int, new_path: str, target_path: str) -> str:
    """便携单文件的自替换引导批处理：等本进程退出 → 覆盖 → 重启。

    做成纯函数是因为命令串里嵌着 PID 与路径，拼错就是「更新完程序消失」，
    这种错误必须在测试里直接断言，不能等用户反馈。窗口以 CREATE_NO_WINDOW
    启动，全程无黑窗。
    """
    image = os.path.basename(target_path)
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "new={0}"\r\n'
        'set "target={1}"\r\n'
        ":wait\r\n"
        'tasklist /fi "PID eq {2}" 2>nul | find /i "{3}" >nul\r\n'
        "if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait)\r\n"
        'move /y "%new%" "%target%" >nul\r\n'
        'start "" "%target%"\r\n'
        'del "%~f0"\r\n'
    ).format(new_path, target_path, pid, image)


def setup_boot_cmd(pid: int, setup_path: str, app_dir: str) -> str:
    """安装器升级的引导批处理：等本进程退出 → 静默 Setup → 重启新程序。

    Setup 的 [Run] 带 skipifsilent，静默安装不会自己拉起程序，这里补一次 start。
    """
    exe = os.path.join(app_dir, "mem_guard.exe")
    image = os.path.basename(exe)
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "setup={0}"\r\n'
        'set "exe={1}"\r\n'
        ":wait\r\n"
        'tasklist /fi "PID eq {2}" 2>nul | find /i "{3}" >nul\r\n'
        "if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait)\r\n"
        '"{0}" /VERYSILENT /SP- /NORESTART\r\n'
        'start "" "{1}"\r\n'
        'del "%~f0"\r\n'
    ).format(setup_path, exe, pid, image)


def download_asset(url: str, dest: str, timeout: int = DOWNLOAD_TIMEOUT) -> dict:
    """把 Release 资产流式下载到 dest。返回 {"ok": bool, "msg"?}。"""
    req = urllib.request.Request(url, headers={"User-Agent": f"MemGuard/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as e:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        return {"ok": False, "msg": f"下载失败：{e}"}
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        return {"ok": False, "msg": "下载结果为空"}
    return {"ok": True}


def due_for_check(cfg: dict, now=None) -> bool:
    """后台静默检查的节流判断：开关打开且距上次检查超过设定间隔才返回 True。"""
    cfg = cfg or {}
    if not cfg.get("auto_update", True):
        return False
    try:
        hours = float(cfg.get("update_check_hours", DEFAULT_CHECK_HOURS) or 0)
    except (TypeError, ValueError):
        # 脏值不能把后台检查悄悄关成「永远不查」：回落默认间隔
        hours = DEFAULT_CHECK_HOURS
    if hours <= 0:
        return False
    try:
        last = float(cfg.get("last_update_check", 0) or 0)
    except (TypeError, ValueError):
        last = 0.0
    return (time.time() if now is None else now) - last >= hours * 3600


def fetch_latest_release(timeout: int = 8) -> dict:
    """查询 GitHub 最新 Release。

    返回 {"ok": True, "tag", "url", "newer", "assets"} 或 {"ok": False, "msg"}。
    仅用标准库 urllib，PyInstaller 打包后无需额外依赖。
    """
    url = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"MemGuard/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "msg": "该仓库暂无 Release（或不可访问）"}
        return {"ok": False, "msg": f"检查更新失败：HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "msg": f"检查更新失败：{e}"}

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return {"ok": False, "msg": "最新 Release 缺少版本号"}
    assets = [
        {"name": str(a.get("name") or ""),
         "url": str(a.get("browser_download_url") or "")}
        for a in (data.get("assets") or []) if isinstance(a, dict)
    ]
    return {
        "ok": True,
        "tag": tag,
        "url": data.get("html_url") or f"https://github.com/{REPO_SLUG}/releases",
        "newer": _parse_version(tag) > _parse_version(__version__),
        "assets": assets,
    }


def record_check(now=None) -> None:
    """把「已检查」时间落盘：重启后不会立刻再查（弱网与 API 限流时更重要）。"""
    ts = round(float(time.time() if now is None else now), 3)
    try:
        update_config({"last_update_check": ts})
    except Exception as e:
        log(f"记录更新检查时间失败: {e!r}")


def _set_notified(tag: str) -> None:
    """记住已经气泡提醒过的版本，避免同一版本反复提醒。"""
    try:
        update_config({"update_notified_tag": tag})
    except Exception:
        pass


def _open_url(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def check_and_notify(cfg: dict, notify=None, open_page: bool = False,
                     on_ready=None, now=None) -> dict:
    """检查一次更新并按结果安静处理，返回摘要 dict（供 CLI 与测试断言）。

    - 失败 / 已最新：只写日志，不打扰；
    - 有新版本：发一条气泡（每版本一次）；配了「下载后自动安装」则直接走安装；
    - open_page：仅用户显式点「检查更新」时才附赠打开发布页。
    """
    record_check(now)
    r = fetch_latest_release()
    if not r.get("ok"):
        log(r.get("msg", "检查更新失败"))
        return {"ok": False, "action": "none", "msg": r.get("msg", "检查更新失败")}
    if not r["newer"]:
        log(f"已是最新版本 v{__version__}")
        _set_notified("")
        return {"ok": True, "tag": r["tag"], "newer": False, "action": "none",
                "msg": f"已是最新版本（v{__version__}）"}

    tag = r["tag"]
    log(f"发现新版本 {tag}（当前 v{__version__}）")
    if (cfg or {}).get("auto_install"):
        if notify:
            try:
                notify(f"正在下载并安装 {tag}，完成后自动重启", "MemGuard 更新")
            except Exception:
                pass
        return install_latest(cfg, on_ready=on_ready, release=r)
    if str((cfg or {}).get("update_notified_tag") or "") != tag:
        _set_notified(tag)
        if notify:
            try:
                notify(f"发现新版本 {tag}（当前 v{__version__}）\n"
                       f"右键托盘 → 立即更新到最新版", "MemGuard 更新")
            except Exception:
                pass
        if open_page:
            _open_url(r["url"])
    return {"ok": True, "tag": tag, "newer": True, "action": "notify",
            "msg": f"发现新版本 {tag}"}


def install_latest(cfg: dict, on_ready=None, release: dict = None) -> dict:
    """下载最新版并静默安装 / 自替换。成功时进程被引导批处理重启，不再返回。

    on_ready 在引导批处理启动前回调：调用方在这里停托盘、收图标，保证目标 exe
    不被占用。任何失败都在动手前返回 {"ok": False, ...}，托盘可继续正常运行。
    """
    exe = _frozen_exe()
    if not exe:
        _open_url(f"https://github.com/{REPO_SLUG}/releases/latest")
        return {"ok": False, "action": "open_page",
                "msg": "源码运行模式无法自更新，已打开发布页"}
    r = release or fetch_latest_release()
    if not r.get("ok"):
        return {"ok": False, "action": "none", "msg": r.get("msg", "检查更新失败")}
    if not r.get("newer"):
        return {"ok": False, "action": "none",
                "msg": f"已是最新版本（v{__version__}）"}
    asset = pick_asset(r.get("assets"))
    if not asset:
        _open_url(r["url"])
        return {"ok": False, "action": "open_page",
                "msg": "Release 里没有可用安装包，已打开发布页"}

    tag = str(r["tag"])
    safe_tag = re.sub(r"[^0-9A-Za-z._-]", "_", tag)
    tmp_dir = tempfile.gettempdir()
    new_path = os.path.join(tmp_dir, f"mem_guard_update_{safe_tag}.exe")
    d = download_asset(asset["url"], new_path)
    if not d.get("ok"):
        return {"ok": False, "action": "none", "msg": d.get("msg", "下载失败")}
    log(f"更新包下载完成（{asset['name']}），准备切换到 {tag}")

    pid = os.getpid()
    if is_installed_layout():
        boot_cmd = setup_boot_cmd(pid, new_path, os.path.dirname(exe))
    else:
        boot_cmd = portable_boot_cmd(pid, new_path, exe)
    boot_path = os.path.join(tmp_dir, f"mem_guard_update_boot_{pid}.cmd")
    try:
        # cmd.exe 按 ANSI 代码页读批处理，mbcs 才能与中文路径兼容；非 Windows 退回 utf-8
        try:
            with open(boot_path, "w", encoding="mbcs", newline="") as f:
                f.write(boot_cmd)
        except (LookupError, OSError):
            with open(boot_path, "w", encoding="utf-8", newline="") as f:
                f.write(boot_cmd)
    except Exception as e:
        return {"ok": False, "action": "none", "msg": f"写入更新脚本失败：{e}"}

    try:
        subprocess.Popen(
            ["cmd", "/c", boot_path],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    except Exception as e:
        return {"ok": False, "action": "none", "msg": f"启动更新脚本失败：{e}"}
    if on_ready:
        try:
            on_ready()
        except Exception:
            pass
    # 引导批处理在等本进程退出；留半秒让托盘把图标收干净
    time.sleep(0.5)
    log(f"MemGuard 退出以完成到 {tag} 的更新")
    os._exit(0)
