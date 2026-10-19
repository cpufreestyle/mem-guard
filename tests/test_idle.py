# -*- coding: utf-8 -*-
"""
用户空闲判定（GetLastInputInfo）：人不在时不打扰。

全栈路径用假 Win32 覆盖——测试不该真的去读用户输入状态；Guard 的通知静默
在这同一个文件里断言（补发汇总、阈值 0 = 关）。
"""
import ctypes

from memguard import idle, tray, winapi
from memguard.config import normalize_config
from memguard.tray import Guard


def _install(monkeypatch, dw_time, tick, ok=True):
    def fake_get(ptr):
        if not ok:
            return False
        info = ctypes.cast(ptr, ctypes.POINTER(winapi.LASTINPUTINFO)).contents
        info.cbSize = ctypes.sizeof(winapi.LASTINPUTINFO)
        info.dwTime = dw_time
        return True

    monkeypatch.setattr(winapi.user32, "GetLastInputInfo", fake_get)
    monkeypatch.setattr(winapi.kernel32, "GetTickCount", lambda: tick)


def test_idle_seconds_measures_since_last_input(monkeypatch):
    _install(monkeypatch, dw_time=100_000, tick=145_000)
    assert idle.idle_seconds() == 45.0


def test_idle_seconds_tolerates_tick_wraparound(monkeypatch):
    """GetTickCount 约 49.7 天回绕一次：跨过回绕点也要算出正数。"""
    _install(monkeypatch, dw_time=0xFFFFFF00, tick=0x00000100)
    assert idle.idle_seconds() == 0.512


def test_idle_seconds_returns_none_when_lookup_fails(monkeypatch):
    _install(monkeypatch, dw_time=0, tick=0, ok=False)
    assert idle.idle_seconds() is None


def test_idle_seconds_swallows_unexpected_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("win32 blew up")

    monkeypatch.setattr(winapi.user32, "GetLastInputInfo", boom)
    assert idle.idle_seconds() is None, "探测失败必须按『不离开』处理，不能永久静默"


def test_is_user_away_thresholds(monkeypatch):
    _install(monkeypatch, dw_time=0, tick=300_000)      # 空闲 300 秒
    assert idle.is_user_away(0) is False, "阈值 0 = 关闭静默"
    assert idle.is_user_away(None) is False
    assert idle.is_user_away("x") is False
    assert idle.is_user_away(299) is True
    assert idle.is_user_away(300) is True
    assert idle.is_user_away(301) is False
    assert idle.is_user_away(3600) is False


def test_is_user_away_false_when_unknown(monkeypatch):
    _install(monkeypatch, dw_time=0, tick=0, ok=False)
    assert idle.is_user_away(1) is False


def test_notify_is_suppressed_while_user_is_away(monkeypatch):
    """离开期间只记账不弹；回来后第一条前缀汇总，一条都不丢。"""
    guard = Guard()
    guard.cfg = normalize_config({"notify_idle_sec": 300})
    sent = []
    monkeypatch.setattr(guard, "_send_balloon", lambda m, t: sent.append((m, t)))
    monkeypatch.setattr(tray, "is_user_away", lambda sec: True)

    guard._notify_balloon("内存已清理", "MemGuard")
    guard._notify_balloon("又一条", "MemGuard")
    assert sent == [] and guard._away_notes == 2

    monkeypatch.setattr(tray, "is_user_away", lambda sec: False)
    guard._notify_balloon("用户回来了", "MemGuard")
    assert len(sent) == 1
    assert "另有 2 条通知" in sent[0][0] and "用户回来了" in sent[0][0]
    assert guard._away_notes == 0

    guard._notify_balloon("正常一条", "MemGuard")
    assert len(sent) == 2 and "另有" not in sent[1][0]


def test_notify_not_suppressed_when_threshold_off(monkeypatch):
    """阈值 0 = 关闭静默：哪怕真的已离开半小时也照常提醒。"""
    guard = Guard()
    guard.cfg = normalize_config({"notify_idle_sec": 0})
    sent = []
    monkeypatch.setattr(guard, "_send_balloon", lambda m, t: sent.append((m, t)))
    _install(monkeypatch, dw_time=0, tick=30 * 60 * 1000)   # 已离开 30 分钟
    guard._notify_balloon("x", "y")
    assert len(sent) == 1, "阈值 0 时再空闲也照常提醒"


def test_notify_default_threshold_suppresses_after_five_minutes(monkeypatch):
    """默认 300 秒：离开 4 分钟照常提醒，离开 6 分钟起静默并记账。"""
    # conftest 的护栏把判据扳成「在场」，这里要的就是真实判据，自己装回去
    monkeypatch.setattr(tray, "is_user_away", idle.is_user_away)
    guard = Guard()
    guard.cfg = normalize_config({})
    sent = []
    monkeypatch.setattr(guard, "_send_balloon", lambda m, t: sent.append((m, t)))
    _install(monkeypatch, dw_time=0, tick=4 * 60 * 1000)
    guard._notify_balloon("刚到四分钟", "MemGuard")
    assert len(sent) == 1
    _install(monkeypatch, dw_time=0, tick=6 * 60 * 1000)
    guard._notify_balloon("离开六分钟了", "MemGuard")
    assert len(sent) == 1 and guard._away_notes == 1


def test_send_balloon_survives_missing_icon():
    guard = Guard()
    guard.icon = None
    assert guard._send_balloon("x", "y") is None

