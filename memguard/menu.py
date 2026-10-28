# -*- coding: utf-8 -*-
"""
托盘菜单：菜单项树构建与全部菜单回调。

回调需要读写 Guard 的运行状态（配置 / 历史采样 / 图标句柄 / 退出事件），故以 guard 实例为参数，
不反向导入 tray，保持依赖单向：… ← menu ← tray。
"""
from __future__ import annotations

import os
import threading
import time

import pystray

from .advisor import analyze
from . import adapters
from . import hotkeys
from .autostart import autostart_enabled, install_autostart, remove_autostart
from .clean import do_clean, kill_process, kill_protect_reason, top_processes_list
from .config import LOG_PATH, __version__, gb, log, update_config
from .diag import export_diagnostics
from .ui import (apply_advice_actions, confirm_box, message_box,
                show_advice_window, show_top_window, show_trend_window)
from .update import fetch_latest_release, install_latest
from .winapi import is_admin


def _kill_one(guard, pid: int, name: str) -> None:
    """托盘子菜单点选后的单进程结束流程：确认框 -> kill_process -> 汇报 -> 刷新。

    提升到模块级与 ui.run_kill 同一个理由：闭包在 build_menu 里，测试碰不到，
    而这条路径直接决定用户机器上少不少一个进程。
    """
    cfg = guard.cfg
    if cfg.get("process_kill_confirm", True) and not confirm_box(
            "MemGuard - 结束进程", f"确定要结束进程 {name} (PID {pid}) 吗？"):
        return
    r = kill_process(pid, name, cfg)
    message_box("MemGuard - 结束结果", r.get("msg", "已完成"))
    guard.refresh()


# 托盘子菜单「结束进程（内存占用 Top8）」的槽位数与快照有效期（v1.29.0）
_KILL_SLOT_COUNT = 8
_KILL_SNAPSHOT_TTL = 0.5


class _KillSlots:
    """Top8 子菜单的实时行：菜单展开那一刻现取进程表，而不是建树时定死。

    旧实现把 PID 直接写进 MenuItem，`build_menu` 只在托盘图标创建时跑一次——
    MemGuard 一开机就是几天，子菜单里全是启动时的旧 PID，点谁都只得到
    「已经找不到这个进程（可能刚退出）」（真机日志 2026-10-27 20:02 正是如此）。
    现在固定 8 个槽位，文案 / 是否可点 / 点下去杀谁都在展开时按快照求值。

    快照缓存 0.5 秒：一次展开里 8 个槽位连续求值只扫一遍进程表；点击动作读的是
    同一份快照（不再重扫），保证「点到的那一行就是刚才显示的那一行」。
    """

    def __init__(self, guard, count: int = _KILL_SLOT_COUNT):
        self.guard = guard
        self.count = count
        self._rows = []
        self._at = 0.0

    def rows(self) -> list:
        """当前快照 [(name, rss, pid), ...]，带 TTL 缓存。"""
        now = time.monotonic()
        if now - self._at > _KILL_SNAPSHOT_TTL:
            try:
                self._rows = list(top_processes_list(self.count))
            except Exception:
                self._rows = []
            self._at = now
        return self._rows

    def label(self, idx: int) -> str:
        rows = self.rows()
        if idx >= len(rows):
            return "(无)"
        name, rss, pid = rows[idx]
        text = f"{name} ({rss / 1024 / 1024:.0f}MB, PID {pid})"
        return f"{text} · 受保护" if kill_protect_reason(name) else text

    def enabled(self, idx: int) -> bool:
        if not bool(self.guard.cfg.get("process_kill_enabled", True)):
            return False
        rows = self.rows()
        if idx >= len(rows):
            return False
        return kill_protect_reason(rows[idx][0]) is None

    def kill(self, idx: int) -> None:
        # 用展开时定下的那份快照：重扫可能让同一个下标换成另一个进程。
        rows = self._rows or self.rows()
        if idx >= len(rows):
            return
        name, _rss, pid = rows[idx]
        _kill_one(self.guard, pid, name)


