# -*- coding: utf-8 -*-
"""
命令行入口：GUI 子系统退出流处理、--once、--selftest、主运行。

只负责"怎么启动"，业务实现分布在 config / winapi / clean / tray。
"""
from __future__ import annotations

import os
import sys

from .config import (
    DEFAULT_CONFIG,
    CLEAN_BLACKLIST_STEMS,
    __version__,
    _blacklist_stems,
    _norm_proc_name,
    gb,
    log,
    load_config,
    name_in_blacklist,
    normalize_config,
)
from . import foreground
from . import menutips
from .hotkeys import (action_specs, choice_clashes, choice_specs,
                     choices_are_usable, parse_hotkey)
from . import idle
import psutil
import pystray
from .clean import do_clean, kill_process, top_processes
from .advisor import analyze, format_advice
from .autostart import autostart_enabled
from .menu import build_menu
from .tray import Guard
from .ui import make_icon, message_box
from .adapters import ADAPTERS, reset_patch
from .update import (_parse_version, check_and_notify, find_checksums_asset,
                    install_latest, parse_checksums, pick_asset,
                    purge_stale_backup, sha256_of, verify_asset)
from .winapi import (
    acquire_single_instance,
    get_mem,
    is_admin,
    kernel32,
    pin_tray_to_corner,
    privilege_state,
    request_show_overview,
    set_app_user_model_id,
    user32,
)


def _hide_console() -> None:
    """隐藏随 python.exe 启动时附带的控制台窗口。

    用 pythonw.exe 启动时本就没有控制台，此函数会静默跳过；
    用 python.exe 启动（例如直接双击 .py 或未改造的旧快捷方式）时，
    在这里把黑框隐藏，托盘程序全程不出现前台窗口。
    """
    try:
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def once() -> None:
    """命令行模式：打印一次状态并执行清理，用于验证，不进托盘。"""
    s = get_mem()
    print(f"物理内存 {s['phys_pct']:.1f}%  ({gb(s['used_phys'])} / {gb(s['total_phys'])})")
    print(f"提交内存 {s['commit_pct']:.1f}%  ({gb(s['used_commit'])} / {gb(s['total_commit'])})")
    print(f"管理员权限: {is_admin()}")
    print("-" * 50)
    r = do_clean("测试")
    if not r["ok"]:
        print(r["msg"])
        return
    print(f"清理档位: {'激进' if r.get('level') == 'aggressive' else '保守'}"
          + ("（定向大户）" if r.get("targeted") else "")
          + ("（自动升档）" if r.get("escalated") else ""))
    print(f"清理前可用物理 {gb(r['before']['avail_phys'])}  提交 {r['before']['commit_pct']:.1f}%")
    print(f"清理后可用物理 {gb(r['after']['avail_phys'])}  提交 {r['after']['commit_pct']:.1f}%")
    print(f"释放 {gb(max(r['freed'], 0))}")
    print(f"明细: {r['detail']}")


def status() -> int:
    """--status：打印一次内存状态与守护状态，不起托盘、不清理。

    对标 EmptyStandbyList 那类命令行工具：脚本 / 计划任务 / 远程排查时想确认
    「程序在不在干活、热键生没生效」，不必去点托盘。内容与概览窗的守护状态行
    同源（Guard._status_lines），两边不会说不同的话。
    """
    s = get_mem()
    print(f"MemGuard v{__version__} 状态")
    print(f"  物理内存  {s['phys_pct']:5.1f}%   "
          f"({gb(s['used_phys'])} / {gb(s['total_phys'])}，可用 {gb(s['avail_phys'])})")
    print(f"  提交内存  {s['commit_pct']:5.1f}%   "
          f"({gb(s['used_commit'])} / {gb(s['total_commit'])}，可用 {gb(s['avail_commit'])})")
    idle_sec = idle.idle_seconds()
    print(f"  用户空闲  {'未知' if idle_sec is None else f'{idle_sec:.0f} 秒'}"
          f"（通知静默阈值 {load_config().get('notify_idle_sec', 300)} 秒）")
    guard = Guard()
    for line in guard._status_lines():
        print(f"  {line}")
    return 0


