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

from . import update
from .advisor import advice_actions, analyze, frequent_escalation
from .clean import do_clean, predictive_due, top_processes_list
from .config import CONFIG_PATH, __version__, gb, load_config, log, update_config
from .menu import build_menu
from .ui import (make_icon, show_advice_window, show_overview_window,
                show_top_window, show_trend_window)
from .winapi import (close_show_event, create_show_event, ensure_tray_promoted,
                    get_mem, is_admin, wait_show_request)

# ---------------------------------------------------------------- 主程序

# pystray 的 NIM_ADD 发生在 icon.run() 里，而 _declare_tray_pin 抢在它前面跑，注册表
# 条目多半还没影；最多等这么久让条目出现，等不到就交给日志说明。
TRAY_PIN_WAIT = 15.0


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
        self.advice_apply_count = 0  # 其中「可一键应用」的条数，供菜单项决定能否点击
        self._advice_at = 0.0        # 上次刷新建议的时间戳（按 advice_refresh_sec 节流）
        self._icon_img = None        # 当前托盘图标正在显示的图像对象，用于跳过无变化的重绘
        self.last_scheduled = time.time()  # 定时清理计时起点（启动后先等一个完整周期）
        self.stop = threading.Event()
        self.icon: pystray.Icon | None = None
        self._show_event = None       # 唤窗事件句柄：给二次启动的信号用
        self._tray_pin_done = False   # 托盘常驻声明是否已尝试过（只试一次）
        self._update_checking = False  # 是否已有一次更新检查在途（防重复发起）


    # -- 循环 ----------------------------------------------------

    def refresh(self) -> None:
        self.state = get_mem()

    # -- 概览窗口（对标 WinMemoryCleaner / Mem Reduct：左键点托盘就打开主窗口）----

    def open_overview(self) -> None:
        """打开内存概览窗口。

        用 hooks 把窗口里的按钮接回 Guard，ui 层因此不需要反向依赖 tray。
        """
        show_overview_window({
            "clean": lambda: do_clean("手动"),
            "top": show_top_window,
            "trend": lambda: show_trend_window(lambda: list(self.history)),
            # 传 callable 而非 dict：窗口内「一键应用」会经 update_config 换掉
            # guard.cfg 这个对象，传 callable 才能让窗口刷新读到最新配置。
            "advice": lambda: show_advice_window(lambda: self.cfg),
        })

    # -- 唤窗信号（从任务栏再点一次图标时用）-------------------------------------

    def _start_show_waiter(self) -> None:
        """监听「唤窗信号」，让二次启动能把概览窗唤出来。

        信号是命名事件（见 winapi），等待放在守护线程里轮询，不占托盘主循环的精力。
        开机自启默认不带 --show，也就没人往里发信号，登录时不会弹窗。
        """
        if self._show_event is not None:
            return
        self._show_event = create_show_event()
        if self._show_event is None:
            log("唤窗事件创建失败：任务栏图标再次点击时无法唤出概览窗")
            return
        threading.Thread(target=self._show_waiter, daemon=True).start()

    def _show_waiter(self) -> None:
        handle = self._show_event
        while handle and not self.stop.is_set():
            try:
                if wait_show_request(handle):
                    self.open_overview()
            except Exception as e:
                log(f"唤窗等待异常: {e!r}")
                break


    def _auto_clean(self, now: float, reason: str, s: dict, note: str = "",
                    preventive: bool = False) -> None:
        """执行一次自动清理并弹通知（各触发源共用；reason 用于日志与通知标题）。

        note 非空时追加到通知正文末尾：预防式清理用它解释「还没超阈值为什么清」。
        preventive=True 表示本次是趋势预防式清理，do_clean 会累计 stats.preventive，
        供统计行展示与 advisor 的自调优建议使用。
        """
        self.last_clean = now
        self.over_since = None
        r = do_clean(reason, preventive=True) if preventive else do_clean(reason)
        if r["ok"]:
            # 统计已在 do_clean 里落盘：顺手同步回内存，否则菜单顶部那行累计统计要等
            # 下一次热重载（间隔由 interval 决定，最长可能等很久）才刷新
            if r.get("stats"):
                self.cfg["stats"] = r["stats"]
        if r["ok"] and self.icon:
            top3 = top_processes_list(3)
            top_txt = "\n".join(f"  {n} {rss / 1024 ** 3:.2f}GB" for n, rss, _ in top3)
            lvl_txt = "激进" if r.get("level") == "aggressive" else "保守"
            if r.get("escalated"):
                lvl_txt += "（自动升档）"
            body = (f"物理 {s['phys_pct']:.0f}% / 提交 {s['commit_pct']:.0f}%\n"
                    f"已释放 {gb(max(r['freed'], 0))}（{lvl_txt}档）\n"
                    f"当前占用 Top3:\n{top_txt}")
            if r.get("targeted"):
                tgt_txt = "\n".join(
                    f"  {n} {rss / 1024 ** 3:.2f}GB" for n, rss, _ in r["targeted"])
                body += f"\n定向清理大户:\n{tgt_txt}"
            if note:
                body += f"\n{note}"
            title = "MemGuard 自动清理" if reason == "自动" else f"MemGuard {reason}清理"
            try:
                self.icon.notify(
                    body,
                    title,
                )
            except Exception:
                pass

    def _maybe_predictive_clean(self, now: float, s: dict) -> bool:
        """趋势预防式清理（v1.8.0）：还没超阈值，但按上升斜率很快就要超。

        与触发源 1 互补：超阈值是「反应式」，这里是拿 history 拟合斜率、预判触阈
        时间，在预测窗口内就提前清一次，让占用始终不真的越线。走的仍是
        「保守→定向→升档」那套阶梯，打扰不比一次普通自动清理更多。
        门：开关 + 自动清理 + 冷却 + predictive_due 判定，返回是否已触发清理。
        """
        try:
            cfg = self.cfg
            if not (cfg.get("predict_clean") and cfg["auto_clean"]):
                return False
            if now - self.last_clean <= cfg["cooldown"]:
                return False
            pred = predictive_due(self.history, cfg)
            if not pred:
                return False
            note = (f"趋势 {pred['slope_pm']:+.1f}%/分钟，预计 {pred['eta'] / 60:.1f} "
                    f"分钟后触及{pred['metric']}阈值，已提前清理")
            log(f"预防式清理 | {note}")
            self._auto_clean(now, "预防式", s, note=note, preventive=True)
            # 同一次 tick 不再弹「接近阈值」预警：已经提前处理过了
            self._warned = True
            return True
        except Exception as e:
            log(f"预防式清理异常: {e!r}")
            return False

    def maybe_reload_config(self) -> None:
        """检查配置文件 mtime，变化则热重载（无需重启托盘）。"""
        try:
            mtime = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else None
            if mtime != self._cfg_mtime:
                self._cfg_mtime = mtime
                if mtime is not None:
                    self.cfg = load_config()
                    # 配置刚变（可能换了档位/阈值），把建议节流计时清零，让菜单上的
                    # 「优化建议（N 条）」立刻反映新配置，而不是最多再等一个周期
                    self._advice_at = 0.0
                    log("配置已热重载（来自 mem_guard.json）")
        except Exception as e:
            log(f"配置重载检查失败: {e}")

    def _refresh_advice(self, now: float) -> None:
        """按时间节流刷新「建议条数 / 可一键应用条数」：一次进程扫描只喂给 analyze。

        菜单标签只读 advice_count / advice_apply_count，所以这里可以放心低频；扫描
        失败保留上次的值，且时间戳已前移，不会在监控线程里对失败项发起热重试。
        """
        if now - self._advice_at < self.cfg.get("advice_refresh_sec", 60):
            return
        self._advice_at = now
        try:
            items = analyze(self.cfg, self.state, top_processes_list(15))
            self.advice_count = len(items)
            self.advice_apply_count = len(advice_actions(items))
        except Exception:
            pass

    def _maybe_adapt_level(self) -> None:
        """自调优闭环（v1.6.0）：保守档频繁升档时自动改用激进档，整个生命周期最多一次。

        判据与 advisor 3b 同源（frequent_escalation）：累计清理 escalated 过半且
        ≥3 次——「先温和再彻底」每次多跑一遍、多打扰一次，不如一次清到位。切换走
        update_config（读-改-写持锁）并落 level_adapt_done 闩：用户手动切回保守也不会
        被再次自动覆盖；关掉 auto_level_adapt 则完全不评估。任何异常只记日志，绝不让
        监控循环崩。
        """
        try:
            cfg = self.cfg
            if not cfg.get("auto_level_adapt", True) or cfg.get("level_adapt_done"):
                return
            if not frequent_escalation(cfg):
                return
            st = cfg.get("stats") or {}
            clean_cnt = int(st.get("count", 0) or 0)
            esc_cnt = int(st.get("escalated", 0) or 0)
            self.cfg = update_config({"clean_level": "aggressive",
                                      "level_adapt_done": True})
            log(f"自动优化 | 保守档累计 {clean_cnt} 次清理中 {esc_cnt} 次仍不达标需升档，"
                f"清理力度已自动切换为激进（一次性，不再自动评估，可随时手动改回）")
            if self.icon:
                try:
                    self.icon.notify(
                        f"保守档 {clean_cnt} 次清理有 {esc_cnt} 次清完仍不达标\n"
                        f"已自动切换为激进档：一次清到位，少跑一遍少打扰一次\n"
                        f"（右键菜单可随时改回保守）",
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动档位适配异常: {e!r}")

    def _refresh_icon(self) -> None:
        """刷新托盘显示；图标内容没变就不赋值给 pystray。

        pystray 的 icon 赋值没有内部判等：每次都会 DestroyIcon + 重建 HICON +
        发一次 NIM_MODIFY，而 make_icon 对相同（文本, 颜色）返回同一对象，判身份即可。
        鼠标提示每次都赋值：pystray 的 title setter 自己判等，且可用内存数值确实会变。
        """
        icon = self.icon
        if not icon:
            return
        s = self.state
        try:
            img = make_icon(s["phys_pct"], self.cfg["phys_threshold"])
            if img is not self._icon_img:
                self._icon_img = img
                icon.icon = img
            icon.title = (
                f"MemGuard  物理 {s['phys_pct']:.0f}% / 提交 {s['commit_pct']:.0f}%\n"
                f"可用物理 {gb(s['avail_phys'])}    可用提交 {gb(s['avail_commit'])}"
            )
        except Exception:
            pass

    def _maybe_check_update(self, now: float) -> None:
        """后台静默检查更新的节流门：到期且没有在途检查才放行。"""
        if self._update_checking:
            return
        try:
            if not update.due_for_check(self.cfg, now):
                return
            # 先把内存里的时间戳推到当前：interval 可能长达 1 小时，若等 worker
            # 落盘，这段时间里每个周期都会再次发起检查（弱网时就是连续请求）
            self.cfg["last_update_check"] = round(now, 3)
        except Exception as e:
            log(f"更新检查调度失败: {e!r}")
            return
        self._update_checking = True
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self) -> None:
        """后台线程查一次更新：失败/已最新只写日志，有新版才发一条气泡。

        配了「下载后自动安装」时发现新版会直接进入安装，安装前由
        _stop_for_update 收掉托盘，保证目标 exe 不被占用。
        """
        try:
            update.check_and_notify(self.cfg, notify=self._notify_update,
                                    on_ready=self._stop_for_update)
        except Exception as e:
            log(f"更新检查异常: {e!r}")
        finally:
            self._update_checking = False

    def _notify_update(self, msg: str, title: str) -> None:
        """更新通知：走托盘气泡，图标还没就绪时静默跳过。"""
        icon = self.icon
        if not icon:
            return
        try:
            icon.notify(msg, title)
        except Exception:
            pass

    def _stop_for_update(self) -> None:
        """安装开始前的收尾：停监控循环、收托盘图标，让目标 exe 不被占用。"""
        self.stop.set()
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass

    def _scheduled_due(self, now: float) -> bool:
        """定时清理是否到期：到期且距上次自动清理已过冷却才返回 True。

        冷却内直接跳过且不推进 last_scheduled，冷却结束后仍能触发，
        避免与阈值清理背靠背双清双气泡（cooldown 是两次自动清理的冷却）。
        """
        sched = self.cfg.get("scheduled_minutes", 0)
        return bool(sched and now - self.last_scheduled >= sched * 60
                    and now - self.last_clean > self.cfg["cooldown"])

    def monitor(self) -> None:
        self.maybe_reload_config()
        # 启动时清理（对标 Mem Reduct）：放监控线程内做，不阻塞托盘图标出现
        if self.cfg.get("clean_on_start"):
            try:
                log("启动时清理：按配置执行一次")
                do_clean("启动")
                self.last_clean = time.time()
            except Exception as e:
                log(f"启动清理异常: {e!r}")
        while not self.stop.is_set():
            try:
                self.maybe_reload_config()
                self.refresh()
                s = self.state
                self.history.append((time.time(), s["phys_pct"], s["commit_pct"]))
                now = time.time()
                self._refresh_advice(now)
                self._maybe_adapt_level()
                self._maybe_check_update(now)
                self._refresh_icon()
                over = (s["phys_pct"] >= self.cfg["phys_threshold"]
                        or s["commit_pct"] >= self.cfg["commit_threshold"])
                # 触发源 4（提前量）：趋势预防式清理，未越线但按斜率即将触阈
                if not over and self._maybe_predictive_clean(now, s):
                    self._warned = True
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
                # 触发源 1：超阈值（百分比，带防抖 + 冷却 + 自动清理开关）
                if over:
                    if self.over_since is None:
                        self.over_since = now
                    debounced = (now - self.over_since) >= self.cfg.get("debounce_sec", 0)
                    due = (self.cfg["auto_clean"] and debounced
                           and now - self.last_clean > self.cfg["cooldown"])
                    if due:
                        self._auto_clean(now, "自动", s)
                else:
                    self.over_since = None
                # 触发源 2：可用物理内存低于绝对阈值（对标 Mem Reduct 的低内存触发器）
                min_avail = self.cfg.get("min_avail_mb", 0)
                if (min_avail and s["avail_phys"] < min_avail * 1024 * 1024
                        and self.cfg["auto_clean"]
                        and now - self.last_clean > self.cfg["cooldown"]):
                    self._auto_clean(now, "低内存", s)
                # 触发源 3：定时清理（不看内存占用，按设定间隔触发）
                if self._scheduled_due(now):
                    self.last_scheduled = now
                    self._auto_clean(now, "定时", s)
            except Exception as e:
                log(f"监控异常: {e}")
            self.stop.wait(self.cfg["interval"])

    def _declare_tray_pin(self) -> None:
        """图标登记后顺手把「常驻托盘」写进注册表。

        pystray 的 NIM_ADD 在 icon.run() 里才发生，本方法抢在它前面，条目多半还没影，
        所以丢到后台线程里等（最多 TRAY_PIN_WAIT 秒），别卡住托盘主循环。Explorer 也
        只在（重）启时读 IsPromoted，所以这里只写注册表；重启动作留给 --pin-tray，
        绝不擅自重启用户的外壳。任何失败只记日志，托盘照常工作。

        这里不补占位图标（allow_add=False）：托盘进程自己就有图标，再 NIM_ADD 一枚
        只会多出个重复条目。占位图标是 --pin-tray 那条「程序还没起」的流的补法。
        """
        if self._tray_pin_done:
            return
        self._tray_pin_done = True
        threading.Thread(target=self._tray_pin_worker, daemon=True).start()

    def _tray_pin_worker(self) -> None:
        """后台等条目出现并写 IsPromoted。"""
        try:
            result = ensure_tray_promoted(wait_sec=TRAY_PIN_WAIT)
        except Exception as e:
            log(f"托盘常驻声明失败: {e!r}")
            return
        if result == "updated":
            log("托盘常驻已声明（注册表 IsPromoted=1）；重启 Explorer 后生效，"
                "或运行 mem_guard --pin-tray 立即生效")
        elif result == "missing":
            log(f"托盘常驻：等 {TRAY_PIN_WAIT:.0f} 秒仍未在注册表见到本程序条目，"
                "本次跳过（下次启动自动补）")
        elif result == "unsupported":
            log("托盘常驻：当前平台不支持（非 Windows）")
        elif result == "already":
            log("托盘常驻：注册表已是常驻（IsPromoted=1）")


    def run(self, show_on_start: bool = False) -> None:
        s = get_mem()
        log(f"MemGuard v{__version__} 启动 | 管理员={is_admin()} | 物理 {s['phys_pct']:.0f}% | "
            f"提交 {s['commit_pct']:.0f}% | 档位={self.cfg.get('clean_level')}")
        if not is_admin():
            log("提示：非管理员运行，自动清理不会生效，请右键以管理员身份运行")
        # 监控线程只启动一次；托盘图标在循环内可被重建，实现"托盘自愈"
        threading.Thread(target=self.monitor, daemon=True).start()

        self._start_show_waiter()
        while not self.stop.is_set():
            try:
                self.icon = _GuardIcon(
                    "MemGuard", make_icon(self.state["phys_pct"], self.cfg["phys_threshold"]),
                    "MemGuard 运行中", build_menu(self), on_left_click=self.open_overview,
                )
                self._declare_tray_pin()
                if show_on_start:
                    # 从任务栏/桌面快捷方式主动启动：把概览窗一起带出来。
                    # 开机自启不带 --show，不会走到这里，登录时不会弹窗。
                    # 托盘自愈重建后不重复弹，所以这里当一次性开关用。
                    show_on_start = False
                    threading.Thread(target=self.open_overview, daemon=True).start()
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

        close_show_event(self._show_event)


class _GuardIcon(pystray.Icon):
    """左键打开概览窗口的托盘图标。

    pystray 默认左键展开右键菜单（`Icon.__call__`），同类工具（WinMemoryCleaner /
    Mem Reduct）都是左键开主窗口、右键给菜单；这里只覆盖左键行为，右键菜单不变。
    """

    def __init__(self, *args, on_left_click=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_left_click = on_left_click

    def __call__(self):
        if self._on_left_click is None:
            super().__call__()
            return
        try:
            self._on_left_click()
        except Exception as e:
            log(f"概览窗口打开失败: {e!r}")
