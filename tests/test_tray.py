# -*- coding: utf-8 -*-
"""tray：监控循环里两处「别白干活」的行为——建议按时间刷新、图标按需重绘。

两条都只关乎自身资源占用：前者决定多久扫一次全进程，后者决定多久向 shell
重建一次 HICON 并发一次 NIM_MODIFY。断言的是赋值次数，不是内部字段。
"""
import os
import time

from memguard import config as config_mod
import memguard.tray as tray
from memguard.tray import Guard, _GuardIcon

GB = 1024 ** 3


class FakeIcon:
    """记录 pystray 属性被赋值的次数（赋 icon 一次 = 重建图标句柄 + 通知 shell）。"""

    def __init__(self):
        self.icon_sets = 0
        self._icon = None
        self._title = ""

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value):
        self._icon = value
        self.icon_sets += 1

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value


def _guard(state):
    g = Guard()
    g.state = state
    g.icon = FakeIcon()
    return g


def _state(phys_pct=50.0, avail_phys=8 * GB, avail_commit=16 * GB):
    return {
        "total_phys": 16 * GB, "avail_phys": avail_phys, "used_phys": 8 * GB,
        "phys_pct": phys_pct, "total_commit": 32 * GB, "avail_commit": avail_commit,
        "used_commit": 16 * GB, "commit_pct": 50.0,
    }


def test_icon_republished_only_when_image_changes(monkeypatch):
    """同一张（文本, 颜色）图片对象重复取到就不赋值；数值换了才重绘。"""
    imgs = {}
    monkeypatch.setattr(tray, "make_icon", lambda pct, thr: imgs.setdefault(round(pct), object()))
    g = _guard(_state(50.0))
    g._refresh_icon()
    g._refresh_icon()
    assert g.icon.icon_sets == 1
    g.state = _state(51.0)
    g._refresh_icon()
    assert g.icon.icon_sets == 2


def test_tooltip_still_tracks_values_without_redraw(monkeypatch):
    """图标无需重绘时，鼠标提示里的可用内存仍随采样更新。"""
    monkeypatch.setattr(tray, "make_icon", lambda pct, thr: object())
    g = _guard(_state(50.0, avail_phys=8 * GB))
    g._refresh_icon()
    first = g.icon.title
    g.state = _state(50.0, avail_phys=3 * GB)
    g._refresh_icon()
    assert first != g.icon.title
    assert "3.0GB" in g.icon.title


def test_advice_refresh_is_time_gated(monkeypatch):
    """未过 advice_refresh_sec 就不再扫进程；过期后刷新并复用同一份采样。"""
    scans = []
    monkeypatch.setattr(tray, "top_processes_list",
                        lambda n=15: scans.append(n) or [("a.exe", 5 * GB, 1)])
    passed = {}

    def fake_analyze(cfg, mem=None, top=None):
        passed["top"] = top
        return [{"level": "tip"}, {"level": "warn"}]

    monkeypatch.setattr(tray, "analyze", fake_analyze)
    g = Guard()
    g.cfg["advice_refresh_sec"] = 60
    now = time.time()
    g._refresh_advice(now)
    g._refresh_advice(now + 5)
    assert len(scans) == 1, "同一刷新周期内不应重复扫描进程"
    assert g.advice_count == 2
    assert passed["top"] == [("a.exe", 5 * GB, 1)], "扫描结果应作为快照传给 analyze"
    g._refresh_advice(now + 61)
    assert len(scans) == 2


def test_advice_survives_scan_failure(monkeypatch):
    """进程扫描失败不能让监控线程抛出去，保留上一次的条数即可。"""
    def boom(n=15):
        raise OSError("snapshot failed")

    monkeypatch.setattr(tray, "top_processes_list", boom)
    g = Guard()
    g.advice_count = 7
    g._refresh_advice(time.time())
    assert g.advice_count == 7


def test_hot_reload_resets_advice_throttle(monkeypatch, tmp_path):
    """配置热重载后立刻重算建议条数，而不是最多再等一个 advice_refresh_sec。"""
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(tray, "CONFIG_PATH", str(cfg_file))
    scans = []
    monkeypatch.setattr(tray, "top_processes_list",
                        lambda n=15: scans.append(n) or [("a.exe", 5 * GB, 1)])
    monkeypatch.setattr(tray, "analyze", lambda cfg, mem=None, top=None: [{"level": "tip"}])

    guard = Guard()
    guard.cfg["advice_refresh_sec"] = 600          # 周期拉长到 10 分钟，方便验证清零
    t0 = time.time()
    os.utime(cfg_file, (t0, t0))

    guard.maybe_reload_config()
    guard._refresh_advice(t0)
    guard._refresh_advice(t0 + 1)
    assert len(scans) == 1, "同一刷新周期内不应重复扫描进程"

    os.utime(cfg_file, (t0 + 60, t0 + 60))          # 外部改了配置文件
    guard.maybe_reload_config()
    guard._refresh_advice(t0 + 1)                   # 距上次刷新才 1 秒，也应立即重算
    assert len(scans) == 2, "热重载应把建议节流计时清零"
# -*- coding: utf-8 -*-

def test_open_overview_wires_window_hooks(monkeypatch):
    """回归：概览窗口的 top/trend/advice 三个跳转 hook 必须绑定到已导入的 ui 函数。

    曾因 tray.py 漏 import，open_overview 构造 hooks 字典时就 NameError，用户点
    Top10/趋势/建议全部失灵。这里逐个替换并回调，证明每个 hook 都绑好了。
    """
    seen = {}

    def grab(hooks):
        seen.update(hooks)

    monkeypatch.setattr(tray, "show_overview_window", grab)
    monkeypatch.setattr(tray, "show_top_window",
                        lambda: seen.setdefault("hits", []).append("top"))
    monkeypatch.setattr(tray, "show_trend_window",
                        lambda _h: seen.setdefault("hits", []).append("trend"))
    monkeypatch.setattr(tray, "show_advice_window",
                        lambda _cfg: seen.setdefault("hits", []).append("advice"))

    guard = Guard()
    guard.open_overview()

    assert set(seen) >= {"clean", "top", "trend", "advice"}, seen.keys()
    seen["top"]()
    seen["trend"]()
    seen["advice"]()
    assert seen["hits"] == ["top", "trend", "advice"]


def test_guard_icon_left_click_opens_overview():
    """左键应触发注入的 open_overview 回调（对标 WinMemoryCleaner/Mem Reduct）。"""
    fired = []
    icon = _GuardIcon("MemGuard", None, "tip", menu=None,
                      on_left_click=lambda: fired.append("open"))
    icon()                          # 模拟左键：pystray 对图标调用 __call__
    assert fired == ["open"]


def test_guard_icon_left_click_swallows_errors(monkeypatch):
    """回调抛异常不能让托盘线程裸崩：记日志后吞掉，图标继续存活。"""
    def boom():
        raise RuntimeError("窗口炸了")

    logged = []
    monkeypatch.setattr(tray, "log", lambda msg: logged.append(msg))
    icon = _GuardIcon("MemGuard", None, "tip", menu=None, on_left_click=boom)
    icon()                          # 不应抛出
    assert any("概览窗口打开失败" in m for m in logged)