def selftest() -> int:
    """内置自检：验证关键功能可用，返回退出码（0=全部通过）。"""
    print(f"MemGuard 自检 (v{__version__})")
    results = []

    def check(name, fn):
        try:
            results.append((name, True, fn()))
        except Exception as e:
            results.append((name, False, repr(e)))

    check("get_mem", lambda: f"物理 {get_mem()['phys_pct']:.0f}% / 提交 {get_mem()['commit_pct']:.0f}%")
    check("make_icon", lambda: f"{make_icon(50, 85).size}")
    check("is_admin", is_admin)
    check("privilege_state", lambda: privilege_state("SeDebugPrivilege"))
    check("autostart_enabled", autostart_enabled)
    check("top_processes", lambda: f"{len(top_processes(5).splitlines())} 行")
    check("normalize_config", lambda: (
        f"非法档位->{normalize_config({'clean_level': 'xx'})['clean_level']}, "
        f"越界阈值->{normalize_config({'phys_threshold': 999})['phys_threshold']}, "
        f"白名单->{normalize_config({'user_blacklist': ['Chrome.EXE', 'chrome']})['user_blacklist']}"
    ))
    check("blacklist匹配", lambda: (
        f"csrss.exe->{_norm_proc_name('csrss.exe') in CLEAN_BLACKLIST_STEMS}, "
        f"通配符->{name_in_blacklist('Chrome.exe', _blacklist_stems(['*chrome*']))}, "
        f"不误伤->{not name_in_blacklist('notchrome.exe', _blacklist_stems(['*chrome*']))}"
    ))
    check("_parse_version", lambda: (
        f"v1.10.2 > 1.9.9 -> {_parse_version('v1.10.2') > _parse_version('1.9.9')}, "
        f"同版本 -> {_parse_version('1.3.0') == _parse_version('v1.3.0')}"
    ))
    check("update资产选择", lambda: (
        f"setup优先->{pick_asset([{'name': 'mem_guard.exe', 'browser_download_url': 'u1'}, {'name': 'MemGuard-Setup-1.5.0.exe', 'browser_download_url': 'u2'}])['name']}, "
        f"退回便携->{pick_asset([{'name': 'mem_guard.exe', 'browser_download_url': 'u1'}])['name']}, "
        f"空列表->{pick_asset([])}"
    ))
    check("advisor", lambda: (
        f"{len(analyze(normalize_config({})))} 条 / "
        f"{len(format_advice(analyze(normalize_config({}))))} 字符"
    ))
    check("build_menu", lambda: f"菜单构建成功（{type(build_menu(Guard())).__name__}）")

    def _menu_tips_probe() -> str:
        """只核对菜单悬停说明与真实菜单项的对应关系，不建窗口、不起线程。

        三条都要成立，否则说明窗会在用户划过菜单时静默失灵：
        ① 每条说明的键都能命中一个叶子项（带子菜单的父项划不出说明窗）；
        ② 每条自适应档位（adapters.label）自己就带着说明；
        ③ 至少 40 个叶子项有说明——低于这个数说明菜单大改后这里没跟上。
        分隔线不算项：v1.27.0 分组收纳后菜单里多了十条八条 - - - -，
        把它们计进分母，「43/108」这种数字就再也看不出覆盖率是升是降了。
        """
        tree = build_menu(Guard())
        leaf, popup = [], []

        def walk(items):
            for it in items:
                if it is pystray.Menu.SEPARATOR:
                    continue
                label = str(it.text)
                sub = getattr(it, "submenu", None)
                (popup if sub else leaf).append(label)
                if sub:
                    walk(sub)

        walk(tree.items)
        dead = sorted(k for k in menutips.tip_keys()
                      if not menutips.reachable(k, leaf))
        assert not dead, f"说明键在菜单里划不到: {dead}"
        on_parent = sorted(k for k in menutips.tip_keys()
                          if k in popup and k not in leaf)
        assert not on_parent, f"说明写在了带子菜单的父项上: {on_parent}"
        no_tip = [a.label for a in ADAPTERS if not a.tip]
        assert not no_tip, f"自适应档位缺说明: {no_tip}"
        covered = sum(1 for l in leaf if menutips.tip_for(l))
        assert covered >= 40, f"只有 {covered} 个叶子项有说明"
        keys = menutips.tip_keys()
        assert len(keys) == menutips.tip_count(), "tip_keys 与 tip_count 口径不一致"
        return (f"{covered}/{len(leaf)} 叶子项有说明, {len(keys)} 条键"
                f"（精确 {len(menutips.TIPS)} + 动态前缀 {len(keys) - len(menutips.TIPS)}）"
                f"全部可达")

    check("菜单悬停说明对账", _menu_tips_probe)
    check("sha256校验", lambda: (
        f"清单解析->{parse_checksums('a' * 64 + '  mem_guard.exe', 'mem_guard.exe') == 'a' * 64}, "
        f"挑清单->{find_checksums_asset([{'name': 'checksums.txt', 'browser_download_url': 'u'}])['name']}, "
        f"自身哈希->{sha256_of(__file__)[:12]}…, "
        f"缺摘要->{verify_asset(__file__, '')['skipped']}"
    ))
    check("快捷键菜单键位", lambda: (
        f"{len(choice_specs())} 组可选, "
        f"不可用->{choices_are_usable()}, 跨组撞车->{choice_clashes()}"
    ))
    check("空闲判定", lambda: (
        f"当前空闲->{idle.idle_seconds()}, "
        f"阈值0不静默->{not idle.is_user_away(0)}, "
        f"阈值10亿静默->{idle.is_user_away(10 ** 9)}"
    ))
    check("全局热键解析", lambda: (
        f"ctrl+alt+m->{parse_hotkey('ctrl+alt+m') is not None}, "
        f"无修饰->{parse_hotkey('m')}, 双主键->{parse_hotkey('ctrl+m+k')}, "
        f"非法键->{parse_hotkey('ctrl+alt+f99')}, "
        f"默认键位->{sorted(action_specs(normalize_config({})))}"
    ))
    check("全屏/前台判定", lambda: (
        f"覆盖整屏->{foreground.covers_rect((0, 0, 1920, 1080), (0, 0, 1920, 1080))}, "
        f"窗口化不算->{foreground.covers_rect((0, 0, 1000, 800), (0, 0, 1920, 1080))}, "
        f"1px缝隙算->{foreground.covers_rect((-1, -1, 1921, 1081), (0, 0, 1920, 1080))}, "
        f"当前全屏->{foreground.fullscreen_hwnd() is not None}, "
        f"前台PID->{foreground.foreground_pid()}"
    ))
    check("adapters注册表", lambda: (
        f"{len(ADAPTERS)} 条自适应 / 复位 {len(reset_patch())} 键 / "
        f"默认配置键全登记->"
        f"{all(k in DEFAULT_CONFIG for a in ADAPTERS for k in (a.key, a.latch))}"
    ))

    def _kill_probe() -> str:
        """只验证 kill_process 的保护判定，绝不真的结束任何进程。

        五条都是必然被拒绝的输入：PID 0 是系统保留、本进程 PID 触发自杀保护、
        不存在的 PID 取不到进程信息（RSS=0）走到 terminate_process 仍失败、
        名字是 vmmemWSL 时撞虚拟机宿主保护（v1.28.0）、名字是 MemCompression
        时撞 Windows 核心组件保护（v1.29.0）。后两条读的是传入的名字，不依赖
        机器上此刻到底有没有在跑 WSL / 内存压缩。
        任何一条返回 ok=True 就说明保护形同虚设。
        """
        cfg = normalize_config({})
        dead = 2 ** 22
        while psutil.pid_exists(dead):
            dead += 1
        rows = [(0, "probe.exe", "PID 0"),
                (os.getpid(), "probe.exe", "本进程"),
                (dead, "probe.exe", "不存在 PID"),
                (dead, "vmmemWSL", "虚拟机宿主"),
                (dead, "MemCompression", "核心组件")]
        rejected = []
        for pid, name, _label in rows:
            r = kill_process(pid, name, cfg)
            rejected.append(not r["ok"])
        return "全部拒绝->%s" % all(rejected)

    check("结束进程保护判定", _kill_probe)

    ok_all = all(ok for _, ok, _ in results)
    for name, ok, val in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    single = acquire_single_instance()
    print(f"  [INFO] single_instance: {'已持有锁' if single else '已有实例在运行'}")
    print("结果：" + ("全部通过" if ok_all else "存在失败项"))
    return 0 if ok_all else 1


