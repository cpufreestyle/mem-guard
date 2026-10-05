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
from .advisor import (advice_actions, analyze, deepen_round_saturated,
                      frequent_escalation, frequent_short_relief,
                      narrow_top_coverage, predict_window_saturated,
                      threshold_too_late, unreachable_top_threshold)
from .clean import (GROWTH_MAX_SAMPLES, _DEEPEN_MAX_ROUNDS, _HEADROOM_MAX,
                    _HEADROOM_STEP, _MINAVAIL_MAX, _MINAVAIL_STEP,
                    _MINMB_DIV, _MINMB_FLOOR_MB, _PREDICT_WINDOW_MAX,
                    _PREDICT_WINDOW_STEP, _THRESHOLD_COMMIT_FLOOR,
                    _THRESHOLD_PHYS_FLOOR,
                    _THRESHOLD_STEP, do_clean, leak_candidates,
                    predictive_due, top_processes_list)
from .config import (CONFIG_PATH, TARGET_CLEAN_TOP_MAX, __version__, gb,
                     load_config, log, update_config)
from .menu import build_menu
from .ui import (make_icon, show_advice_window, show_overview_window,
                show_top_window, show_trend_window)
from .winapi import (close_show_event, create_show_event, ensure_tray_promoted,
                    get_mem, is_admin, wait_show_request)

# ---------------------------------------------------------------- 主程序

# pystray 的 NIM_ADD 发生在 icon.run() 里，而 _declare_tray_pin 抢在它前面跑，注册表
# 条目多半还没影；最多等这么久让条目出现，等不到就交给日志说明。
TRAY_PIN_WAIT = 15.0

# ---- 自适应冷却（v1.10.0）：短效复发时把下次最小间隔压缩，连续 hold 住后恢复用户值 ----
_COOLDOWN_SHRINK = 0.5   # 压缩系数：短效复发后的最小间隔 = max(cooldown * 系数, floor)
_COOLDOWN_OK_STREAK = 2  # 连续这么多次距上次清理 >= effect_min_relief_sec，就恢复完整 cooldown

# ---- 清理提前量自调优（v1.16.0）：效果连续偏短时把判定阈值提前几个百分点 ----
_HEADROOM_STRIKES = 2    # 连续这么多次效果偏短，就把 target_headroom_pct 加上 _HEADROOM_STEP 个百分点


# ---- 低内存下限自调优（v1.17.0）：绝对下限触发的清理连续偏短时抬高 min_avail_mb ----
_MINAVAIL_STRIKES = 2    # 连续这么多次「低内存触发且效果偏短」，就把绝对下限抬高 _MINAVAIL_STEP MB

# ---- 预防窗口自调优（v1.21.0）：预防式清理没防住压力时，把预测窗口放宽一档 ----
_PREDICT_MISS_STRIKES = 2  # 连续这么多次「预防式清理后仍很快复发」，就把 predict_window_min 加宽

# ---- 触发阈值自适应（v1.22.0）：温和阶梯用尽仍效果偏短时，把两条触发线各降一档 ----
_THRESHOLD_STRIKES = 3    # 连续这么多次「超阈值触发且效果偏短」，就把 phys/commit 阈值各降一档

# ---- 阶段自学习重试（v1.21.0）：被学废的阶段连续跳过这么多轮后，重新给一次试验机会 ----
_STAGE_REARM_ROUNDS = 5    # 某阶段被 stage_learn 连续跳过这么多轮，就清它的零释放计数再试一次


