# -*- coding: utf-8 -*-
"""menu / tray：托盘菜单可构建，且 Guard 暴露后台建议条数缓存。"""
import pystray

from memguard.menu import build_menu
from memguard.tray import Guard


def test_build_menu_returns_menu():
    assert isinstance(build_menu(Guard()), pystray.Menu)


def test_guard_exposes_advice_count():
    guard = Guard()
    assert isinstance(guard.advice_count, int)
    assert isinstance(guard._advice_tick, int)