def pin_tray_cli() -> int:
    """--pin-tray：把托盘图标固定到任务栏角区。

    IsPromoted 写进注册表后，只有重启 Explorer 才会重新排布托盘，重启会让任务栏
    闪一下、已打开的资源管理器窗口全部关闭，所以先向用户说明再动手。条目不存在时
    （程序还没起过）会先用 NIM_ADD 补一枚占位图标把条目逼出来——2026-10-01 实测：
    当前 Windows 构建上 Explorer 已不再为 NIM_ADD 建条目，首装真正可行的做法是
    让用户在系统托盘设置里手动把图标拖出折叠区一次。
    """
    print("将把 MemGuard 托盘图标固定到任务栏角区。")
    print("生效需要重启 Explorer：任务栏会闪一下，已打开的资源管理器窗口会关闭。")
    state = pin_tray_to_corner()
    if state == "pinned":
        print("完成：注册表已写入常驻标记，Explorer 已重启。")
        print("启动 MemGuard 后若图标仍在折叠区，请在任务栏托盘设置里手动拖出一次；")
        print("拖出后注册表条目生成，之后再运行本命令即可长期保持。")
        return 0
    if state == "already":
        print("无需处理：注册表已是常驻（IsPromoted=1）。")
        print("若图标仍在折叠区：首装需在托盘设置里手动拖出一次；之后重启 Explorer 即生效。")
        return 1
    if state == "missing":
        print("失败：注册表里没有本程序的托盘条目，NIM_ADD 也没能让 Explorer 建出来。")
        print("请先在系统托盘设置里手动拖出 MemGuard 图标一次，再运行本命令。")
        return 1
    if state == "restart_failed":
        print("注册表已写入，但 Explorer 重启失败或超时；请手动重启 Explorer。")
        return 1
    print("失败：当前平台不支持（仅 Windows）。")
    return 1


