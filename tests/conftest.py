# -*- coding: utf-8 -*-
"""pytest 公共配置：把仓库根加入 sys.path，使测试可以直接 `import memguard`。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402  与上面同一理由：先注入路径再导入包

from memguard import tray as _tray  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_hotkeys(monkeypatch):
    """任何测试都不许把真热键注册到桌面上。

    Guard 会把 Ctrl+Alt+M 等键真注册进用户桌面，回调还是 do_clean——跑测试时
    用户手滑按一下就是一次真清理。这里统一换成记录器；真实的注册 / 派发 / 解
    注册由 test_hotkeys.py 用注入的 register/unregister/poll 覆盖。

    返回监听器实例列表，测试据此断言「起了几条、带的什么回调、停了没有」。
    """
    instances = []

    class _RecordingListener:
        def __init__(self, specs, callbacks=None, poll_sec=0.25):
            self.specs = dict(specs or {})
            self.callbacks = dict(callbacks or {})
            self.started = False
            self.stopped = False
            self.failures = []
            instances.append(self)

        def start(self):
            self.started = True
            return self

        def stop(self, timeout=2.0):
            self.stopped = True

        def actions(self):
            return list(self.specs.items())

    monkeypatch.setattr(_tray, "HotkeyListener", _RecordingListener)
    return instances


@pytest.fixture(autouse=True)
def no_idle_suppression(monkeypatch):
    """测试里的通知一律当「用户在场」处理。

    v1.24.0 起 `Guard._notify_balloon` 会在用户离开超过 notify_idle_sec 时只记账
    不弹气泡——跑测试的机器完全可能已经空闲几小时（CI / 挂机的开发机都是），
    于是断言「通知送到了图标」的用例会成片假红。这里统一把判据扳回「在场」，
    需要验证静默本身的用例（test_idle.py）在自己的 monkeypatch 里覆盖即可。
    """
    monkeypatch.setattr(_tray, "is_user_away", lambda sec: False)

@pytest.fixture(autouse=True)
def no_real_message_box(monkeypatch):
    """测试期间不弹出真实 MessageBox 窗口。"""
    from memguard import ui, menu
    monkeypatch.setattr(ui, "message_box", lambda *a, **k: None)
    monkeypatch.setattr(menu, "message_box", lambda *a, **k: None)
