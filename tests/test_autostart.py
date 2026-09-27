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


def test_task_xml_keeps_full_path_with_spaces():
    """回归：含空格路径必须完整落在同一 <Command>，不能被拆成 Command/Arguments。

    历史上 frozen 自启用 schtasks /TR 注册含空格路径会把路径按第一个空格拆开，
    任务开机起不来。改为 XML 导入后 <Command> 应原样包含完整路径。
    """
    exe = r'D:\ai share\repo\mem-guard\dist\mem_guard.exe'
    xml = autostart._task_xml(exe)
    assert '<Command>' + exe + '</Command>' in xml
    assert '<Arguments>' not in xml
    assert '<RunLevel>HighestAvailable</RunLevel>' in xml
    assert '<LogonType>InteractiveToken</LogonType>' in xml
    assert '<LogonTrigger>' in xml


def test_task_xml_escapes_xml_specials():
    """路径里万一含 &、<、> 要转义，避免破坏 XML。"""
    xml = autostart._task_xml(r'D:\a&b\c<d>e.exe')
    assert '&amp;' in xml and '&lt;' in xml and '&gt;' in xml
    assert '<Command>D:\\a&b\\c<d>e.exe</Command>' not in xml
