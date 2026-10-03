# -*- coding: utf-8 -*-
"""cli：启动分流——`--show` 二次启动只发信号、不抢提示框，且不会误弹窗。

只测 main() 的分流决策，不真的起托盘：Guard / 单实例锁 / 控制台隐藏全部换成记录型替身。
"""
import sys

import pytest

import memguard.cli as cli


class _RecordingGuard:
    """顶替 Guard：只记录 run(show_on_start=...) 收到的值。"""

    def __init__(self):
        self.kwargs = None

    def run(self, show_on_start=False):
        self.kwargs = show_on_start


def _quiet(monkeypatch, argv):
    """屏蔽 main() 的真实副作用：控制台窗口、AUMID、单实例锁。"""
    monkeypatch.setattr(cli, "_hide_console", lambda: None)
    monkeypatch.setattr(cli, "set_app_user_model_id", lambda: True)
    monkeypatch.setattr(cli, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(sys, "argv", list(argv))


def test_show_start_signals_running_instance(monkeypatch):
    """已有实例时 --show 只负责唤窗：发完信号静默退场，不再弹「已在运行」。"""
    _quiet(monkeypatch, ["mem_guard.py", "--show"])
    monkeypatch.setattr(cli, "acquire_single_instance", lambda: False)
    calls = []
    monkeypatch.setattr(cli, "request_show_overview",
                        lambda: calls.append("signal") or True)
    monkeypatch.setattr(cli, "message_box",
                        lambda title, text: calls.append("box"))

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert calls == ["signal"], "唤窗成功时不该再打扰用户"


def test_show_start_falls_back_to_notice_when_signal_fails(monkeypatch):
    """老版本实例不会创建唤窗事件：信号发不出去就照旧提示「已在运行」。"""
    _quiet(monkeypatch, ["mem_guard.py", "--show"])
    monkeypatch.setattr(cli, "acquire_single_instance", lambda: False)
    monkeypatch.setattr(cli, "request_show_overview", lambda: False)
    calls = []
    monkeypatch.setattr(cli, "message_box",
                        lambda title, text: calls.append("box"))

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert calls == ["box"]


def test_help_flag_prints_usage_and_exits(monkeypatch, capsys):
    """--help 必须先落到用法输出，不能滑进隐藏 GUI 分支。"""
    _quiet(monkeypatch, ["mem_guard.py", "--help"])
    guard = _RecordingGuard()
    monkeypatch.setattr(cli, "Guard", lambda: guard)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "--selftest" in capsys.readouterr().out
    assert guard.kwargs is None, "--help 不该启动托盘"


def test_help_flag_wins_over_once(monkeypatch, capsys):
    """--help 和其他参数同时出现时仍先打印用法，不执行清理。"""
    _quiet(monkeypatch, ["mem_guard.py", "--once", "--help"])
    monkeypatch.setattr(cli, "once", lambda: pytest.fail("--help 不该触发 --once"))

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "--once" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["mem_guard.py", "--helpp"],
    ["mem_guard.py", "--verbose"],
    ["mem_guard.py", "--once", "--extra"],
])
def test_unknown_flag_rejected_instead_of_hidden_gui(monkeypatch, capsys, argv):
    """写错的参数宁可退出码 2，也不要静默启动隐藏的托盘程序（幽灵实例坑）。"""
    _quiet(monkeypatch, argv)
    guard = _RecordingGuard()
    monkeypatch.setattr(cli, "Guard", lambda: guard)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "未知参数" in capsys.readouterr().err
    assert guard.kwargs is None


@pytest.mark.parametrize("argv,expected", [
    (["mem_guard.py", "--show"], True),
    (["mem_guard.py"], False),
])
def test_guard_receives_show_flag(monkeypatch, argv, expected):
    """--show 才把概览窗带出来；开机自启是无参启动，登录时不该弹窗。"""
    _quiet(monkeypatch, argv)
    guard = _RecordingGuard()
    monkeypatch.setattr(cli, "Guard", lambda: guard)

    cli.main()

    assert guard.kwargs is expected



