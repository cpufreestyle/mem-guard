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



# ---------------------------------------------------------------- 托盘常驻声明

class _RecordingThreading:
    """顶替 tray.threading 里的 Thread：只拦起后台线程，其余全走真的。

    Guard.__init__ 自己要用 threading.Event()，所以只覆盖 Thread。
    inline=True 时 target 当场跑完，用来同步断言日志。
    """

    def __init__(self, inline=False):
        import threading as _real
        self._real = _real
        self._inline = inline
        self.started = 0 if inline else []

    def Thread(self, target=None, **kw):
        if self._inline:
            target()
            self.started += 1
        else:
            self.started.append(target)
        return _NoopThread()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _NoopThread:
    def start(self):
        pass


def test_declare_tray_pin_dispatches_background_worker_once(monkeypatch):
    """托盘自愈重建后 run() 会再来一遍：注册表只该写一次，且不能卡住主循环。"""
    threading_fake = _RecordingThreading()
    monkeypatch.setattr(tray, "threading", threading_fake)
    guard = Guard()
    guard._declare_tray_pin()
    guard._declare_tray_pin()
    assert len(threading_fake.started) == 1, "重复调用只该起一个后台线程"
    assert guard._tray_pin_done is True


def test_tray_pin_worker_waits_for_the_entry(monkeypatch):
    """pystray 的 NIM_ADD 还没发生，所以必须带 wait_sec 等条目出现。"""
    calls, logs = [], []
    monkeypatch.setattr(tray, "ensure_tray_promoted",
                        lambda **kw: calls.append(kw) or "updated")
    monkeypatch.setattr(tray, "log", logs.append)
    Guard()._tray_pin_worker()
    assert calls == [{"wait_sec": tray.TRAY_PIN_WAIT}]
    assert "IsPromoted" in logs[0]


def test_tray_pin_worker_never_registers_a_placeholder(monkeypatch):
    """托盘进程自己就有图标，补占位只会多出个重复条目：allow_add 必须关着。"""
    calls = []
    monkeypatch.setattr(tray, "ensure_tray_promoted", lambda **kw: calls.append(kw))
    monkeypatch.setattr(tray, "log", lambda m: None)
    Guard()._tray_pin_worker()
    assert calls[0].get("allow_add") is not True


def test_declare_tray_pin_missing_entry_still_informs(monkeypatch):
    """条目还没生成时给一句提示，让用户知道下次启动会补上。"""
    logs = []
    monkeypatch.setattr(tray, "ensure_tray_promoted", lambda **kw: "missing")
    monkeypatch.setattr(tray, "log", logs.append)
    Guard()._tray_pin_worker()
    assert len(logs) == 1
    assert "注册表" in logs[0]


def test_declare_tray_pin_already_is_silent(monkeypatch):
    """已经常驻：一句就够，不该反复刷。"""
    logs = []
    monkeypatch.setattr(tray, "ensure_tray_promoted", lambda **kw: "already")
    monkeypatch.setattr(tray, "log", logs.append)
    Guard()._tray_pin_worker()
    assert len(logs) == 1
    assert "IsPromoted" in logs[0]


def test_declare_tray_pin_swallows_registry_errors(monkeypatch):
    """注册表炸了也只记日志：托盘循环不能被声明动作带崩。"""
    def boom(**kw):
        raise OSError("registry denied")

    logs = []
    monkeypatch.setattr(tray, "ensure_tray_promoted", boom)
    monkeypatch.setattr(tray, "log", logs.append)
    monkeypatch.setattr(tray, "threading", _RecordingThreading(inline=True))
    guard = Guard()
    guard._declare_tray_pin()          # 不应抛出
    assert guard._tray_pin_done is True
    assert any("失败" in m for m in logs)


# ---------------------------------------------------------------- 后台更新检查（v1.5.0）

