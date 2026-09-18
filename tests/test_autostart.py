# -*- coding: utf-8 -*-
"""autostart：查询缓存语义与安装/卸载后失效（不真的执行 schtasks）。"""
import pytest

from memguard import autostart


@pytest.fixture(autouse=True)
def _reset_cache():
    autostart._invalidate_autostart_cache()
    yield
    autostart._invalidate_autostart_cache()


def test_enabled_uses_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(autostart, "_run_silent", lambda cmd: (calls.append(cmd), True)[1])
    assert autostart.autostart_enabled() is True
    assert autostart.autostart_enabled() is True                    # 命中缓存，不再执行
    assert len(calls) == 1
    assert autostart.autostart_enabled(use_cache=False) is True     # 强制实时查询
    assert len(calls) == 2


def test_install_invalidates_cache(monkeypatch):
    monkeypatch.setattr(autostart, "_run_silent", lambda cmd: True)
    assert autostart.autostart_enabled() is True
    monkeypatch.setattr(autostart, "_install_autostart_impl", lambda: True)
    calls = []
    monkeypatch.setattr(autostart, "_run_silent", lambda cmd: (calls.append(cmd), True)[1])
    assert autostart.install_autostart() is True
    autostart.autostart_enabled()          # 缓存已失效 -> 应重新查询
    assert len(calls) == 1


def test_remove_invalidates_cache(monkeypatch):
    monkeypatch.setattr(autostart, "_run_silent", lambda cmd: True)
    assert autostart.autostart_enabled() is True
    monkeypatch.setattr(autostart, "_remove_autostart_impl", lambda: True)
    calls = []
    monkeypatch.setattr(autostart, "_run_silent", lambda cmd: (calls.append(cmd), True)[1])
    assert autostart.remove_autostart() is True
    autostart.autostart_enabled()
    assert len(calls) == 1