class Guard:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.state = get_mem()
        self.last_clean = 0.0
        self.over_since = None       # 内存超阈值起始时刻（防抖用）
        self.history = collections.deque(maxlen=120)  # 内存使用率采样历史
        self.proc_history = collections.deque(maxlen=GROWTH_MAX_SAMPLES)  # 逐进程采样历史
        self.leaks = []              # 疑似泄漏进程 [(name, rss, pid), ...]
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
        # 自适应冷却（v1.10.0）：纯内存态，不落盘；短效复发压间隔，连续达标恢复
        self._relief_short_streak = 0  # 连续「距上次清理不足 effect_min_relief_sec」次数
        self._relief_ok_streak = 0     # 连续「效果达标」次数
        self._level_seen = self.cfg.get("clean_level")  # 上次观察到的档位（识别用户手动切回保守）
        # v1.12.0 持续压力粘滞激进：纯内存态，不落盘。上一轮走完整条阶梯仍没压住
        # 内存压力时置位，下一轮自动清理直接按激进档执行（见 _post_clean_learn）
        self._sticky_aggr = False
        # v1.12.0 阶梯阶段自学习：某阶段连续多次没释放出东西就跳过它（不落盘）
        # v1.15.0 起加深也算一个可学的阶段：它只跳过撒宽补刀，第一轮定向照跑
        self._stage_zero = {"targeted": 0, "bg": 0, "deepen": 0}

        # v1.17.0 低内存下限自调优（纯内存态，不落盘）：_last_clean_reason 记住上一次
        # 自动清理的触发源——_note_relief 度量的是上一次清理的效果，归因要配上它；
        # _lowmem_short_streak 累计「低内存触发且效果偏短」的连发次数，达标才抬高下限
        self._last_clean_reason = ""
        self._lowmem_short_streak = 0

        # v1.21.0 预防窗口自调优（纯内存态，不落盘）：_predictive_eta 记住上一次
        # 预防式清理时预测的触阈时间（秒），_note_relief 拿「距上次自动清理多久」跟它
        # 比——比 eta 还短就是没防住；判一次即清标记，pending 让本次 do_clean 落盘
        self._predictive_eta = None
        self._predict_miss_streak = 0
        self._predict_miss_pending = False
        # v1.21.0 阶段自学习重试（纯内存态，不落盘）：被跳过的阶段连续这么多轮没再上场，
        # 就清掉零释放计数重新给一次试验机会，免得工况变了它永远被钉在跳过集里
        self._stage_skip = {"targeted": 0, "bg": 0, "deepen": 0}

        # v1.22.0 触发阈值自调优（纯内存态，不落盘）：_last_clean_escalated 记住
        # 上一次清理是否自动升过档——升过才说明温和阶梯确曾用尽，是动触发线的证据；
        # _threshold_short_streak 累计「超阈值触发且效果偏短」的连发次数，达标才降线
        self._last_clean_escalated = False
        self._threshold_short_streak = 0


    # -- 循环 ----------------------------------------------------

    def refresh(self) -> None:
        self.state = get_mem()

    # -- 概览窗口（对标 WinMemoryCleaner / Mem Reduct：左键点托盘就打开主窗口）----

    def open_overview(self) -> None:
        """打开内存概览窗口。

        用 hooks 把窗口里的按钮接回 Guard，ui 层因此不需要反向依赖 tray。
        """
        show_overview_window({
            "clean": lambda: do_clean("手动", growth_rows=self.leaks),
            "top": show_top_window,
            "trend": lambda: show_trend_window(lambda: list(self.history)),
            # 传 callable 而非 dict：窗口内「一键应用」会经 update_config 换掉
            # guard.cfg 这个对象，传 callable 才能让窗口刷新读到最新配置。
            # history 同理：v1.15.0 的覆盖面自调优建议只认 proc_history 这份采样。
            "advice": lambda: show_advice_window(lambda: self.cfg,
                                                  lambda: self.proc_history),
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
                    preventive: bool = False, low_relief: bool = False) -> None:
        """执行一次自动清理并弹通知（各触发源共用；reason 用于日志与通知标题）。

        note 非空时追加到通知正文末尾：预防式清理用它解释「还没超阈值为什么清」。
        preventive=True 表示本次是趋势预防式清理，do_clean 会累计 stats.preventive，
        供统计行展示与 advisor 的自调优建议使用。
        low_relief=True 表示本次距上次自动清理不足 effect_min_relief_sec（v1.9.0
        效果闭环）：上次清理没hold住、压力很快复发。动手前先度量间隔——effect_track
        关闭、或上次清理来自手动/CLI（last_clean=0）时不测；短效则透传给 do_clean
        累计 stats.short_relief，供统计行、通知与 advisor 自调优使用。
        v1.12.0 起还负责持续压力粘滞激进与阶梯阶段自学习：_sticky_aggr 置位
        且 _sticky_allowed() 时以 sticky=True 调 do_clean 直接激进执行，并按
        _skip_stages() 跳过已被学习证明无效的阶梯阶段；清理后由
        _post_clean_learn 更新上述状态，返回的说明句追加到通知正文。
        v1.13.0 起定向清理证明有效但压力没完全按住时，do_clean 会自行把挑选网撒宽
        一轮再清（定向加深）：仍在阶梯内部逐级加重，不需要这里额外决策。
        """
        prev = self.last_clean
        relief = now - prev if prev > 0 else None
        if (relief is not None and self.cfg.get("effect_track", True)
                and relief < int(self.cfg.get("effect_min_relief_sec", 600) or 0)):
            low_relief = True
        if low_relief and relief is not None:
            log(f"{reason}清理 | 距上次自动清理 {relief / 60:.1f} 分钟，"
                f"上次清理未稳住内存（效果偏短）")
        self.last_clean = now
        self._note_relief(relief, low_relief)
        # _note_relief 度量的是上一次清理的效果，归因要配上一次的触发源，故在它之后
        # 才更新（这一次的原因留给下一次效果判定用）
        self._last_clean_reason = reason
        # 绝对下限触发（可用物理内存低于 min_avail_mb）时给本次清理打上低内存标记，
        # do_clean 据此累计 stats.low_mem，作为抬高该下限的证据
        low_mem = (reason == "低内存")
        self.over_since = None
        kw = {}
        if preventive:
            kw["preventive"] = True
        if low_relief:
            kw["low_relief"] = True
        if low_mem:
            kw["low_mem"] = True
        # v1.21.0：上次预防式清理没防住（_note_relief 判定过），让本次清理把 missed
        # 落进 stats.preventive_missed，作为放宽预测窗口的证据；读完立刻撤
        kw["predictive_missed"] = bool(self._predict_miss_pending)
        self._predict_miss_pending = False
        # 泄漏进程一起带下去：定向清理会优先清它们（见 do_clean 的 growth_rows）
        sticky_on = self._sticky_aggr and self._sticky_allowed()
        skip = self._skip_stages()
        r = do_clean(reason, growth_rows=self.leaks, sticky=sticky_on,
                     skip=skip, **kw)
        sticky_note = ""
        if r["ok"]:
            # 统计已在 do_clean 里落盘：顺手同步回内存，否则菜单顶部那行累计统计要等
            # 下一次热重载（间隔由 interval 决定，最长可能等很久）才刷新
            if r.get("stats"):
                self.cfg["stats"] = r["stats"]
            # v1.12.0 阶段自学习：按 still / targeted_freed 等结果更新粘滞态与跳过集
            sticky_note = self._post_clean_learn(r, skip)
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
            if r.get("bg_trim"):
                body += f"\n后台进程工作集: {r['bg_trim']} 个"
            if r.get("deepened"):
                # v1.13.0 定向加深：同一张网撒宽又清到东西，别让用户以为定向白跑了；
                # v1.14.0 起可多轮，只有真跑了多轮才补一句轮数，别把通知写长
                rounds = r.get("deepen_rounds") or 0
                more = f"（{rounds} 轮）" if rounds > 1 else ""
                body += f"\n定向加深: {r['deepened']} 个{more}"
            # v1.12.0 分阶段释放量：只列真的清到了东西的阶段，别把通知刷长
            stage_lines = []
            for label, key in (("定向", "targeted_freed"), ("后台", "bg_freed"),
                               ("升档", "escalated_freed")):
                v = r.get(key) or 0
                if v > 0:
                    stage_lines.append(f"{label} {gb(v)}")
            if stage_lines:
                body += "\n分阶段释放: " + "、".join(stage_lines)
            if r.get("sticky"):
                body += "\n持续高压：粘滞激进"
            if relief is not None and self.cfg.get("effect_track", True):
                body += f"\n距上次自动清理 {relief / 60:.1f} 分钟"
                if low_relief:
                    body += "（偏短）"
            if sticky_note:
                body += f"\n{sticky_note}"
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

    def _cooldown_gap(self) -> int:
        """本次自动清理后的最小间隔（秒）：v1.10.0 自适应冷却。

        静态 cooldown 是最大缺口：短效复发时不会更快复查，连续 hold 住后也不放松。
        这里与 _auto_clean 共用同一份判定输入：距上次清理不足 effect_min_relief_sec
        时累计 _relief_short_streak，把最小间隔压到 max(cooldown * _COOLDOWN_SHRINK,
        adaptive_cooldown_floor)，连续 _COOLDOWN_OK_STREAK 次达标再恢复完整 cooldown。
        压缩只减不增：clamp(0, cooldown)，floor 高于 cooldown 也不放大；开关关闭时
        恒为用户值。四处冷却判定（趋势预防 / 超阈值 / 低内存 / 定时）都走这里。
        """
        cfg = self.cfg
        gap = int(cfg.get("cooldown", 300) or 0)
        if not cfg.get("adaptive_cooldown", True) or self._relief_short_streak <= 0:
            return gap
        floor = int(cfg.get("adaptive_cooldown_floor", 30) or 0)
        return max(0, min(gap, max(int(gap * _COOLDOWN_SHRINK), floor)))

    def _note_relief(self, relief, low_relief: bool) -> None:
        """记录本次自动清理的效果好坏，驱动 _cooldown_gap 的自适应（纯内存态，不落盘）。

        relief 为 None（上次清理来自手动/CLI，或无上次记录）时不判定；短效累计
        _relief_short_streak，连续 _COOLDOWN_OK_STREAK 次达标清零、恢复完整冷却。
        v1.17.0 起还驱动低内存下限自调优（_maybe_adapt_min_avail）：只有上一次清理
        本来就是被绝对下限叫起来的，短效才算它的证据（归因配 _last_clean_reason）。
        v1.21.0 起还驱动预防窗口自调优（_maybe_adapt_predict_window）：上一次清理
        是预防式时，本次间隔比它当时预测的 eta 还短就是没防住，连发才放宽窗口。
        v1.22.0 起还驱动触发阈值自调优（_maybe_adapt_threshold）：上一次是超阈值
        「自动」触发且效果偏短时累计连发，达标才把两条触发线各降一档。
        """
        if relief is None:
            return
        # v1.21.0 预防窗口自调优：上一次清理是预防式时，拿本次距上次清理的间隔和它
        # 当时预测的 eta 比——比 eta 还短就是没防住（预测说这么久才触阈，更早就复发了）
        if self._predictive_eta is not None:
            eta = self._predictive_eta
            self._predictive_eta = None  # 只判一次：判完就撤标记
            if relief < eta:
                self._predict_miss_streak += 1
                # 让本次清理把 missed 落进 stats.preventive_missed（放宽窗口的证据）
                self._predict_miss_pending = True
                if self._predict_miss_streak >= _PREDICT_MISS_STRIKES:
                    self._maybe_adapt_predict_window()
            else:
                self._predict_miss_streak = 0
        if low_relief:
            self._relief_ok_streak = 0
            self._relief_short_streak += 1
            if self._relief_short_streak >= _HEADROOM_STRIKES:
                self._maybe_adapt_headroom()
            # 百分比阈值触发的短效归提前量管；只有低内存下限触发的才算它的证据
            if self._last_clean_reason == "低内存":
                self._lowmem_short_streak += 1
                if self._lowmem_short_streak >= _MINAVAIL_STRIKES:
                    self._maybe_adapt_min_avail()
            else:
                self._lowmem_short_streak = 0
            # v1.22.0 触发阈值自调优：只有超阈值「自动」触发的短效才算触发线的
            # 证据；低内存下限触发的归绝对下限适配管，预防式归预测窗口适配管
            if self._last_clean_reason == "自动":
                self._threshold_short_streak += 1
                if self._threshold_short_streak >= _THRESHOLD_STRIKES:
                    self._maybe_adapt_threshold()
            else:
                self._threshold_short_streak = 0
        else:
            self._lowmem_short_streak = 0
            self._threshold_short_streak = 0
            self._relief_ok_streak += 1
            if self._relief_ok_streak >= _COOLDOWN_OK_STREAK:
                self._relief_short_streak = 0

    # -- 持续压力粘滞激进 + 阶梯阶段自学习（v1.12.0）--------------------

    def _sticky_allowed(self) -> bool:
        """粘滞激进是否被允许：总开关或档位自调优关闭时都不粘滞。

        sticky_aggressive 是本次功能的总开关；auto_level_adapt 关闭说明用户
        连档位都不想让它自己动，粘滞（本质是强制激进）同样不该自作主张。
        """
        return bool(self.cfg.get("sticky_aggressive", True)
                    and self.cfg.get("auto_level_adapt", True))

    def _skip_stages(self) -> set:
        """本轮要跳过的阶梯阶段名集合（stage_learn 关闭或未达标时为空集）。

        某阶段连续 stage_learn_strikes 次没释放出东西，说明它对当前这轮压力
        没用，跳过省时间；只影响 targeted/bg/deepen 这几个阶梯阶段（ deepen 被跳过
        只是不再撒宽加深，第一轮定向大户照跑），升档不受影响。
        """
        if not self.cfg.get("stage_learn", True):
            return set()
        strikes = int(self.cfg.get("stage_learn_strikes", 3) or 0)
        return {name for name, zero in self._stage_zero.items() if zero >= strikes}

    def _bump_stage_zero(self, name: str, freed: int) -> None:
        """记录某阶梯阶段本轮是否真的清到了东西：清到了就清零计数，否则记一次。"""
        if freed > 0:
            self._stage_zero[name] = 0
        else:
            self._stage_zero[name] += 1

    def _rearm_skipped_stages(self, skipped) -> None:
        """被跳过的阶梯阶段连续多轮没上场后，重新给一次试验机会（v1.21.0，纯内存态）。

        阶段自学习（v1.12.0/v1.15.0）按零释放过往判「该跳」，却没有回头路：工况变了
        （大户被重启、后台程序被关掉）之后，那个阶段照样躺在跳过集里，永远不再上场。
        这里给每个阶段记「连续被跳过几轮」——攒到 _STAGE_REARM_ROUNDS 就清掉它的零
        释放计数，下一轮重新试一次；真的还是清不出东西，几轮之后又会被学回去。重试
        只是试验不是加码：只清计数，不动任何配置，也不弹气泡（少打扰）。
        """
        skipped = skipped or set()
        for name in ("targeted", "bg", "deepen"):
            if name in skipped:
                self._stage_skip[name] = self._stage_skip.get(name, 0) + 1
                if self._stage_skip[name] >= _STAGE_REARM_ROUNDS:
                    self._stage_zero[name] = 0
                    self._stage_skip[name] = 0
                    log(f"阶段自学习重试 | 阶段 {name} 已连续跳过 "
                        f"{_STAGE_REARM_ROUNDS} 轮，重新给一次试验机会")
            else:
                self._stage_skip[name] = 0

    def _post_clean_learn(self, r: dict, skipped=None) -> str:
        """按清理结果更新粘滞态与阶段零释放计数，返回要追加进通知的说明句。

        压力解除（still 为假）：退出粘滞、阶段计数清零，回到「从保守档重跑」。
        压力仍在且未粘滞：进入粘滞，下一轮直接激进；已粘滞则保持。阶段计数只在
        本轮真的跑了阶梯时更新——粘滞轮整条阶梯都被跳过，没什么可学的。
        v1.13.0 起定向加深的释放量已计入 targeted_freed，这里看到的仍是「定向这
        一级」的总战绩：加深也算这级有用，不会把它误判成该跳过的空转阶段。
        v1.15.0 起加深另有 deepen_rounds / deepen_freed 单独报量，于是它可以被单独
        学：连续多轮加深都清不出东西就只跳过加深，别每次都白撒一张更宽的网。
        v1.21.0 起另做「阶段自学习重试」：skipped 是本轮被跳过的阶段名集合（来自
        _skip_stages，与传给 do_clean 的是同一份），被跳过的阶段连续
        _STAGE_REARM_ROUNDS 轮没上场就重新给一次试验机会（见 _rearm_skipped_stages）。
        v1.22.0 起另记「上一次清理是否自动升过档」：_note_relief 度量的是上一次
        清理的效果，动触发线前要确认温和阶梯确曾用尽，归因要配上这一位；早退
        （压力解除）也要记，不能漏掉升过档又恰好看住的那一轮。
        """
        self._last_clean_escalated = bool(r.get("escalated"))
        if not r.get("still"):
            if self._sticky_aggr:
                log("粘滞激进退出 | 清理后压力已解除，下次自动清理回到保守档起重")
            self._sticky_aggr = False
            self._stage_zero = {"targeted": 0, "bg": 0, "deepen": 0}
            # 压力解除，整套学习结论一起作废：重试计数也归零（v1.21.0）
            self._stage_skip = {"targeted": 0, "bg": 0, "deepen": 0}
            return ""
        note = ""
        if not r.get("sticky") and not self._sticky_aggr and self._sticky_allowed():
            self._sticky_aggr = True
            note = "内存压力持续，后续自动清理将直接按激进档执行（跳过已证不够用的阶梯）"
            log("粘滞激进进入 | 完整阶梯后压力仍在，下次自动清理直接按激进档")
        if not r.get("sticky"):
            # v1.21.0 阶段自学习重试：被跳过的阶段连续多轮没上场就重新给一次试验机会
            self._rearm_skipped_stages(skipped)
            if r.get("targeted"):
                self._bump_stage_zero("targeted", r.get("targeted_freed") or 0)
            if r.get("bg_trim"):
                self._bump_stage_zero("bg", r.get("bg_freed") or 0)
            # v1.15.0 加深单学一级：按实际跑过的轮数报量；因收益衰减提前收尾而少跑
            # 不等于失败，交给 strikes 兜底
            if r.get("deepen_rounds"):
                self._bump_stage_zero("deepen", r.get("deepen_freed") or 0)
        return note

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
            if now - self.last_clean <= self._cooldown_gap():
                return False
            pred = predictive_due(self.history, cfg)
            if not pred:
                return False
            note = (f"趋势 {pred['slope_pm']:+.1f}%/分钟，预计 {pred['eta'] / 60:.1f} "
                    f"分钟后触及{pred['metric']}阈值，已提前清理")
            log(f"预防式清理 | {note}")
            self._auto_clean(now, "预防式", s, note=note, preventive=True)
            # 记住这次预防式清理预测的触阈时间：下次任意自动清理时，_note_relief 拿
            # 「距上次清理多久」和它比，短于 eta 就是没防住（v1.21.0 预防窗口自调优）
            try:
                self._predictive_eta = float(pred["eta"])
            except (TypeError, ValueError):
                self._predictive_eta = None
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
                    # 手动把档位从激进切回保守 = 想重新评估自调优，重新武装（v1.10.0）；
                    # 返回 True 说明已清闩写盘，_cfg_mtime 取写盘后的新值，省掉下一 tick 一次白重载
                    if self._sync_level_seen():
                        self._cfg_mtime = (os.path.getmtime(CONFIG_PATH)
                                           if os.path.exists(CONFIG_PATH) else mtime)
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
        顺手把这次扫描存进 proc_history：v1.11.0 起同一份 top15 采样还用来拟合
        各进程的 RSS 上升斜率，零额外成本（不额外扫盘）就能识别疑似泄漏进程。
        """
        if now - self._advice_at < self.cfg.get("advice_refresh_sec", 60):
            return
        self._advice_at = now
        try:
            top = top_processes_list(15)
            self.proc_history.append(
                (now, {int(pid): (name, int(rss)) for name, rss, pid in top}))
            self.leaks = leak_candidates(self.proc_history)
            # v1.15.0：history 一并喂给 analyze，让覆盖面自调优建议有据可依
            items = analyze(self.cfg, self.state, top, leaks=self.leaks,
                            history=self.proc_history)
            self.advice_count = len(items)
            self.advice_apply_count = len(advice_actions(items))
        except Exception:
            pass

    def _sync_level_seen(self) -> bool:
        """档位观察 + 自调优重武装（v1.10.0）：用户手动切回保守档时重新武装。

        v1.6.0/v1.9.0 的 level_adapt_done 是永久闩：自动切过一次激进后，即使内存工
        况变了、用户也手动改回了保守，也永远不再自动评估。这里每次热重载后比对「上次
        观察到的档位」与当前档位：只有 激进→保守（且确实自动切过、auto_level_adapt
        仍开着）才清闩。写盘走 update_config 读-改-写持锁，返回是否已重新武装——调用
        方据此把 _cfg_mtime 更新为写盘后的新 mtime，省掉下一 tick 一次白重载与一行多
        余日志。
        """
        prev_level = self._level_seen
        cur_level = self.cfg.get("clean_level")
        self._level_seen = cur_level
        if prev_level != "aggressive" or cur_level != "conservative":
            return False
        if not (self.cfg.get("level_adapt_done") and self.cfg.get("auto_level_adapt", True)):
            return False
        self.cfg = update_config({"level_adapt_done": False})
        log("检测到手动切回保守档，档位自调优已重新武装（再次频繁不达标会自动切激进）")
        return True

    def _maybe_adapt_level(self) -> None:
        """自调优闭环（v1.6.0/v1.9.0）：保守档不给力时自动改用激进档，整个生命周期最多一次。

        两条判据均与 advisor 3b 同源：累计清理 escalated 过半且 ≥3 次
        （frequent_escalation，保守清完仍不达标需升档），或 short_relief 过半且
        ≥3 次（frequent_short_relief，距上次清理不足 effect_min_relief_sec 即
        效果偏短）——「先温和再彻底」在这台机器上只是白跑一遍、多打扰一次，不如
        一次清到位。切换走 update_config（读-改-写持锁）并落 level_adapt_done 闩：
        用户手动切回保守会重新武装；关掉 auto_level_adapt 则完全不评估。
        任何异常只记日志，绝不让监控循环崩。
        """
        try:
            cfg = self.cfg
            if not cfg.get("auto_level_adapt", True) or cfg.get("level_adapt_done"):
                return
            esc = frequent_escalation(cfg)
            srt = frequent_short_relief(cfg)
            if not (esc or srt):
                return
            st = cfg.get("stats") or {}
            clean_cnt = int(st.get("count", 0) or 0)
            esc_cnt = int(st.get("escalated", 0) or 0)
            srt_cnt = int(st.get("short_relief", 0) or 0)
            min_relief = int(cfg.get("effect_min_relief_sec", 600) or 0)
            self.cfg = update_config({"clean_level": "aggressive",
                                      "level_adapt_done": True})
            self._level_seen = "aggressive"  # 免得下一 tick 把「自动切换」误判成用户手动切回
            crit = []
            if esc:
                crit.append(f"保守档累计 {clean_cnt} 次清理中 {esc_cnt} 次仍不达标需升档")
            if srt:
                crit.append(f"保守档累计 {clean_cnt} 次清理中 {srt_cnt} 次"
                            f"距上次清理不足 {min_relief} 秒（效果偏短）")
            log(f"自动优化 | {'；'.join(crit)}，"
                f"清理力度已自动切换为激进（可随时手动改回；改回保守档会重新武装）")
            if self.icon:
                lines = []
                if esc:
                    lines.append(f"保守档 {clean_cnt} 次清理有 {esc_cnt} 次清完仍不达标")
                if srt:
                    lines.append(f"保守档 {clean_cnt} 次清理有 {srt_cnt} 次"
                                 f"距上次清理不足 {min_relief} 秒")
                body = ("\n".join(lines)
                        + "\n已自动切换为激进档：一次清到位，少跑一遍少打扰一次\n"
                        "（右键菜单可随时改回保守；改回保守档会重新武装）")
                try:
                    self.icon.notify(
                        body,
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动档位适配异常: {e!r}")

    def _maybe_adapt_top(self) -> None:
        """定向覆盖面自调优（v1.15.0）：大户榜长期钉在同几个进程时，把大户数 +1。

        单次大户数（target_clean_top）定得太小，就会出现「每轮都在清同一批，旁边
        新长出来的中等进程一直轮不到」。判据与 advisor 3d 同源：narrow_top_coverage
        用同一份 proc_history 算集中度，达到阈值才算窄。v1.21.0 起不再限整个
        生命周期一次：仍窄就继续 +1 爬到 target_clean_top 上限（爬山），
        target_top_adapt_done 落「已加过的次数」（用户可手动改 target_clean_top，
        这个自动增量只负责把「明显偏窄」这一种情况补上）。写盘走 update_config
        （读-改-写持锁）；任何异常只记日志，绝不让监控循环崩。
        """
        try:
            cfg = self.cfg
            if not cfg.get("target_top_adapt", True):
                return
            if not narrow_top_coverage(cfg, self.proc_history):
                return
            top = int(cfg.get("target_clean_top") or 0)
            if not (0 < top < TARGET_CLEAN_TOP_MAX):
                return
            new_top = top + 1
            done = int(cfg.get("target_top_adapt_done") or 0) + 1
            self.cfg = update_config({"target_clean_top": new_top,
                                      "target_top_adapt_done": done})
            log(f"自动优化 | 定向大户榜长期集中在同几个进程（第 {done} 次撒宽，"
                f"单次大户数 {top}），已自动加大户数为 {new_top}（可随时手动改回）")
            if self.icon:
                try:
                    self.icon.notify(
                        f"定向大户榜长期集中在同几个进程\n"
                        f"单次大户数 {top} → {new_top}：一次多清几个，"
                        f"旁边新长出来的中等进程也排得上号\n"
                        f"（右键菜单可随时改回 target_clean_top）",
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动覆盖面适配异常: {e!r}")

    def _maybe_adapt_headroom(self) -> None:
        """清理提前量自调优（v1.16.0）：效果连续偏短时把判定阈值提前几个百分点。

        贴线清理救火不如提前：连续 _HEADROOM_STRIKES 次距上次自动清理不足
        effect_min_relief_sec（效果偏短），说明阈值贴常态占用太近，每回都是顶到线
        才被叫起来。把 target_headroom_pct 加上 _HEADROOM_STEP（封顶 _HEADROOM_MAX），
        判定「仍受压」时阈值先让出这么多，清理就发生在压力顶上来之前。整台机器整个
        生命周期最多提前一次，并落 headroom_adapt_done 闩（用户可手动改回
        target_headroom_pct）。写盘走 update_config（读-改-写持锁）；do_clean 每次
        现读配置，故下一次清理即用新提前量。任何异常只记日志，绝不让监控循环崩。
        """

        try:
            cfg = self.cfg
            if not cfg.get("headroom_adapt", True) or cfg.get("headroom_adapt_done"):
                return
            try:
                hr = int(cfg.get("target_headroom_pct") or 0)
            except (TypeError, ValueError):
                return
            if not (0 <= hr < _HEADROOM_MAX):
                return
            new_hr = min(hr + _HEADROOM_STEP, _HEADROOM_MAX)
            self.cfg = update_config({"target_headroom_pct": new_hr,
                                      "headroom_adapt_done": True})
            log(f"自动优化 | 清理效果连续偏短（短效 {self._relief_short_streak} 连发），"
                f"触发阈值已自动提前 {hr} → {new_hr} 个百分点（可随时手动改回）")
            if self.icon:
                try:
                    self.icon.notify(
                        f"清理效果连续偏短（短效 {self._relief_short_streak} 连发）\n"
                        f"触发阈值已自动提前 {hr} → {new_hr} 个百分点："
                        f"清理发生在压力顶上来之前\n"
                        f"（右键菜单或 mem_guard.json 可随时改回 "
                        f"target_headroom_pct）",
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动提前量适配异常: {e!r}")

    def _maybe_adapt_min_avail(self) -> None:
        """低内存下限自调优（v1.17.0）：绝对下限触发的清理连续偏短时抬高 min_avail_mb。

        min_avail_mb 是「可用物理内存低于它就清理」的绝对红线，也是唯一没有「提前」
        维度的触发输入：压力由它触发时仍是顶到线才被叫起来。连续 _MINAVAIL_STRIKES 次
        短效说明这条线本身偏低——把它抬高 _MINAVAIL_STEP MB（封顶 _MINAVAIL_MAX，且不许
        越过总物理内存的四分之一，否则变成「永远在清」），清理发生得更早更温和。整台
        机器整个生命周期最多抬高一次，并落 min_avail_adapt_done 闩（用户可手动改回
        min_avail_mb）。写盘走 update_config（读-改-写持锁）；do_clean 每次现读配置，
        故下一次清理即用新下限。任何异常只记日志，绝不让监控循环崩。
        """

        try:
            cfg = self.cfg
            if not cfg.get("min_avail_adapt", True) or cfg.get("min_avail_adapt_done"):
                return
            try:
                cur = int(cfg.get("min_avail_mb") or 0)
            except (TypeError, ValueError):
                return
            if not (0 < cur < _MINAVAIL_MAX):
                return
            new = min(cur + _MINAVAIL_STEP, _MINAVAIL_MAX)
            # 红线抬过总物理内存的四分之一就成「永远在清」：那类机器交给用户自己定夺
            if new * 1024 ** 2 > float(self.state.get("total_phys", 0) or 0) * 0.25:
                return
            self.cfg = update_config({"min_avail_mb": new,
                                      "min_avail_adapt_done": True})
            log(f"自动优化 | 低内存下限触发的清理连续偏短（短效 {self._lowmem_short_streak} 连发），"
                f"可用内存下限已自动抬高 {cur} → {new} MB（可随时手动改回）")
            if self.icon:
                try:
                    self.icon.notify(
                        f"低内存下限触发的清理连续偏短（短效 {self._lowmem_short_streak} 连发）\n"
                        f"可用内存下限已自动抬高 {cur} → {new} MB：\n"
                        f"清理发生在可用内存见底之前\n"
                        f"（右键菜单或 mem_guard.json 可随时改回 "
                        f"min_avail_mb）",
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动低内存下限适配异常: {e!r}")

    def _maybe_adapt_min_mb(self) -> None:
        """定向大户下限自调优（v1.19.0）：榜上有人却够不着下限时，把大户下限降一档。

        target_clean_min_mb 定得偏高时，长期在榜进程的 rss 怎么也过不了这条线——
        每轮定向清理都挑不中它们，只剩后台撒宽在兜底，短效与升档便接踵而至。判据与
        advisor 3g 同源：unreachable_top_threshold 用同一份 proc_history 算「大户榜
        长期钉住 + 这些名字够不着现行下限」，并要求已累计清理若干次却一次都没定向清到
        东西——是门槛太高，不是没有大户可清。整台机器整个生命周期最多降一次，并落
        min_mb_adapt_done 闩（用户可手动改回 target_clean_min_mb）。写盘走
        update_config（读-改-写持锁）；do_clean 每次现读配置，故下一轮定向清理即用
        新下限。任何异常只记日志，绝不让监控循环崩。
        """
        try:
            cfg = self.cfg
            if not unreachable_top_threshold(cfg, self.proc_history):
                return
            cur = int(cfg.get("target_clean_min_mb") or 0)
            new = max(cur // _MINMB_DIV, _MINMB_FLOOR_MB)
            self.cfg = update_config({"target_clean_min_mb": new,
                                      "min_mb_adapt_done": True})
            log(f"自动优化 | 定向大户榜长期在榜的进程够不着大户下限（现行 {cur}MB），"
                f"已自动把大户下限降为 {new}MB（可随时手动改回）")
            if self.icon:
                try:
                    self.icon.notify(
                        f"定向大户榜长期在榜的进程够不着大户下限\n"
                        f"大户下限 {cur} → {new} MB：榜上进程重新进得了定向清理的网\n"
                        f"（右键菜单或 mem_guard.json 可随时改回 "
                        f"target_clean_min_mb）",
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动大户下限适配异常: {e!r}")

    def _maybe_adapt_deepen_rounds(self) -> None:
        """定向加深轮数自调优（v1.20.0）：加深被轮数上限卡住时，把上限 +1。

        stage_deepen_rounds 是加深轮数的天花板（默认 1，clamp
        1.._DEEPEN_MAX_ROUNDS）。加深反复把上限跑满、最后一轮仍在释放、加深级
        收尾时压力仍没按住——温和阶梯是被轮数卡住的，多给加深一轮比升档全清打扰
        小。判据与 advisor 3h 同源：deepen_round_saturated 看同一份统计
        （stats.deepen_capped），爬到 _DEEPEN_MAX_ROUNDS 即停。写盘走
        update_config（读-改-写持锁）；do_clean 每次现读配置，故下一轮加深即用
        新上限。任何异常只记日志，绝不让监控循环崩。
        """
        try:
            cfg = self.cfg
            if not deepen_round_saturated(cfg):
                return
            cur = int(cfg.get("stage_deepen_rounds") or 1)
            new = min(cur + 1, _DEEPEN_MAX_ROUNDS)
            self.cfg = update_config({"stage_deepen_rounds": new,
                                      "deepen_rounds_adapt_done": new})
            log(f"自动优化 | 定向加深反复被轮数上限 {cur} 卡住（跑满仍在释放且"
                f"压力未按住），已自动把加深轮数上限提为 {new}（可随时手动改回 "
                f"stage_deepen_rounds）")
            if self.icon:
                try:
                    self.icon.notify(
                        f"定向加深反复把轮数上限跑满仍在释放、压力仍没按住，"
                        f"加深轮数上限 {cur} → {new}：多给温和加深一轮机会，"
                        f"不必改激进清理（右键菜单或 mem_guard.json 可随时改回 "
                        f"stage_deepen_rounds）",
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动加深轮数适配异常: {e!r}")

    def _maybe_adapt_predict_window(self) -> None:
        """预防窗口自调优（v1.21.0）：预防式清理没防住压力时，把预测窗口放宽一档。

        预防式清理的价值全押在「提前得够早」。连续 _PREDICT_MISS_STRIKES 次预防式
        清理之后压力仍很快复发（距上次清理比当时预测的 eta 还短，见 _note_relief），
        说明这条上升曲线比 predict_window_min 预判的更陡——把预测窗口加上
        _PREDICT_WINDOW_STEP 分钟（封顶 _PREDICT_WINDOW_MAX），预防式清理就能在
        更早的斜率上报警、提前动手，而不是等真的顶到线。判据与 advisor 3i 同源：
        predict_window_saturated 看同一份统计（stats.preventive_missed）。写盘走
        update_config（读-改-写持锁）；predictive_due 每次现读配置，故下一次预防
        判定即用新窗口。任何异常只记日志，绝不让监控循环崩。
        """
        try:
            cfg = self.cfg
            if not predict_window_saturated(cfg):
                return
            try:
                cur = int(cfg.get("predict_window_min") or 0)
            except (TypeError, ValueError):
                return
            if not (0 < cur < _PREDICT_WINDOW_MAX):
                return
            new = min(cur + _PREDICT_WINDOW_STEP, _PREDICT_WINDOW_MAX)
            self.cfg = update_config({"predict_window_min": new,
                                      "predict_window_adapt_done": new})
            log(f"自动优化 | 预防式清理连续没防住（未中 {self._predict_miss_streak} "
                f"连发），预测窗口已自动放宽 {cur} → {new} 分钟（可随时手动改回 "
                f"predict_window_min）")
            if self.icon:
                try:
                    self.icon.notify(
                        f"预防式清理连续没防住（未中 "
                        f"{self._predict_miss_streak} 连发）\n"
                        f"预测窗口 {cur} → {new} 分钟：更早的斜率上就提前清理，"
                        f"不等压力顶到阈值\n"
                        f"（右键菜单或 mem_guard.json 可随时改回 "
                        f"predict_window_min）",
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动预防窗口适配异常: {e!r}")

    def _headroom_exhausted(self) -> bool:
        """提前量适配是否已无路可走：关了、闩已落、已到上限或脏值都算用尽（v1.22.0）。

        触发阈值适配是「提前量」之后的最后一步：目标余量还有空间时，先提前判定
        阈值（更温和、只影响「仍受压」判定，不动触发线本身）；只有它确实没有空间
        了，才动 Phys/commit 两条总触发线。口径与 _maybe_adapt_headroom 的门一一
        对应：那里会直接 return 的情形，这里都算用尽。
        """
        if not self.cfg.get("headroom_adapt", True):
            return True
        if self.cfg.get("headroom_adapt_done"):
            return True
        try:
            hr = int(self.cfg.get("target_headroom_pct") or 0)
        except (TypeError, ValueError):
            return True
        return hr >= _HEADROOM_MAX

    def _maybe_adapt_threshold(self) -> None:
        """触发阈值自调优（v1.22.0）：温和阶梯用尽仍效果偏短时，把两条触发线各降一档。

        phys/commit_threshold 是两条总触发线，线内已有一整套温和阶梯（提前量 /
        加深 / 覆盖面 / 大户下限 / 低内存下限 / 预测窗口）。当「超阈值触发且效果
        偏短」连续 _THRESHOLD_STRIKES 次、且上一次清理自动升过档或在粘滞激进
        （温和手段确曾用尽，见 _post_clean_learn 与 _sticky_aggr）、提前量也已
        用尽（_headroom_exhausted），该动的就不是清理力度——激进在这台机器上也
        被证明不够——而是线本身：各降 _THRESHOLD_STEP 个百分点（各自兜底下限不许
        跌破，到 _THRESHOLD_MAX_DROPS 即停），清理发生在压力顶上来之前。判据与
        advisor 3j 同源：threshold_too_late 看同一份统计、同一份兜底口径，两处
        必须说一样的话。写盘走 update_config（读-改-写持锁）；do_clean 每次现读
        配置，故下一次触发判定即用新阈值。只发一条「MemGuard 自动优化」通知；
        任何异常只记日志，绝不让监控循环崩。
        """
        try:
            cfg = self.cfg
            if not cfg.get("auto_clean", True):
                return
            if not (self._sticky_aggr or self._last_clean_escalated):
                return
            if not self._headroom_exhausted():
                return
            if not threshold_too_late(cfg):
                return
            try:
                phys = int(cfg.get("phys_threshold") or 0)
                commit = int(cfg.get("commit_threshold") or 0)
                done = int(cfg.get("threshold_adapt_done") or 0)
            except (TypeError, ValueError):
                return
            new_phys = phys - _THRESHOLD_STEP
            new_commit = commit - _THRESHOLD_STEP
            # 兜底下限：再降就成常态清理，这类机器交给用户自己定夺
            if (new_phys < _THRESHOLD_PHYS_FLOOR
                    or new_commit < _THRESHOLD_COMMIT_FLOOR):
                return
            self.cfg = update_config({"phys_threshold": new_phys,
                                      "commit_threshold": new_commit,
                                      "threshold_adapt_done": done + 1})
            log(f"自动优化 | 超阈值触发的清理连续偏短（短效 "
                f"{self._threshold_short_streak} 连发）且温和手段已用尽，"
                f"触发阈值已自动下调 {phys}% → {new_phys}% / {commit}% → "
                f"{new_commit}%（可随时手动改回）")
            if self.icon:
                try:
                    self.icon.notify(
                        f"清理效果连续偏短（短效 {self._threshold_short_streak} 连发），"
                        f"温和手段已用尽\n"
                        f"触发阈值已自动下调：{phys}% → {new_phys}% / "
                        f"{commit}% → {new_commit}%\n"
                        f"清理发生在压力顶上来之前\n"
                        f"（右键菜单或 mem_guard.json 可随时改回 "
                        f"phys_threshold / commit_threshold）",
                        "MemGuard 自动优化",
                    )
                except Exception:
                    pass
        except Exception as e:
            log(f"自动触发阈值适配异常: {e!r}")

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
                    and now - self.last_clean > self._cooldown_gap())

    def monitor(self) -> None:
        self.maybe_reload_config()
        # 启动时清理（对标 Mem Reduct）：放监控线程内做，不阻塞托盘图标出现
        if self.cfg.get("clean_on_start"):
            try:
                log("启动时清理：按配置执行一次")
                do_clean("启动", growth_rows=self.leaks)
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
                self._maybe_adapt_top()
                self._maybe_adapt_min_mb()
                self._maybe_adapt_deepen_rounds()
                self._maybe_adapt_predict_window()
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
                           and now - self.last_clean > self._cooldown_gap())
                    if due:
                        self._auto_clean(now, "自动", s)
                else:
                    self.over_since = None
                # 触发源 2：可用物理内存低于绝对阈值（对标 Mem Reduct 的低内存触发器）
                min_avail = self.cfg.get("min_avail_mb", 0)
                if (min_avail and s["avail_phys"] < min_avail * 1024 * 1024
                        and self.cfg["auto_clean"]
                        and now - self.last_clean > self._cooldown_gap()):
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