def check_update_cli() -> int:
    """--check-update：命令行检查一次更新，只打印，不弹窗、不开浏览器。"""
    r = check_and_notify(load_config(), notify=None)
    if not r.get("ok"):
        print(r.get("msg", "检查更新失败"))
        return 1
    if r.get("newer"):
        print(f"发现新版本 {r['tag']}（当前 v{__version__}）")
        print("可用 --update 直接安装，或右键托盘 → 立即更新到最新版")
        return 0
    print(f"已是最新版本（v{__version__}）")
    return 0


def update_cli() -> int:
    """--update：发现新版本就下载并静默安装；已最新则安静退出。

    安装路径分两种（由 Release 资产决定，见 update.py）：安装版走 Inno 静默
    升级，便携版等本进程退出后自替换，成功后都会自动拉起新版本。
    """
    r = install_latest(load_config())
    print(r.get("msg", "更新结束"))
    return 0 if r.get("ok") else 1


_USAGE = """MemGuard 用法: mem_guard [选项]

（无参数）   启动托盘常驻程序
--once      打印一次内存状态并执行一次清理，不进托盘
--selftest  运行内置自检
--show      已有实例时静默唤出内存概览窗
--check-update  检查一次 GitHub Release，只打印不弹窗
--update    发现新版本则下载并静默安装（完成后自动重启）
--pin-tray   写 IsPromoted 注册表并重启 Explorer（首装需先手动拖出图标一次，任务栏会闪一下）
--status    打印一次内存状态与守护状态（热键/全屏让路/前台保护），不起托盘也不清理
--help/-h   打印本说明
"""