@pytest.mark.parametrize("state,expected", [
    ("pinned", 0),
    ("already", 1),
    ("missing", 1),
    ("restart_failed", 1),
    ("unsupported", 1),
])
def test_pin_tray_exit_codes(monkeypatch, capsys, state, expected):
    """只有重启 Explorer 成功才是 0；其余情况都让调用方看见非零。"""
    _quiet(monkeypatch, ["mem_guard.py", "--pin-tray"])
    monkeypatch.setattr(cli, "pin_tray_to_corner", lambda: state)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == expected
    assert "Explorer" in capsys.readouterr().out


def test_pin_tray_warns_before_restarting_shell(monkeypatch, capsys):
    """重启外壳有代价，必须先打招呼再动手。"""
    _quiet(monkeypatch, ["mem_guard.py", "--pin-tray"])
    order = []
    monkeypatch.setattr(cli, "pin_tray_to_corner",
                        lambda: order.append("act") or "already")

    with pytest.raises(SystemExit):
        cli.main()

    assert "任务栏" in capsys.readouterr().out, "提示里必须说清任务栏会闪"
    assert order == ["act"]


def test_pin_tray_does_not_start_guard(monkeypatch):
    """--pin-tray 是维护命令，不该顺手常驻一个托盘实例。"""
    _quiet(monkeypatch, ["mem_guard.py", "--pin-tray"])
    guard = _RecordingGuard()
    monkeypatch.setattr(cli, "Guard", lambda: guard)
    monkeypatch.setattr(cli, "pin_tray_to_corner", lambda: "already")

    with pytest.raises(SystemExit):
        cli.main()

    assert guard.kwargs is None


def test_pin_tray_advertised_in_usage():
    """漏进 _KNOWN_ARGS 会被当成未知参数拒掉，_USAGE 也要写明白。"""
    assert "--pin-tray" in cli._KNOWN_ARGS
    assert "--pin-tray" in cli._USAGE


# ---------------------------------------------------------------- 更新旗标（v1.5.0）

def test_check_update_flag_prints_without_tray(monkeypatch, capsys):
    """--check-update 只打印结果：不弹气泡、不起托盘。"""
    _quiet(monkeypatch, ["mem_guard.py", "--check-update"])
    monkeypatch.setattr(cli, "load_config", lambda: {"auto_update": True})
    notifies = []
    monkeypatch.setattr(cli, "check_and_notify",
                        lambda cfg, notify=None: notifies.append(notify) or
                        {"ok": True, "newer": True, "tag": "v9.9.9"})
    guard = _RecordingGuard()
    monkeypatch.setattr(cli, "Guard", lambda: guard)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert notifies == [None], "命令行检查不该带气泡回调"
    out = capsys.readouterr().out
    assert "v9.9.9" in out and "1.5.0" in out
    assert guard.kwargs is None, "检查更新不该启动托盘"


def test_check_update_flag_latest_exits_zero(monkeypatch, capsys):
    """已是最新：退出码 0，且不出现任何新版本提示。"""
    _quiet(monkeypatch, ["mem_guard.py", "--check-update"])
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "check_and_notify",
                        lambda cfg, notify=None: {"ok": True, "newer": False})

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "已是最新版本" in capsys.readouterr().out


def test_update_flag_installs_without_tray(monkeypatch, capsys):
    """--update 把安装交给 update 模块：装不成也安静退出，不进 GUI。"""
    _quiet(monkeypatch, ["mem_guard.py", "--update"])
    monkeypatch.setattr(cli, "load_config", lambda: {})
    installs = []
    monkeypatch.setattr(cli, "install_latest",
                        lambda cfg: installs.append(cfg) or
                        {"ok": False, "msg": "已是最新版本（v1.5.0）"})
    guard = _RecordingGuard()
    monkeypatch.setattr(cli, "Guard", lambda: guard)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert installs == [{}]
    assert "已是最新版本" in capsys.readouterr().out
    assert guard.kwargs is None


def test_update_flags_advertised_in_usage():
    """两个旗标都要在 _KNOWN_ARGS 与 _USAGE 里，否则会被当未知参数拒掉。"""
    for flag in ("--check-update", "--update"):
        assert flag in cli._KNOWN_ARGS
        assert flag in cli._USAGE
