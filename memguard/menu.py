# -*- coding: utf-8 -*-
"""
托盘菜单：菜单项树构建与全部菜单回调。

回调需要读写 Guard 的运行状态（配置 / 历史采样 / 图标句柄 / 退出事件），故以 guard 实例为参数，
不反向导入 tray，保持依赖单向：… ← menu ← tray。
"""
from __future__ import annotations

import os
import threading

import pystray

from .advisor import analyze
from .autostart import autostart_enabled, install_autostart, remove_autostart
from .clean import do_clean, top_processes_list
from .config import LOG_PATH, __version__, gb, log, update_config
from .diag import export_diagnostics
from .ui import (apply_advice_actions, show_advice_window, show_top_window,
                show_trend_window)
from .update import fetch_latest_release, install_latest
from .winapi import is_admin


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

    def on_toggle_level_adapt(icon, item) -> None:
        guard.cfg = update_config(
            {"auto_level_adapt": not bool(guard.cfg.get("auto_level_adapt", True))})
        log(f"保守档不给力自动改激进 -> {'开' if guard.cfg['auto_level_adapt'] else '关'}")

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

    def line_stats(_):
        st = guard.cfg.get("stats") or {}
        esc = int(st.get('escalated', 0))
        tgt = int(st.get('targeted', 0))
        prev = int(st.get('preventive', 0))
        srt = int(st.get('short_relief', 0))
        bgt = int(st.get('bg_trimmed', 0))
        stk = int(st.get('sticky', 0))
        dpn = int(st.get('deepen', 0))
        base = f"累计清理 {int(st.get('count', 0))} 次   释放 {gb(int(st.get('freed', 0)))}"
        return (base + (f"（升档 {esc} 次）" if esc else "")
                + (f"（定向 {tgt} 次）" if tgt else "")
                + (f"（加深 {dpn} 次）" if dpn else "")
                + (f"（预防 {prev} 次）" if prev else "")
                + (f"（短效 {srt} 次）" if srt else "")
                + (f"（后台 {bgt} 次）" if bgt else "")
                + (f"（粘滞 {stk} 次）" if stk else ""))

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
        show_advice_window(guard.cfg)

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
                            leaks=guard.leaks)
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
        path = export_diagnostics()
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
        pystray.MenuItem("定时清理", scheduled_menu()),
        pystray.MenuItem("低内存触发", min_avail_menu()),
        pystray.MenuItem("启动时清理", on_toggle_clean_on_start,
                         checked=lambda i: bool(guard.cfg.get("clean_on_start"))),
        pystray.MenuItem("清理未达标自动升档", on_toggle_escalate,
                         checked=lambda i: bool(guard.cfg.get("escalate_clean", True))),
        pystray.MenuItem("定向清理内存大户", on_toggle_target_clean,
                         checked=lambda i: bool(guard.cfg.get("target_clean", True))),
        pystray.MenuItem("后台进程工作集清理", on_toggle_bg_trim,
                         checked=lambda i: bool(guard.cfg.get("bg_trim", True))),
        pystray.MenuItem("保守档不给力自动改激进", on_toggle_level_adapt,
                         checked=lambda i: bool(guard.cfg.get("auto_level_adapt", True))),
        pystray.MenuItem("持续高压粘滞激进", on_toggle_sticky,
                         checked=lambda i: bool(guard.cfg.get("sticky_aggressive", True))),
        pystray.MenuItem("阶段自学习(跳过无效阶段)", on_toggle_stage_learn,
                         checked=lambda i: bool(guard.cfg.get("stage_learn", True))),
        pystray.MenuItem("定向清理加深(不达标前再撒宽一轮)", on_toggle_stage_deepen,
                         checked=lambda i: bool(guard.cfg.get("stage_deepen", True))),
        pystray.MenuItem("趋势预防式清理", on_toggle_predict_clean,
                         checked=lambda i: bool(guard.cfg.get("predict_clean", True))),
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
        pystray.MenuItem("立即更新到最新版", on_update_now),
        pystray.MenuItem("自动更新（后台检查）", on_toggle_auto_update,
                         checked=lambda i: bool(guard.cfg.get("auto_update", True))),
        pystray.MenuItem("下载后自动安装", on_toggle_auto_install,
                         checked=lambda i: bool(guard.cfg.get("auto_install", False))),
        pystray.MenuItem("退出", on_quit),
    )
