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

from .autostart import autostart_enabled, install_autostart, remove_autostart
from .clean import do_clean, top_processes_list
from .config import LOG_PATH, __version__, gb, log, save_config
from .diag import export_diagnostics
from .ui import show_advice_window, show_top_window, show_trend_window
from .update import fetch_latest_release
from .winapi import is_admin


def build_menu(guard) -> pystray.Menu:
    """构建托盘右键菜单。

    guard 为 tray.Guard 实例（此处不做类型注解以避免循环导入）。
    每次重建图标都会调用本函数，故能拿到热重载后的最新配置。
    """
    cfg = guard.cfg

    # -- 菜单回调 ------------------------------------------------

    def on_clean_now(icon=None, item=None) -> None:
        try:
            r = do_clean("手动")
        except Exception as e:
            log(f"手动清理异常: {e!r}")
            if icon:
                icon.notify(f"清理异常：{e}", "MemGuard")
            return
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
        guard.refresh()

    def on_top(icon=None, item=None) -> None:
        show_top_window()

    def on_toggle_auto(icon, item) -> None:
        guard.cfg["auto_clean"] = not guard.cfg["auto_clean"]
        save_config(guard.cfg)
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
            cfg["phys_threshold"] = phys
            cfg["commit_threshold"] = commit
            save_config(cfg)
        return setter

    def _set_cooldown(minutes):
        def setter(icon, item):
            cfg["cooldown"] = minutes * 60
            save_config(cfg)
        return setter

    def _set_level(level):
        def setter(icon, item):
            cfg["clean_level"] = level
            save_config(cfg)
            log(f"清理档位 -> {'激进' if level == 'aggressive' else '保守'}")
        return setter

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
                             checked=lambda i: cfg["phys_threshold"] == 75),
            pystray.MenuItem("标准  物理85% / 提交90%", _preset(85, 90), radio=True,
                             checked=lambda i: cfg["phys_threshold"] == 85),
            pystray.MenuItem("宽松  物理92% / 提交95%", _preset(92, 95), radio=True,
                             checked=lambda i: cfg["phys_threshold"] == 92),
        )

    def cooldown_menu():
        choices = [(1, "1 分钟"), (5, "5 分钟"), (10, "10 分钟"), (30, "30 分钟")]
        return pystray.Menu(*[
            pystray.MenuItem(label, _set_cooldown(m), radio=True,
                             checked=(lambda i, mm=m: cfg["cooldown"] == mm * 60))
            for m, label in choices
        ])

    def level_menu():
        return pystray.Menu(
            pystray.MenuItem("保守  只清缓存(最温和)", _set_level("conservative"), radio=True,
                             checked=lambda i: cfg.get("clean_level") == "conservative"),
            pystray.MenuItem("激进  额外清空进程工作集", _set_level("aggressive"), radio=True,
                             checked=lambda i: cfg.get("clean_level") == "aggressive"),
        )

    return pystray.Menu(
        pystray.MenuItem(line_phys, None, enabled=False),
        pystray.MenuItem(line_commit, None, enabled=False),
        pystray.MenuItem(line_tip, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("立即清理", on_clean_now),
        pystray.MenuItem("内存占用 Top10", on_top),
        pystray.MenuItem("内存趋势", on_trend),
        pystray.MenuItem(lambda i: f"优化建议（{guard.advice_count} 条）", on_advice),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("自动清理", on_toggle_auto,
                         checked=lambda i: cfg["auto_clean"]),
        pystray.MenuItem("清理阈值", preset_menu()),
        pystray.MenuItem("清理力度", level_menu()),
        pystray.MenuItem("清理冷却", cooldown_menu()),
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
        pystray.MenuItem("退出", on_quit),
    )
