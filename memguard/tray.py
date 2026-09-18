# -*- coding: utf-8 -*-
"""
托盘主循环 Guard：运行状态、配置热重载、监控循环与图标自愈。

菜单树与菜单回调见 menu.py；界面见 ui.py；
自启 / 诊断 / 更新检查分别见 autostart.py / diag.py / update.py。
自身不实现清理，只编排调用。
"""
from __future__ import annotations

import collections
import os
import threading
import time

import pystray

from .advisor import analyze
from .clean import do_clean, top_processes_list
from .config import CONFIG_PATH, __version__, gb, load_config, log
from .menu import build_menu
from .ui import make_icon
from .winapi import get_mem, is_admin

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
        self.advice_count = 0        # 后台低频刷新的「优化建议条数」，供菜单标签零成本读取
        self._advice_tick = 0
        self.stop = threading.Event()
        self.icon: pystray.Icon | None = None

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
                # 优化建议条数在监控线程后台低频刷新：菜单标签只读缓存值，
                # 避免每次展开菜单都遍历全进程（原实现的明显卡顿来源）。
                self._advice_tick += 1
                if self._advice_tick % 3 == 1:
                    try:
                        self.advice_count = len(analyze(self.cfg))
                    except Exception:
                        pass
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
                    "MemGuard 运行中", build_menu(self),
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
