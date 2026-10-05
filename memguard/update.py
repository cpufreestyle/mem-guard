# -*- coding: utf-8 -*-
"""
自动更新：查询 GitHub 最新 Release、比较版本，并按需下载静默安装 / 自替换。

只依赖标准库（hashlib / urllib / subprocess / os / sys / re / tempfile）+ config，
托盘后台静默检查、菜单「立即更新」、CLI（--check-update / --update）共用本模块。

最小打扰原则：
- 检查失败、已是最新版本：只写日志，不弹任何窗；
- 发现新版本：每个版本只发一条托盘气泡（update_notified_tag 去重）；
- 真正下载安装只发生在用户点「立即更新」，或配置开了「下载后自动安装」。

完整性原则：
- Release 随包发布 sha256 清单（checksums.txt，由 CI 生成后一起上传）；
- 下载完先比对 sha256，不通过就删包中止——绝不让未校验的二进制进引导批处理；
- 便携自替换前留一份旧 exe 备份，新版本起不来就自动还原旧版本。
"""
from __future__ import annotations

import hashlib
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

# Release 资产里的 sha256 清单：checksums.txt / sha256sums.txt / checksums.sha256 都认
CHECKSUMS_ASSET_RE = re.compile(
    r"^(checksums?|sha256sums?|sha256)\.(txt|sha256|sums?)$", re.IGNORECASE)

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

    覆盖前先把当前 exe 留一份 .bak，新版本 6 秒内没起来就把旧版本还原回去：
    宁可退回老代码，也不让用户面对一个双击打不开的程序。
    """
    image = os.path.basename(target_path)
    bak = target_path + ".bak"
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "new={0}"\r\n'
        'set "target={1}"\r\n'
        'set "bak={2}"\r\n'
        'set "image={3}"\r\n'
        ":wait\r\n"
        'tasklist /fi "PID eq {4}" 2>nul | find /i "%image%" >nul\r\n'
        "if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait)\r\n"
        'if exist "%target%" copy /y "%target%" "%bak%" >nul\r\n'
        'move /y "%new%" "%target%" >nul\r\n'
        'start "" "%target%"\r\n'
        ":: 等新版本起来；起不来就把旧版本还原回去\r\n"
        "timeout /t 6 /nobreak >nul\r\n"
        'tasklist /fi "IMAGENAME eq %image%" 2>nul | find /i "%image%" >nul\r\n'
        "if errorlevel 1 (\r\n"
        '  if exist "%bak%" (move /y "%bak%" "%target%" >nul & start "" "%target%")\r\n'
        ") else (\r\n"
        '  if exist "%bak%" del "%bak%"\r\n'
        ")\r\n"
        'del "%~f0"\r\n'
    ).format(new_path, target_path, bak, image, pid)


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


# ---------------------------------------------------------------- sha256 校验

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# 「sha256:<hex>」这种带算法前缀的写法，OCI 镜像清单和一部分打包工具用它
_SHA256_TAG_RE = re.compile(r"^sha256[:=]([0-9a-fA-F]{64})$")
_CERTUTIL_RE = re.compile(
    r"^\s*sha256\s*\((.+?)\)\s*=\s*([0-9a-fA-F]{64})\s*$", re.IGNORECASE)


def _clean_token(tok: str) -> str:
    """清单里的文件名 token：去掉引号、二进制模式前缀 * 与前后空白。"""
    return str(tok or "").strip().strip('"').strip("'").lstrip("*")


def sha256_of(path: str, chunk: int = 1 << 20) -> str:
    """流式计算文件 sha256（小写十六进制）；读不到文件返回 ''。

    流式是为了不把几十 MB 的安装包一次性读进内存——更新链路跑在托盘后台
    线程里，一次大块分配会让它自己成为被清理的对象。
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
    except OSError:
        return ""
    return h.hexdigest()


def find_checksums_asset(assets):
    """从 Release 资产里挑 sha256 清单；没有则返回 None。"""
    for a in assets or ():
        name = str(a.get("name") or "")
        url = str(a.get("browser_download_url") or "")
        if CHECKSUMS_ASSET_RE.match(name) and url:
            return {"name": name, "url": url}
    return None


