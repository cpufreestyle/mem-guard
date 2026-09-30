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