_HELP_FLAGS = ("--help", "-h", "/?")
_KNOWN_ARGS = ("--once", "--selftest", "--show", "--check-update", "--status",
               "--update", "--pin-tray") + _HELP_FLAGS


def _print_usage(stream=None) -> None:
    """打印用法；不给 stream 时走 stdout。"""
    print(_USAGE.rstrip(), file=stream or sys.stdout)


def _unknown_args(argv):
    """返回 argv 里无法识别的参数（不含 argv[0]），供 main() 拒绝并退出。"""
    return [a for a in argv[1:] if a not in _KNOWN_ARGS]


def main() -> None:
    # 控制台 / CI 的默认编码可能无法表示中文（如英文 Windows 的 cp1252），
    # 统一把标准流切到 UTF-8 并容忍不可编码字符，避免打印时抛 UnicodeEncodeError。
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None:
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # 用法 / 写错的参数必须在 _hide_console() 与单实例锁之前分流：
    # 否则像 `python mem_guard.py --help` 这种会滑进 GUI 分支，
    # 控制台被藏掉、shell 看着像卡住，GUI 却常驻并持有互斥体，
    # 之后所有启动都被判成「已有实例」秒退。
    if [a for a in sys.argv[1:] if a in _HELP_FLAGS]:
        _print_usage()
        sys.exit(0)
    _unknown = _unknown_args(sys.argv)
    if _unknown:
        _print_usage(sys.stderr)
        print(f"未知参数: {' '.join(_unknown)}（可用 --help 查看用法）", file=sys.stderr)
        sys.exit(2)
    if "--once" in sys.argv:
        once()
    elif "--selftest" in sys.argv:
        # GUI 子系统（--noconsole）下没有 stdout，CI 冒烟测试需要把自检结果落盘才能看到明细
        _log_path = os.environ.get("MEMGUARD_SELFTEST_LOG")
        if _log_path and sys.stdout is None:
            try:
                sys.stdout = open(_log_path, "w", encoding="utf-8")
            except Exception:
                pass
        sys.exit(selftest())
    elif "--check-update" in sys.argv:
        sys.exit(check_update_cli())
    elif "--update" in sys.argv:
        sys.exit(update_cli())
    elif "--status" in sys.argv:
        sys.exit(status())
    elif "--pin-tray" in sys.argv:
        sys.exit(pin_tray_cli())
    else:
        # pythonw.exe 启动时没有标准输出流，兜底到空设备，避免任何 print 抛异常
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        # 隐藏 python.exe 自带的控制台窗口，托盘程序不停留在前台
        _hide_console()
        # 任务栏分组：MemGuard 的概览 / Top / 趋势 / 建议窗口在任务栏并成一个按钮，
        # 并用 System.AppUserModel.ID 与固定到任务栏的快捷方式对上号（两边要一致）。
        set_app_user_model_id()
        show_on_start = "--show" in sys.argv
        if not acquire_single_instance():
            log("检测到已有 MemGuard 实例在运行，本次启动已取消")
            if show_on_start and request_show_overview():
                # 二次启动（任务栏 / 桌面快捷方式又点了一次）：只负责把概览窗唤出来，
                # 静默退场，不再重复弹「已在运行」的提示框。
                log("已通知运行中的实例打开内存概览窗")
                sys.exit(0)
            # 用守护线程弹提示，主线程立即退出，避免互斥体句柄被卡死的进程长期持有
            message_box("MemGuard", "MemGuard 已在运行（请查看系统托盘），本次不再重复启动。")
            sys.exit(0)
        # 上一版自替换成功后 .bak 已无用；引导批处理没跑完就被杀时也会留下残留。
        # 在安装目录里放一个大型可执行文件只会让用户困惑、也容易被安全软件单独报毒。
        purge_stale_backup()
        Guard().run(show_on_start=show_on_start)