class _ThreadRecorder:
    """只拦 Thread：记录起了哪些后台线程，其余 threading 能力走真的。"""

    def __init__(self):
        import threading as _real
        self._real = _real
        self.started = []

    def Thread(self, target=None, **kw):
        self.started.append(target)
        return _NoopThread()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_update_check_dispatches_only_when_due(monkeypatch):
    """没到期一次检查都不该发起：弱网与 GitHub API 限流都禁不起反复查。"""
    rec = _ThreadRecorder()
    monkeypatch.setattr(tray, "threading", rec)
    monkeypatch.setattr(tray.update, "due_for_check", lambda cfg, now: False)
    monkeypatch.setattr(tray, "log", lambda m: None)
    guard = Guard()
    guard._maybe_check_update(time.time())
    assert rec.started == []
    assert guard._update_checking is False


def test_update_check_guards_in_flight_and_advances_timestamp(monkeypatch):
    """到期才发起；在途不重复发起；内存时间戳先推进，防 interval 长达 1h 时连查。"""
    rec, checks = _ThreadRecorder(), []
    monkeypatch.setattr(tray, "threading", rec)
    monkeypatch.setattr(tray.update, "due_for_check", lambda cfg, now: True)
    monkeypatch.setattr(tray.update, "check_and_notify",
                        lambda cfg, notify=None, on_ready=None:
                        checks.append(cfg) or {"ok": True})
    monkeypatch.setattr(tray, "log", lambda m: None)
    guard = Guard()
    now = 1000.0
    guard._maybe_check_update(now)
    guard._maybe_check_update(now + 1)          # 在途：不该再起一个
    assert len(rec.started) == 1, "同一时刻只该有一次更新检查在飞"
    assert guard.cfg["last_update_check"] == 1000.0

    rec.started[0]()                            # worker 当场跑完
    assert checks and checks[0] is guard.cfg
    assert guard._update_checking is False, "跑完要复位，否则再也不会检查"


def test_update_check_swallows_scheduler_errors(monkeypatch):
    """节流判断本身抛异常也不能带崩监控线程。"""
    def boom(cfg, now):
        raise RuntimeError("cfg 炸了")
    rec = _ThreadRecorder()
    monkeypatch.setattr(tray, "threading", rec)
    monkeypatch.setattr(tray.update, "due_for_check", boom)
    monkeypatch.setattr(tray, "log", lambda m: None)
    guard = Guard()
    guard._maybe_check_update(time.time())      # 不应抛出
    assert rec.started == []


def test_update_worker_survives_failure(monkeypatch):
    """检查失败只记日志，托盘继续跑。"""
    logs = []
    monkeypatch.setattr(tray, "log", logs.append)

    def boom(*a, **kw):
        raise ConnectionError("no route")
    monkeypatch.setattr(tray.update, "check_and_notify", boom)
    guard = Guard()
    guard._update_checking = True
    guard._update_worker()                      # 不应抛出
    assert guard._update_checking is False
    assert any("更新检查异常" in m for m in logs)


class _NotifyIcon:
    def __init__(self):
        self.notes = []

    def notify(self, msg, title=None):
        self.notes.append((msg, title))


def test_notify_update_goes_through_icon(monkeypatch):
    guard = Guard()
    guard.icon = _NotifyIcon()
    guard._notify_update("发现新版本 v9.9.9", "MemGuard 更新")
    assert guard.icon.notes == [("发现新版本 v9.9.9", "MemGuard 更新")]

    guard.icon = None                           # 图标还没就绪：静默跳过
    guard._notify_update("x", "y")              # 不应抛出


def test_stop_for_update_releases_icon_and_stops_monitor():
    """安装前必须停监控、收图标，否则目标 exe 被占用、引导批处理覆盖失败。"""
    class _StopIcon:
        def __init__(self):
            self.stopped = False
        def stop(self):
            self.stopped = True
    guard = Guard()
    guard.icon = _StopIcon()
    guard._stop_for_update()
    assert guard.stop.is_set()
    assert guard.icon.stopped is True