def _kill_submenu(guard):
    """托盘子菜单：列出内存占用 Top8，点一项即结束该进程。

    受保护的进程（vmmem 这类虚拟机宿主、SYSTEM 核心组件）仍然列出来，但做成
    不可点的一行并标上「受保护」——它们占着榜首，从列表里删掉只会让人以为
    MemGuard 没看见；而做成能点的按钮更糟：点了只会得到一句失败。

    槽位固定、内容实时（见 _KillSlots），菜单不需要在托盘刷新时重建。
    """
    slots = _KillSlots(guard)
    items = []
    for i in range(slots.count):
        items.append(pystray.MenuItem(
            lambda item, i=i: slots.label(i),
            _slot_action(slots, i),
            enabled=lambda item, i=i: slots.enabled(i)))
    return pystray.Menu(*items)


def _slot_action(slots: "_KillSlots", idx: int):
    """pystray 的动作必须是 (icon, item) 两参数——工厂函数避免闭包下标串掉。"""
    def act(icon, item):
        slots.kill(idx)
    return act


def build_menu(guard) -> pystray.Menu:
    """构建托盘右键菜单。

    guard 为 tray.Guard 实例（此处不做类型注解以避免循环导入）。

    所有配置读写都走 ``guard.cfg`` 而不是在闭包里缓存字典引用：监控线程热重载时
    ``guard.cfg`` 会被整体替换成新字典，缓存旧引用会让菜单读到过期值，更糟的是
    勾选动作会把旧值整份写回 mem_guard.json，覆盖掉用户刚改的配置。
    """

    # -- 菜单回调 ------------------------------------------------

    def on_clean_now(icon=None, item=None) -> None:
        try:
            r = do_clean("手动", growth_rows=guard.leaks)
        except Exception as e:
            log(f"手动清理异常: {e!r}")
            if icon:
                icon.notify(f"清理异常：{e}", "MemGuard")
            return
        if not r["ok"]:
            icon.notify(r["msg"], "MemGuard")
            return
        if r.get("stats"):
            guard.cfg["stats"] = r["stats"]   # 与 do_clean 的落盘保持一致，菜单立即刷新
        freed = max(r["freed"], 0)
        lvl_txt = "激进" if r.get("level") == "aggressive" else "保守"
        if r.get("escalated"):
            lvl_txt += "（自动升档）"
        top3 = top_processes_list(3)
        top_txt = "\n".join(f"  {n} {rss / 1024 ** 3:.2f}GB" for n, rss, _ in top3)
        tgt = r.get("targeted") or []
        tgt_txt = "\n".join(f"  {n} {rss / 1024 ** 3:.2f}GB" for n, rss, _ in tgt)
        body = (f"释放 {gb(freed)}  可用物理 {gb(r['after']['avail_phys'])}（{lvl_txt}档）\n"
                f"当前占用 Top3:\n{top_txt}")
        if tgt:
            body += f"\n定向清理大户:\n{tgt_txt}"
        if r.get("bg_trim"):
            body += f"\n后台进程工作集: {r["bg_trim"]} 个"
        icon.notify(
            body,
            "MemGuard 清理完成",
        )
        guard.refresh()

    def on_overview(icon=None, item=None) -> None:
        guard.open_overview()

    def on_top(icon=None, item=None) -> None:
        show_top_window()

    def on_toggle_auto(icon, item) -> None:
        guard.cfg = update_config({"auto_clean": not guard.cfg["auto_clean"]})
        log(f"自动清理 -> {'开启' if guard.cfg['auto_clean'] else '关闭'}")

    def on_open_log(icon=None, item=None) -> None:
        try:
            os.startfile(LOG_PATH)
        except Exception:
            pass

    def on_quit(icon, item) -> None:
        guard.stop.set()
        icon.stop()

    def _preset(phys, commit):
        def setter(icon, item):
            guard.cfg = update_config({"phys_threshold": phys, "commit_threshold": commit})
        return setter

    def _set_cooldown(minutes):
        def setter(icon, item):
            guard.cfg = update_config({"cooldown": minutes * 60})
        return setter

    def _set_level(level):
        def setter(icon, item):
            guard.cfg = update_config({"clean_level": level})
            log(f"清理档位 -> {'激进' if level == 'aggressive' else '保守'}")
        return setter

    # -- 清理区域 / 触发方式（v1.4.0，对标 WinMemoryCleaner / Mem Reduct）--

    def _toggle_area(key, label):
        def setter(icon, item):
            areas = dict(guard.cfg.get("clean_areas") or {})
            areas[key] = not areas.get(key, True)
            guard.cfg = update_config({"clean_areas": areas})
            log(f"清理区域[{label}] -> {'开' if areas[key] else '关'}")
        return setter

    def areas_menu():
        rows = [
            ("standby", "Standby 列表"),
            ("low_priority_standby", "低优先级 Standby"),
            ("modified", "修改页列表"),
            ("file_cache", "系统文件缓存"),
            ("working_sets", "系统工作集（仅激进档）"),
        ]
        return pystray.Menu(*[
            pystray.MenuItem(
                label, _toggle_area(k, label),
                checked=(lambda i, kk=k: bool((guard.cfg.get("clean_areas") or {}).get(kk, True))),
            )
            for k, label in rows
        ])

    def _set_scheduled(minutes):
        def setter(icon, item):
            guard.cfg = update_config({"scheduled_minutes": minutes})
            log(f"定时清理 -> {'关闭' if not minutes else f'{minutes} 分钟'}")
        return setter

    def scheduled_menu():
        choices = [(0, "关闭"), (15, "15 分钟"), (30, "30 分钟"),
                   (60, "1 小时"), (180, "3 小时")]
        return pystray.Menu(*[
            pystray.MenuItem(label, _set_scheduled(m), radio=True,
                             checked=(lambda i, mm=m: guard.cfg.get("scheduled_minutes", 0) == mm))
            for m, label in choices
        ])

    def _set_min_avail(mb):
        def setter(icon, item):
            guard.cfg = update_config({"min_avail_mb": mb})
            log(f"低内存触发 -> {'关闭' if not mb else f'{mb} MB'}")
        return setter

    def min_avail_menu():
        choices = [(0, "关闭"), (512, "512 MB"), (1024, "1 GB"),
                   (2048, "2 GB"), (4096, "4 GB")]
        return pystray.Menu(*[
            pystray.MenuItem(label, _set_min_avail(mb), radio=True,
                             checked=(lambda i, mm=mb: guard.cfg.get("min_avail_mb", 0) == mm))
            for mb, label in choices
        ])

    def on_toggle_clean_on_start(icon, item) -> None:
        guard.cfg = update_config(
            {"clean_on_start": not bool(guard.cfg.get("clean_on_start", False))})
        log(f"启动时清理 -> {'开' if guard.cfg['clean_on_start'] else '关'}")

    def on_toggle_escalate(icon, item) -> None:
        guard.cfg = update_config(
            {"escalate_clean": not bool(guard.cfg.get("escalate_clean", True))})
        log(f"清理未达标自动升档 -> {'开' if guard.cfg['escalate_clean'] else '关'}")

    def on_toggle_target_clean(icon, item) -> None:
        guard.cfg = update_config(
            {"target_clean": not bool(guard.cfg.get("target_clean", True))})
        log(f"定向清理内存大户 -> {'开' if guard.cfg['target_clean'] else '关'}")

    # -- 自适应开关：文案 / 键名 / 勾选态全部来自 adapters.ADAPTERS --
    # 新增一条自适应只在注册表加一行，菜单不用再补回调、文案、checked lambda
    # 这三处——以前它们是最容易漏同步的。
    def _toggle_adapter(key):
        def setter(icon, item) -> None:
            guard.cfg = update_config({key: not bool(guard.cfg.get(key, True))})
            log(f"{adapters.require(key).label} -> "
                f"{'开' if guard.cfg.get(key, True) else '关'}")
        return setter

    def adapter_menu_items():
        return [
            pystray.MenuItem(a.label, _toggle_adapter(a.key),
                             checked=(lambda i, k=a.key: bool(guard.cfg.get(k, True))))
            for a in adapters.ADAPTERS
        ]

    def on_reset_adapters(icon=None, item=None) -> None:
        """一键复位所有自适应闩：抹掉「已经自动调优过一次」这段记忆。

        只清闩、不动别的配置——用户手动调过的档位 / 阈值 / 大户数全部保住。
        一条都没触发过时这件事没有意义，只写日志、不弹气泡，与程序其它地方
        「无事不打扰」的口径一致。
        """
        status = adapters.reset_status(guard.cfg)
        if status == "自适应闩已全部复位":
            log("复位自适应闩 | " + status + "，无需处理")
            return
        guard.cfg = update_config(adapters.reset_patch())
        log("复位自适应闩 | " + status + " → 已全部复位")

    def on_toggle_sticky(icon, item) -> None:
        guard.cfg = update_config(
            {"sticky_aggressive": not bool(guard.cfg.get("sticky_aggressive", True))})
        log(f"持续高压粘滞激进 -> {'开' if guard.cfg['sticky_aggressive'] else '关'}")

    def on_toggle_stage_learn(icon, item) -> None:
        guard.cfg = update_config(
            {"stage_learn": not bool(guard.cfg.get("stage_learn", True))})
        log(f"阶段自学习(跳过无效阶段) -> {'开' if guard.cfg['stage_learn'] else '关'}")

    def on_toggle_stage_deepen(icon, item) -> None:
        guard.cfg = update_config(
            {"stage_deepen": not bool(guard.cfg.get("stage_deepen", True))})
        log(f"定向清理加深(不达标前再撒宽一轮) -> "
            f"{'开' if guard.cfg['stage_deepen'] else '关'}")

    def on_toggle_bg_trim(icon, item) -> None:
        guard.cfg = update_config(
            {"bg_trim": not bool(guard.cfg.get("bg_trim", True))})
        log(f"后台进程工作集清理 -> {'开' if guard.cfg['bg_trim'] else '关'}")

    def on_toggle_predict_clean(icon, item) -> None:
        guard.cfg = update_config(
            {"predict_clean": not bool(guard.cfg.get("predict_clean", True))})
        log(f"趋势预防式清理 -> {'开' if guard.cfg['predict_clean'] else '关'}")

    def on_toggle_menu_tips(icon, item) -> None:
        """v1.26.0 托盘菜单悬停说明总开关。

        只写配置：menutips 每次收到 WM_MENUSELECT 现读 menu_tips，关掉即「划过什么都不
        弹」，不需要重启托盘去解挂子类化。
        """
        guard.cfg = update_config(
            {"menu_tips": not bool(guard.cfg.get("menu_tips", True))})
        log(f"鼠标悬停显示说明 -> {'开' if guard.cfg['menu_tips'] else '关'}")

    def line_stats(_):
        st = guard.cfg.get("stats") or {}
        esc = int(st.get('escalated', 0))
        tgt = int(st.get('targeted', 0))
        prev = int(st.get('preventive', 0))
        srt = int(st.get('short_relief', 0))
        bgt = int(st.get('bg_trimmed', 0))
        stk = int(st.get('sticky', 0))
        dpn = int(st.get('deepen', 0))
        lm = int(st.get('low_mem', 0))
        pk = int(st.get('process_killed', 0))
        base = f"累计清理 {int(st.get('count', 0))} 次   释放 {gb(int(st.get('freed', 0)))}"
        return (base + (f"（升档 {esc} 次）" if esc else "")
                + (f"（定向 {tgt} 次）" if tgt else "")
                + (f"（加深 {dpn} 次）" if dpn else "")
                + (f"（预防 {prev} 次）" if prev else "")
                + (f"（短效 {srt} 次）" if srt else "")
                + (f"（后台 {bgt} 次）" if bgt else "")
                + (f"（粘滞 {stk} 次）" if stk else "")
                + (f"（低内存 {lm} 次）" if lm else "")
                + (f"（结束进程 {pk} 次）" if pk else ""))

    def on_toggle_autostart(icon, item) -> None:
        if autostart_enabled():
            ok = remove_autostart()
            log(f"取消开机自启 -> {'成功' if ok else '失败'}")
            icon.notify("已取消开机自启" if ok else "取消失败", "MemGuard")
        else:
            ok = install_autostart()
            log(f"设置开机自启 -> {'成功' if ok else '失败'}")
            icon.notify("已设置开机自启（登录时自动启动）" if ok
                        else "设置失败（需管理员权限）", "MemGuard")

    def on_trend(icon=None, item=None) -> None:
        show_trend_window(lambda: list(guard.history))

    def on_advice(icon=None, item=None) -> None:
        # 两个都传 callable：一键应用会经 update_config 换掉 guard.cfg 这个对象，
        # proc_history 也只在监控线程里长——刷新时现取才不是开窗那一刻的旧快照。
        show_advice_window(lambda: guard.cfg, lambda: guard.proc_history)

    def on_apply_advice(icon=None, item=None) -> None:
        """不弹窗、一键应用当前可落地的优化建议（与建议窗口共用同一套动作表）。

        建议是现场 analyze 出来的：菜单上那个计数由后台低频刷新，点击这一刻配置可能
        已经变了（热重载、或刚点过别的菜单项），按当前配置重新分析才不会拿过期结论
        写盘。单条失败只跳过该条；成功后只写日志、不再弹气泡——菜单项自己从「N 项」
        翻成「0 项」并置灰，就是最省打扰的反馈。
        """
        def _apply(changes):
            guard.cfg = update_config(changes)

        try:
            items = analyze(guard.cfg, guard.state, top_processes_list(15),
                            leaks=guard.leaks, history=guard.proc_history)
            done = apply_advice_actions(items, _apply)
        except Exception as e:
            log(f"优化建议一键应用异常: {e!r}")
            return
        if not done:
            log("优化建议一键应用 | 当前没有可应用的建议")
            return
        guard.advice_apply_count = max(0, guard.advice_apply_count - len(done))
        guard.advice_count = max(0, guard.advice_count - len(done))
        log("优化建议一键应用 | " + "、".join(a.get("label", "应用") for a in done))

    def on_export(icon=None, item=None) -> None:
        # 守护状态三行一并进 zip：现场报告不必再问「热键到底生没生效」
        path = export_diagnostics(
            guard._status_lines() if hasattr(guard, "_status_lines") else None)
        if path.startswith("ERR:"):
            icon.notify("诊断导出失败：" + path, "MemGuard")
        else:
            icon.notify("诊断已导出：\n" + path, "MemGuard 诊断")

    def on_check_update(icon=None, item=None) -> None:
        """后台线程查询 GitHub Release，避免联网阻塞托盘菜单。"""
        def worker() -> None:
            r = fetch_latest_release()
            if not r.get("ok"):
                log(r.get("msg", "检查更新失败"))
                if guard.icon:
                    try:
                        guard.icon.notify(r.get("msg", "检查更新失败"), "MemGuard 更新")
                    except Exception:
                        pass
                return
            if r["newer"]:
                log(f"发现新版本 {r['tag']}（当前 v{__version__}）")
                if guard.icon:
                    try:
                        guard.icon.notify(
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
                if guard.icon:
                    try:
                        guard.icon.notify(f"已是最新版本（v{__version__}）", "MemGuard 更新")
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def on_update_now(icon=None, item=None) -> None:
        """立即更新：后台下载并静默安装，成功则不返回（自动重启到新版本）。"""
        def worker() -> None:
            r = install_latest(guard.cfg)
            if r.get("ok"):
                return
            msg = r.get("msg", "更新失败")
            log(f"立即更新失败: {msg}")
            if guard.icon:
                try:
                    guard.icon.notify(msg, "MemGuard 更新")
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def on_toggle_auto_update(icon, item) -> None:
        guard.cfg = update_config(
            {"auto_update": not bool(guard.cfg.get("auto_update", True))})
        log(f"自动更新（后台检查） -> {'开' if guard.cfg['auto_update'] else '关'}")

    def on_toggle_auto_install(icon, item) -> None:
        guard.cfg = update_config(
            {"auto_install": not bool(guard.cfg.get("auto_install", False))})
        log(f"下载后自动安装 -> {'开' if guard.cfg['auto_install'] else '关'}")

    def on_toggle_verify_sha256(icon, item) -> None:
        guard.cfg = update_config(
            {"verify_update_sha256":
             not bool(guard.cfg.get("verify_update_sha256", True))})
        log(f"更新包 sha256 校验 -> "
            f"{'开' if guard.cfg['verify_update_sha256'] else '关'}")

    def on_toggle_hotkeys(icon, item) -> None:
        """v1.24.0 全局热键总开关。键位变化同样要走重启（旧的注册还占着键）。"""
        guard.cfg = update_config(
            {"hotkey_enabled": not bool(guard.cfg.get("hotkey_enabled", True))})
        on = guard.cfg["hotkey_enabled"]
        log(f"全局热键 -> {'开' if on else '关'}"
            + (f"（{hotkeys.summary(guard.cfg)}）" if on else ""))
        if hasattr(guard, "restart_hotkeys"):
            guard.restart_hotkeys()

    def on_toggle_fs_pause(icon, item) -> None:
        """v1.24.0 全屏应用期间暂停自动清理。"""
        guard.cfg = update_config(
            {"fs_pause": not bool(guard.cfg.get("fs_pause", True))})
        log(f"全屏应用时暂停自动清理 -> {'开' if guard.cfg['fs_pause'] else '关'}")
        if hasattr(guard, "reset_fullscreen_state"):
            guard.reset_fullscreen_state()

    def on_toggle_fs_protect(icon, item) -> None:
        """v1.24.0 前台进程保护：激进档也不清空用户正在用的进程。"""
        guard.cfg = update_config(
            {"fs_protect": not bool(guard.cfg.get("fs_protect", True))})
        log(f"保护前台进程（不清空工作集） -> {'开' if guard.cfg['fs_protect'] else '关'}")

    def on_toggle_process_kill(icon, item) -> None:
        """v1.25.0 关闭进程总开关：关闭后托盘子菜单与 Top10 右键全部不可用。"""
        guard.cfg = update_config(
            {"process_kill_enabled": not bool(guard.cfg.get("process_kill_enabled", True))})
        log(f"关闭进程功能 -> {'开' if guard.cfg['process_kill_enabled'] else '关'}")

    def on_toggle_process_kill_confirm(icon, item) -> None:
        """v1.25.0 结束前确认：开启后弹 Yes/On 确认框再执行 TerminateProcess。"""
        guard.cfg = update_config(
            {"process_kill_confirm": not bool(guard.cfg.get("process_kill_confirm", True))})
        log(f"结束进程前确认 -> {'开' if guard.cfg['process_kill_confirm'] else '关'}")

    def _set_hotkey(action, spec):
        def setter(icon, item):
            changes = {"hotkey_" + action: spec}
            # 同一个组合只能登记一条（重复注册会失败）：占了别的动作的键就把那条清掉，
            # 别在用户配置里留一个「设了却不生效」的影子键位
            if spec:
                for other in hotkeys.ACTIONS:
                    if other == action:
                        continue
                    cur = str(guard.cfg.get("hotkey_" + other) or "").strip().lower()
                    if cur == spec:
                        changes["hotkey_" + other] = ""
            guard.cfg = update_config(changes)
            log(f"全局热键[{hotkeys.ACTION_LABELS[action]}] -> "
                f"{hotkeys.format_hotkey(spec) if spec else '关闭'}")
            if hasattr(guard, "restart_hotkeys"):
                guard.restart_hotkeys()
        return setter

    def hotkey_menu():
        """快捷键设置（v1.24.0，对标 Mem Reduct 的键位选择）：三个动作各一组单选。"""
        groups = []
        for action in hotkeys.ACTIONS:
            rows = [pystray.MenuItem(
                "关闭", _set_hotkey(action, ""), radio=True,
                checked=(lambda i, a=action: not guard.cfg.get("hotkey_" + a)))]
            for spec in hotkeys.choice_specs()[action]:
                rows.append(pystray.MenuItem(
                    hotkeys.format_hotkey(spec), _set_hotkey(action, spec), radio=True,
                    checked=(lambda i, a=action, s=spec:
                             str(guard.cfg.get("hotkey_" + a) or "").strip().lower() == s)))
            groups.append(pystray.MenuItem(hotkeys.ACTION_LABELS[action],
                                           pystray.Menu(*rows)))
        return pystray.Menu(*groups)

    # -- 菜单结构 ------------------------------------------------

    def line_phys(_):
        s = guard.state
        return f"物理内存  {s['phys_pct']:.0f}%   ({gb(s['used_phys'])} / {gb(s['total_phys'])})"

    def line_commit(_):
        s = guard.state
        return f"提交内存  {s['commit_pct']:.0f}%   (可用 {gb(s['avail_commit'])})"

    def line_tip(_):
        return "↑ 内存不足报错看这一行"

    def preset_menu():
        return pystray.Menu(
            pystray.MenuItem("激进  物理75% / 提交85%", _preset(75, 85), radio=True,
                             checked=lambda i: guard.cfg["phys_threshold"] == 75),
            pystray.MenuItem("标准  物理85% / 提交90%", _preset(85, 90), radio=True,
                             checked=lambda i: guard.cfg["phys_threshold"] == 85),
            pystray.MenuItem("宽松  物理92% / 提交95%", _preset(92, 95), radio=True,
                             checked=lambda i: guard.cfg["phys_threshold"] == 92),
        )

    def cooldown_menu():
        choices = [(1, "1 分钟"), (5, "5 分钟"), (10, "10 分钟"), (30, "30 分钟")]
        return pystray.Menu(*[
            pystray.MenuItem(label, _set_cooldown(m), radio=True,
                             checked=(lambda i, mm=m: guard.cfg["cooldown"] == mm * 60))
            for m, label in choices
        ])

    def level_menu():
        return pystray.Menu(
            pystray.MenuItem("保守  只清缓存(最温和)", _set_level("conservative"), radio=True,
                             checked=lambda i: guard.cfg.get("clean_level") == "conservative"),
            pystray.MenuItem("激进  额外清空进程工作集", _set_level("aggressive"), radio=True,
                             checked=lambda i: guard.cfg.get("clean_level") == "aggressive"),
        )

    # -- v1.27.0 分组收纳：四个新父项，配置键一个不删、默认值一个不改 --
    # 根菜单原来 56 个根级项，日常只动五六个，其余（8 条自适应 + 4 条进阶 + 4 项更新
    # + 4 组快捷键）常年不碰却各占一行。这里只改形状：把低频项收进父项，父项本身不挂
    # 悬停说明（划过只会展开，说明窗无处安放——v1.26.0 定的口径），该说的一句话放在
    # 父项里第一条不可点的说明行上。

    def line_adapters_guide(_):
        return "自适应 = MemGuard 自己改配置（会落盘）；下面每条都能单独关掉"

    def line_kill_guide(_):
        return ("六层保护：总开关 / 确认框 / 工作集下限 / 系统进程 / 自身进程 / 确认真退出；"
                "「受保护」的行不给点")

    def line_update_guide(_):
        return "更新 = 后台查 Release + 下载；下面三项决定要不要自动装、装前验不验 sha256"

    def line_trigger_guide(_):
        return "什么时候动手、动手按什么顺序；动手线在「清理阈值」与「清理冷却」里"

    def trigger_menu():
        """触发与阶梯：定时 / 低内存 / 启动时三条额外触发线，加上不够狠时的三级阶梯。"""
        return pystray.Menu(
            pystray.MenuItem(line_trigger_guide, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("定时清理", scheduled_menu()),
            pystray.MenuItem("低内存触发", min_avail_menu()),
            pystray.MenuItem("启动时清理", on_toggle_clean_on_start,
                             checked=lambda i: bool(guard.cfg.get("clean_on_start"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("清理未达标自动升档", on_toggle_escalate,
                             checked=lambda i: bool(guard.cfg.get("escalate_clean", True))),
            pystray.MenuItem("定向清理内存大户", on_toggle_target_clean,
                             checked=lambda i: bool(guard.cfg.get("target_clean", True))),
            pystray.MenuItem("后台进程工作集清理", on_toggle_bg_trim,
                             checked=lambda i: bool(guard.cfg.get("bg_trim", True))),
        )

    def adapters_menu():
        """自适应与进阶：8 条自适应 + 复位闩 + 4 个进阶手段。"""
        return pystray.Menu(
            pystray.MenuItem(line_adapters_guide, None, enabled=False),
            pystray.Menu.SEPARATOR,
            *adapter_menu_items(),
            pystray.MenuItem(
                lambda i: "复位自适应闩（" + adapters.reset_status(guard.cfg) + "）",
                on_reset_adapters),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("持续高压粘滞激进", on_toggle_sticky,
                             checked=lambda i: bool(guard.cfg.get("sticky_aggressive", True))),
            pystray.MenuItem("阶段自学习(跳过无效阶段)", on_toggle_stage_learn,
                             checked=lambda i: bool(guard.cfg.get("stage_learn", True))),
            pystray.MenuItem("定向清理加深(不达标前再撒宽一轮)", on_toggle_stage_deepen,
                             checked=lambda i: bool(guard.cfg.get("stage_deepen", True))),
            pystray.MenuItem("趋势预防式清理", on_toggle_predict_clean,
                             checked=lambda i: bool(guard.cfg.get("predict_clean", True))),
        )

    def hotkey_fg_menu():
        """热键与前台：全局热键、键位选择、当前键位摘要，加上全屏让路与前台保护。"""
        return pystray.Menu(
            pystray.MenuItem("全局热键（一键清理/概览/自动清理）", on_toggle_hotkeys,
                             checked=lambda i: bool(guard.cfg.get("hotkey_enabled", True))),
            pystray.MenuItem("快捷键设置", hotkey_menu()),
            pystray.MenuItem(
                lambda i: hotkeys.summary(
                    guard.cfg,
                    guard.hotkey_failures() if hasattr(guard, "hotkey_failures") else ()),
                None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("全屏应用时暂停自动清理", on_toggle_fs_pause,
                             checked=lambda i: bool(guard.cfg.get("fs_pause", True))),
            pystray.MenuItem("保护前台进程（不清空工作集）", on_toggle_fs_protect,
                             checked=lambda i: bool(guard.cfg.get("fs_protect", True))),
            pystray.MenuItem("鼠标悬停显示说明", on_toggle_menu_tips,
                             checked=lambda i: bool(guard.cfg.get("menu_tips", True))),
        )

    def kill_menu():
        """结束进程：两个开关 + Top8 列表。总开关关掉时列表里每项照常置灰。"""
        return pystray.Menu(
            pystray.MenuItem(line_kill_guide, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("关闭进程功能", on_toggle_process_kill,
                             checked=lambda i: bool(guard.cfg.get("process_kill_enabled", True))),
            pystray.MenuItem("结束进程前弹确认框", on_toggle_process_kill_confirm,
                             checked=lambda i: bool(guard.cfg.get("process_kill_confirm", True))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("结束进程（内存占用 Top8）", _kill_submenu(guard)),
        )

    def update_menu():
        """更新设置：立即更新 + 三个更新相关开关（「检查更新」留在根菜单）。"""
        return pystray.Menu(
            pystray.MenuItem(line_update_guide, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即更新到最新版", on_update_now),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("自动更新（后台检查）", on_toggle_auto_update,
                             checked=lambda i: bool(guard.cfg.get("auto_update", True))),
            pystray.MenuItem("下载后自动安装", on_toggle_auto_install,
                             checked=lambda i: bool(guard.cfg.get("auto_install", False))),
            pystray.MenuItem("更新包 sha256 校验", on_toggle_verify_sha256,
                             checked=lambda i: bool(guard.cfg.get("verify_update_sha256", True))),
        )

    return pystray.Menu(
        pystray.MenuItem(line_phys, None, enabled=False),
        pystray.MenuItem(line_commit, None, enabled=False),
        pystray.MenuItem(line_tip, None, enabled=False),
        pystray.MenuItem(line_stats, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("内存概览（左键）", on_overview),
        pystray.MenuItem("立即清理", on_clean_now),
        pystray.MenuItem("内存占用 Top10", on_top),
        pystray.MenuItem("内存趋势", on_trend),
        pystray.MenuItem(lambda i: f"优化建议（{guard.advice_count} 条）", on_advice),
        pystray.MenuItem(lambda i: f"一键应用优化建议（{guard.advice_apply_count} 项）",
                         on_apply_advice,
                         enabled=lambda i: guard.advice_apply_count > 0),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("自动清理", on_toggle_auto,
                         checked=lambda i: guard.cfg["auto_clean"]),
        pystray.MenuItem("清理阈值", preset_menu()),
        pystray.MenuItem("清理力度", level_menu()),
        pystray.MenuItem("清理冷却", cooldown_menu()),
        pystray.MenuItem("清理区域", areas_menu()),
        pystray.MenuItem("触发与阶梯", trigger_menu()),
        pystray.MenuItem("自适应与进阶", adapters_menu()),
        pystray.MenuItem("热键与前台", hotkey_fg_menu()),
        pystray.MenuItem("结束进程", kill_menu()),
        pystray.MenuItem("开机自启", on_toggle_autostart,
                         checked=lambda i: autostart_enabled()),
        pystray.MenuItem(
            lambda i: "权限：管理员（可清理）" if is_admin() else "⚠ 非管理员，无法清理",
            None, enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开日志", on_open_log),
        pystray.MenuItem("导出诊断", on_export),
        pystray.MenuItem(f"检查更新（v{__version__}）", on_check_update),
        pystray.MenuItem("更新设置", update_menu()),
        pystray.MenuItem("退出", on_quit),
    )