def parse_checksums(text: str, name: str):
    """从 sha256 清单文本里取出指定资产的期望摘要；取不到返回 None。

    兼容三种常见写法，都是 CI 或用户手工核对时会遇到的：
      "<hex>  <name>"        coreutils sha256sum（CI 用这种）
      "<hex> *<name>"        二进制模式，PowerShell 的 Get-FileHash 常配这种
      "SHA256(<name>)= <hex>" Windows certutil -hashfile
    """
    want = _clean_token(name).lower()
    if not want:
        return None
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _CERTUTIL_RE.match(line)
        if m and _clean_token(m.group(1)).lower() == want:
            return m.group(2).lower()
        hexes, names = [], []
        for tok in line.split():
            t = _clean_token(tok).lower()
            if _HEX64_RE.match(t):
                hexes.append(t)
            elif _SHA256_TAG_RE.match(t):
                hexes.append(_SHA256_TAG_RE.match(t).group(1).lower())
            else:
                names.append(t)
        if hexes and want in names:
            return hexes[0]
    return None


def fetch_checksums(url: str, timeout: int = 30) -> dict:
    """下载 Release 上的 sha256 清单（几 KB 文本）。"""
    req = urllib.request.Request(url, headers={"User-Agent": f"MemGuard/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "text": resp.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "msg": f"下载 sha256 清单失败：{e}"}


def verify_asset(path: str, expected: str) -> dict:
    """比对下载文件的 sha256。

    expected 为空表示无从校验（Release 没给摘要）：这时返回 skipped=True，
    由调用方决定是硬失败还是放行——自动更新默认硬失败。
    """
    want = str(expected or "").strip().lower()
    if not want:
        return {"ok": False, "skipped": True, "msg": "Release 未提供 sha256 摘要"}
    got = sha256_of(path)
    if not got:
        return {"ok": False, "msg": "已下载的文件读取不到，无法校验"}
    if got != want:
        return {"ok": False, "msg": "sha256 校验不通过（文件与 Release 清单不一致）"}
    return {"ok": True, "sha256": got}


def purge_stale_backup() -> None:
    """删掉上次更新遗留的旧版备份。

    便携自替换成功后 .bak 已无用，留着只会在安装目录里多一个大型可执行文件，
    既让用户困惑也容易被安全软件单独报毒。脏启动路径（批处理没跑完就被杀）
    留下的残留同样在这里清掉。
    """
    exe = _frozen_exe()
    if not exe:
        return
    try:
        bak = exe + ".bak"
        if os.path.exists(bak):
            os.remove(bak)
    except OSError:
        pass


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



def _verify_download(asset: dict, assets, path: str) -> dict:
    """下载后的完整性校验：取清单 → 找摘要 → 比对，任何一步不通都算失败。
    """
    cks = find_checksums_asset(assets)
    if not cks:
        return {"ok": False,
                "msg": "Release 缺少 sha256 清单，已取消安装（可在配置里关掉 sha256 校验）"}
    fetched = fetch_checksums(cks["url"])
    if not fetched.get("ok"):
        return {"ok": False, "msg": fetched.get("msg", "下载 sha256 清单失败")}
    expected = parse_checksums(fetched.get("text"), asset.get("name"))
    if not expected:
        return {"ok": False,
                "msg": f"sha256 清单里没有 {asset.get('name')} 的摘要，已取消安装"}
    v = verify_asset(path, expected)
    if not v.get("ok"):
        return {"ok": False, "msg": v.get("msg", "sha256 校验不通过")}
    return v

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
    # 完整性校验：自动更新链路上唯一一道「装进机器的确实是作者发的那个包」的
    # 关卡。默认硬失败——Release 一定带 sha256 清单（CI 生成并一起上传），
    # 缺清单 / 摘要对不上 / 清单里没这个文件，一律在动手安装前中止。
    if bool((cfg or {}).get("verify_update_sha256", True)):
        v = _verify_download(asset, r.get("assets"), new_path)
        if not v.get("ok"):
            # 校验不过的包当场删掉：引导批处理下一步就要 move 它，留着就是个
            # 未校验的可执行文件躺在临时目录里。
            try:
                if os.path.exists(new_path):
                    os.remove(new_path)
            except OSError:
                pass
            log(f"更新包 sha256 校验失败：{v.get('msg', '')}")
            return {"ok": False, "action": "none", "msg": v.get("msg", "sha256 校验不通过")}
        log(f"更新包 sha256 校验通过（{str(v.get('sha256', ''))[:12]}…）")
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
