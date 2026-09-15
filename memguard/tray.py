# -*- coding: utf-8 -*-
"""
托盘编排：右键菜单、开机自启（计划任务）、诊断导出、更新检查，以及主循环 Guard。

界面细节（图标绘制、Top10 / 趋势 / 优化建议窗口）见 ui.py；
自身不实现清理，只编排调用。依赖 config / winapi / clean / advisor / ui。
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime

import pystray

from .config import (
    BASE_DIR,
    CONFIG_PATH,
    LOG_PATH,
    REPO_SLUG,
    __version__,
    gb,
    load_config,
    log,
    save_config,
)
from .advisor import analyze
from .clean import CLEAN_PRIVILEGES, do_clean, top_processes_list
from .ui import (
    make_icon,
    message_box,
    show_advice_window,
    show_top_window,
    show_trend_window,
)
from .winapi import get_mem, is_admin, privilege_state

# ---------------------------------------------------------------- 开机自启（计划任务）

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

# ---------------------------------------------------------------- 诊断导出

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

# ---------------------------------------------------------------- 更新检查

def _parse_version(v: str) -> tuple:
    """把 'v1.3.0' / '1.3.0' 解析为可比较的整数元组（非数字后缀按 0 处理）。"""
    nums = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums) if nums else (0,)

def fetch_latest_release(timeout: int = 8) -> dict:
    """查询 GitHub 最新 Release。

    返回 {"ok": True, "tag", "url", "newer"} 或 {"ok": False, "msg"}。
    仅用标准库 urllib，PyInstaller 打包后无需额外依赖。
    """
    import urllib.error
    import urllib.request

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
    return {
        "ok": True,
        "tag": tag,
        "url": data.get("html_url") or f"https://github.com/{REPO_SLUG}/releases",
        "newer": _parse_version(tag) > _parse_version(__version__),
    }

# ---------------------------------------------------------------- 主程序

class Guard:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.state = get_mem()
        self.last_clean = 0.0
        self.over_since = None       # 内存超阈值起始时刻（防抖用）
        self.history = collections.deque(maxlen=120)  # 内存使用率采样历史
        self._cfg_mtime = None       # 配置热重载：记录上次 mtime
        self._warned = False         # 是否已就本次接近阈值发过预警（避免反复弹）
        self.stop = threading.Event()
        self.icon: pystray.Icon | None = None

    # -- 菜单回调 ------------------------------------------------

    def on_clean_now(self, icon=None, item=None) -> None:
        r = do_clean("手动")
        if not r["ok"]:
            icon.notify(r["msg"], "MemGuard")
            return
        freed = max(r["freed"], 0)
        top3 = top_processes_list(3)
        top_txt = "\n".join(f"  {n} {rss / 1024 ** 3:.2f}GB" for n, rss, _ in top3)
        icon.notify(
            f"释放 {gb(freed)}  可用物理 {gb(r['after']['avail_phys'])}\n"
            f"当前占用 Top3:\n{top_txt}",
            "MemGuard 清理完成",
        )
        self.refresh()

    def on_top(self, icon=None, item=None) -> None:
        show_top_window()

    def on_toggle_auto(self, icon, item) -> None:
        self.cfg["auto_clean"] = not self.cfg["auto_clean"]
        save_config(self.cfg)
        log(f"自动清理 -> {'开启' if self.cfg['auto_clean'] else '关闭'}")

    def on_open_log(self, icon=None, item=None) -> None:
        try:
            os.startfile(LOG_PATH)
        except Exception:
            pass

    def on_quit(self, icon, item) -> None:
        self.stop.set()
        icon.stop()

    @staticmethod
    def _preset(cfg, phys, commit):
        def setter(icon, item):
            cfg["phys_threshold"] = phys
            cfg["commit_threshold"] = commit
            save_config(cfg)
        return setter

    @staticmethod
    def _set_cooldown(cfg, minutes):
        def setter(icon, item):
            cfg["cooldown"] = minutes * 60
            save_config(cfg)
        return setter

    @staticmethod
    def _set_level(cfg, level):
        def setter(icon, item):
            cfg["clean_level"] = level
            save_config(cfg)
            log(f"清理档位 -> {'激进' if level == 'aggressive' else '保守'}")
        return setter

    def on_toggle_autostart(self, icon, item) -> None:
        if autostart_enabled():
            ok = remove_autostart()
            log(f"取消开机自启 -> {'成功' if ok else '失败'}")
            icon.notify("已取消开机自启" if ok else "取消失败", "MemGuard")
        else:
            ok = install_autostart()
            log(f"设置开机自启 -> {'成功' if ok else '失败'}")
            icon.notify("已设置开机自启（登录时自动启动）" if ok
                        else "设置失败（需管理员权限）", "MemGuard")

    def on_trend(self, icon=None, item=None) -> None:
        show_trend_window(lambda: list(self.history))

    def on_advice(self, icon=None, item=None) -> None:
        show_advice_window(self.cfg)

    def on_export(self, icon=None, item=None) -> None:
        path = export_diagnostics()
        if path.startswith("ERR:"):
            icon.notify("诊断导出失败：" + path, "MemGuard")
        else:
            icon.notify("诊断已导出：\n" + path, "MemGuard 诊断")

    def on_check_update(self, icon=None, item=None) -> None:
        """后台线程查询 GitHub Release，避免联网阻塞托盘菜单。"""
        def worker() -> None:
            r = fetch_latest_release()
            if not r.get("ok"):
                log(r.get("msg", "检查更新失败"))
                if self.icon:
                    try:
                        self.icon.notify(r.get("msg", "检查更新失败"), "MemGuard 更新")
                    except Exception:
                        pass
                return
            if r["newer"]:
                log(f"发现新版本 {r['tag']}（当前 v{__version__}）")
                if self.icon:
                    try:
                        self.icon.notify(
                            f"发现新版本 {r['tag']}（当前 v{__version__}）\n正在打开下载页…",
                            "MemGuard 更新",
                        )
                    except Exception:
                        pass
                try:
                    import webbrowser
                    webbrowser.open(r["url"])
                except Exception:
                    pass
            else:
                log(f"已是最新版本 v{__version__}")
                if self.icon:
                    try:
                        self.icon.notify(f"已是最新版本（v{__version__}）", "MemGuard 更新")
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def build_menu(self) -> pystray.Menu:
        cfg = self.cfg

        def line_phys(_):
            s = self.state
            return f"物理内存  {s['phys_pct']:.0f}%   ({gb(s['used_phys'])} / {gb(s['total_phys'])})"

        def line_commit(_):
            s = self.state
            return f"提交内存  {s['commit_pct']:.0f}%   (可用 {gb(s['avail_commit'])})"

        def line_tip(_):
            return "↑ 内存不足报错看这一行"

        def preset_menu():
            return pystray.Menu(
                pystray.MenuItem(
                    "激进  物理75% / 提交85%",
                    self._preset(cfg, 75, 85), radio=True,
                    checked=lambda i: cfg["phys_threshold"] == 75,
                ),
                pystray.MenuItem(
                    "标准  物理85% / 提交90%",
                    self._preset(cfg, 85, 90), radio=True,
                    checked=lambda i: cfg["phys_threshold"] == 85,
                ),
                pystray.MenuItem(
                    "宽松  物理92% / 提交95%",
                    self._preset(cfg, 92, 95), radio=True,
                    checked=lambda i: cfg["phys_threshold"] == 92,
                ),
            )

        def cooldown_menu():
            choices = [(1, "1 分钟"), (5, "5 分钟"), (10, "10 分钟"), (30, "30 分钟")]
            return pystray.Menu(*[
                pystray.MenuItem(
                    label,
                    self._set_cooldown(cfg, m), radio=True,
                    checked=(lambda i, mm=m: cfg["cooldown"] == mm * 60),
                ) for m, label in choices
            ])

        def level_menu():
            return pystray.Menu(
                pystray.MenuItem(
                    "保守  只清缓存(最温和)",
                    self._set_level(cfg, "conservative"), radio=True,
                    checked=lambda i: cfg.get("clean_level") == "conservative",
                ),
                pystray.MenuItem(
                    "激进  额外清空进程工作集",
                    self._set_level(cfg, "aggressive"), radio=True,
                    checked=lambda i: cfg.get("clean_level") == "aggressive",
                ),
            )

        return pystray.Menu(
            pystray.MenuItem(line_phys, None, enabled=False),
            pystray.MenuItem(line_commit, None, enabled=False),
            pystray.MenuItem(line_tip, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即清理", self.on_clean_now),
            pystray.MenuItem("内存占用 Top10", self.on_top),
            pystray.MenuItem("内存趋势", self.on_trend),
            pystray.MenuItem(lambda i: f"优化建议（{len(analyze(self.cfg))} 条）",
                             self.on_advice),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("自动清理", self.on_toggle_auto,
                             checked=lambda i: cfg["auto_clean"]),
            pystray.MenuItem("清理阈值", preset_menu()),
            pystray.MenuItem("清理力度", level_menu()),
            pystray.MenuItem("清理冷却", cooldown_menu()),
            pystray.MenuItem("开机自启", self.on_toggle_autostart,
                             checked=lambda i: autostart_enabled()),
            pystray.MenuItem(
                lambda i: "权限：管理员（可清理）" if is_admin() else "⚠ 非管理员，无法清理",
                None, enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开日志", self.on_open_log),
            pystray.MenuItem("导出诊断", self.on_export),
            pystray.MenuItem(f"检查更新（v{__version__}）", self.on_check_update),
            pystray.MenuItem("退出", self.on_quit),
        )

    # -- 循环 ----------------------------------------------------

    def refresh(self) -> None:
        self.state = get_mem()

    def maybe_reload_config(self) -> None:
        """检查配置文件 mtime，变化则热重载（无需重启托盘）。"""
        try:
            mtime = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else None
            if mtime != self._cfg_mtime:
                self._cfg_mtime = mtime
                if mtime is not None:
                    self.cfg = load_config()
                    log("配置已热重载（来自 mem_guard.json）")
        except Exception as e:
            log(f"配置重载检查失败: {e}")

    def monitor(self) -> None:
        self.maybe_reload_config()
        while not self.stop.is_set():
            try:
                self.maybe_reload_config()
                self.refresh()
                s = self.state
                self.history.append((time.time(), s["phys_pct"], s["commit_pct"]))
                if self.icon:
                    # 托盘重建/退出瞬间 icon 可能短暂失效，单独容错避免整轮监控被中断
                    try:
                        self.icon.icon = make_icon(s["phys_pct"], self.cfg["phys_threshold"])
                        self.icon.title = (
                            f"MemGuard  物理 {s['phys_pct']:.0f}% / 提交 {s['commit_pct']:.0f}%\n"
                            f"可用物理 {gb(s['avail_phys'])}    可用提交 {gb(s['avail_commit'])}"
                        )
                    except Exception:
                        pass
                now = time.time()
                over = (s["phys_pct"] >= self.cfg["phys_threshold"]
                        or s["commit_pct"] >= self.cfg["commit_threshold"])
                # -- 接近阈值预警：只提醒不清理，让人有提前手动干预的机会 --
                margin = self.cfg.get("warn_margin", 0)
                if margin and not over and s["phys_pct"] >= self.cfg["phys_threshold"] - margin:
                    if not self._warned:
                        self._warned = True
                        log(f"预警 | 物理 {s['phys_pct']:.0f}% 接近阈值 {self.cfg['phys_threshold']}%")
                        if self.icon:
                            try:
                                self.icon.notify(
                                    f"物理内存 {s['phys_pct']:.0f}%，接近阈值 {self.cfg['phys_threshold']}%\n"
                                    f"可右键托盘手动清理",
                                    "MemGuard 内存预警",
                                )
                            except Exception:
                                pass
                else:
                    self._warned = False
                if over:
                    if self.over_since is None:
                        self.over_since = now
                    debounced = (now - self.over_since) >= self.cfg.get("debounce_sec", 0)
                    due = (self.cfg["auto_clean"] and debounced
                           and now - self.last_clean > self.cfg["cooldown"])
                    if due:
                        self.last_clean = now
                        self.over_since = None
                        r = do_clean("自动")
                        if r["ok"] and self.icon:
                            top3 = top_processes_list(3)
                            top_txt = "\n".join(f"  {n} {rss / 1024 ** 3:.2f}GB" for n, rss, _ in top3)
                            lvl_txt = "激进" if r.get("level") == "aggressive" else "保守"
                            try:
                                self.icon.notify(
                                    f"物理 {s['phys_pct']:.0f}% / 提交 {s['commit_pct']:.0f}% 超阈值\n"
                                    f"已释放 {gb(max(r['freed'], 0))}（{lvl_txt}档）\n"
                                    f"当前占用 Top3:\n{top_txt}",
                                    "MemGuard 自动清理",
                                )
                            except Exception:
                                pass
                else:
                    self.over_since = None
            except Exception as e:
                log(f"监控异常: {e}")
            self.stop.wait(self.cfg["interval"])

    def run(self) -> None:
        s = get_mem()
        log(f"MemGuard v{__version__} 启动 | 管理员={is_admin()} | 物理 {s['phys_pct']:.0f}% | "
            f"提交 {s['commit_pct']:.0f}% | 档位={self.cfg.get('clean_level')}")
        if not is_admin():
            log("提示：非管理员运行，自动清理不会生效，请右键以管理员身份运行")
        # 监控线程只启动一次；托盘图标在循环内可被重建，实现"托盘自愈"
        threading.Thread(target=self.monitor, daemon=True).start()

        while not self.stop.is_set():
            try:
                self.icon = pystray.Icon(
                    "MemGuard", make_icon(self.state["phys_pct"], self.cfg["phys_threshold"]),
                    "MemGuard 运行中", self.build_menu(),
                )
                self.icon.run()
            except Exception as e:
                log(f"托盘异常退出: {e!r}")
            if self.stop.is_set():
                break
            # 非用户退出而 icon.run() 意外返回/抛错（Explorer 重启、后端异常）时重建图标，
            # 避免出现"程序还在跑、托盘图标却不见了"的假死状态
            log("托盘未正常退出，1 秒后重建图标（自愈）")
            time.sleep(1.0)
        log("MemGuard 已退出")
