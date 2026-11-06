# -*- coding: utf-8 -*-
r"""窗口启动路径：隐藏 / 最小化方式拉起进程时，新建窗口不能跟着继承成最小化。

v1.29.0 的真机故障：概览窗那排按钮「点了没反应」——窗口其实开出来了，只是被
继承成最小化（rect 停在 -32000）。修复是一对动作：`_prepare_window` 在建窗后
立刻 withdraw，`_present_window` 在 mainloop 前 deiconify + lift + focus_force。
四个窗口构造入口必须共用这一对，少接一个就等于那个按钮又是死的。
"""

import inspect
import types

from memguard import ui

GB = 1024 ** 3


class _StartupRoot:
    """假根窗口：只记录显示动作的顺序，不碰真窗口。"""

    def __init__(self):
        self.actions = []
        self.scheduled = []

    def title(self, *args):
        pass

    def geometry(self, *args):
        pass

    def attributes(self, *args):
        pass

    def configure(self, *args, **kwargs):
        pass

    def protocol(self, *args, **kwargs):
        pass

    def bind(self, *args, **kwargs):
        pass

    def winfo_exists(self):
        return True

    def withdraw(self):
        self.actions.append("withdraw")

    def deiconify(self):
        self.actions.append("deiconify")

    def update_idletasks(self):
        self.actions.append("update_idletasks")

    def lift(self):
        self.actions.append("lift")

    def focus_force(self):
        self.actions.append("focus_force")

    def after(self, delay, callback=None, *args):
        if delay == 0 and callback is not None:
            self.scheduled.append(callback)
        return len(self.scheduled)

    def mainloop(self):
        for callback in self.scheduled:
            callback()


class _AnyWidget:
    """假控件：吞掉一切属性读写，让构造器只跑接线逻辑。"""

    # 这几个返回值会被解包 / 当下标用，不能一律给 None。
    _RETURNS = {"get_children": (), "children": (), "selection": (),
                "insert": "I000", "item": {}, "index": 0}

    def __init__(self, *args, **kwargs):
        self.values = {}

    def __getattr__(self, name):
        value = _AnyWidget._RETURNS.get(name)
        if value is not None:
            return lambda *args, **kwargs: value
        return lambda *args, **kwargs: None

    def __getitem__(self, key):
        return self.values.get(key)

    def __setitem__(self, key, value):
        self.values[key] = value


class _FakeTtk:
    def __getattr__(self, name):
        return _AnyWidget


def _fake_tk(root):
    return types.SimpleNamespace(Tk=lambda: root, Menu=lambda *a, **k: _AnyWidget())


def _stub_sys(monkeypatch):
    monkeypatch.setattr(ui, "get_mem", lambda: {
        "phys_pct": 55.0, "commit_pct": 60.0, "used_phys": 8 * GB, "total_phys": 16 * GB,
        "avail_phys": 8 * GB, "used_commit": 20 * GB, "total_commit": 32 * GB,
        "avail_commit": 12 * GB,
    })
    monkeypatch.setattr(ui, "top_processes_list",
                        lambda n: [("chrome.exe", 3 * GB, 1234)][:n])
    monkeypatch.setattr(ui, "top_processes", lambda n: "  1. chrome.exe 3.00GB")


def test_overview_builder_presents_window_before_mainloop(monkeypatch):
    """先藏后亮，且亮窗排在 mainloop 里：继承来的最小化状态必须被覆盖掉。"""
    root = _StartupRoot()
    _stub_sys(monkeypatch)
    monkeypatch.setattr(ui, "apply_window_icon", lambda r: None)
    monkeypatch.setattr(ui, "apply_window_theme", lambda r, t: None)
    monkeypatch.setattr(ui, "resolve_theme", lambda cfg=None: dict(ui.THEME_LIGHT))

    ui._build_overview(_fake_tk(root), _FakeTtk(), {})

    assert root.actions == ["withdraw", "deiconify", "update_idletasks",
                            "lift", "focus_force"]


def test_every_window_builder_wires_the_startup_pair():
    """四个窗口入口共用同一对：漏接哪个，哪个按钮就回到「点了没反应」。"""
    for name in ("_build_overview", "_build_top", "_build_trend", "show_advice_window"):
        src = inspect.getsource(getattr(ui, name))
        assert "_prepare_window(root)" in src, name
        assert "root.after(0, lambda: _present_window(root))" in src, name
