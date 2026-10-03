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

    def fake_analyze(cfg, mem=None, top=None, leaks=None):
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


def test_advice_refresh_counts_applyable_actions(monkeypatch):
    """一次刷新同时给出「总条数」与「可一键应用条数」，后者供菜单项置灰。"""
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "analyze", lambda cfg, mem=None, top=None, leaks=None: [
        {"title": "自动清理已关闭",
         "action": {"label": "开启自动清理", "changes": dict(auto_clean=True)}},
        {"title": "未以管理员身份运行"},
    ])

    g = Guard()
    g._refresh_advice(time.time())
    assert g.advice_count == 2
    assert g.advice_apply_count == 1, "只有带 action 的建议才算可一键应用"


def test_hot_reload_resets_advice_throttle(monkeypatch, tmp_path):
    """配置热重载后立刻重算建议条数，而不是最多再等一个 advice_refresh_sec。"""
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(tray, "CONFIG_PATH", str(cfg_file))
    scans = []
    monkeypatch.setattr(tray, "top_processes_list",
                        lambda n=15: scans.append(n) or [("a.exe", 5 * GB, 1)])
    monkeypatch.setattr(tray, "analyze",
                        lambda cfg, mem=None, top=None, leaks=None: [{"level": "tip"}])

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

def test_scheduled_clean_respects_cooldown():
    """定时清理也守 cooldown：刚被阈值清理过就跳过，且不推进计时（冷却结束仍到期）。"""
    g = _guard(_state())
    g.cfg = config_mod.normalize_config({"scheduled_minutes": 30, "cooldown": 300})
    now = time.time()

    g.last_scheduled = now - 30 * 60          # 周期已到
    g.last_clean = now - 60                   # 但冷却内刚清过：跳过
    assert g._scheduled_due(now) is False

    g.last_clean = now - 301                  # 冷却已过：放行
    assert g._scheduled_due(now) is True

    g.last_scheduled = now - 29 * 60          # 周期没到：不到期
    assert g._scheduled_due(now) is False

    g.cfg = config_mod.normalize_config({"scheduled_minutes": 0, "cooldown": 300})
    g.last_scheduled = now - 30 * 60
    g.last_clean = now - 3600                 # 定时清理关掉了：永不到期
    assert g._scheduled_due(now) is False


# ---------------------------------------------------------------- 自动清理升档回显（v1.5.1 补测）

def test_auto_clean_notify_marks_escalation(monkeypatch):
    """自动清理补了激进一次：托盘通知要标「（自动升档）」；没升档时不误导（自动优化主链路）。"""
    switch = {"esc": True}

    def _fake_do_clean(reason="自动", low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        return {
            "ok": True,
            "freed": 3 * GB,
            "level": "aggressive" if switch["esc"] else "conservative",
            "escalated": switch["esc"],
            "detail": "-",
            "stats": {"count": 12},
            "before": {"avail_phys": 4 * GB, "commit_pct": 70.0},
            "after": {"avail_phys": 7 * GB, "commit_pct": 60.0},
        }

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])

    g = _guard(_state(phys_pct=86.0))
    notes = []
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._auto_clean(123.0, "自动", g.state)
    assert notes[0][0] == "MemGuard 自动清理"
    assert "（激进（自动升档）档）" in notes[0][1]

    switch["esc"] = False
    g._auto_clean(124.0, "自动", g.state)
    assert "自动升档" not in notes[1][1]
    assert "（保守档）" in notes[1][1]


def test_auto_clean_notify_marks_targeted(monkeypatch):
    """定向清了大户：托盘通知要列出是哪几个；没定向时不出现该段。"""
    switch = {"tgt": True}

    def _fake_do_clean(reason="自动", low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        return {
            "ok": True,
            "freed": 3 * GB,
            "level": "conservative",
            "escalated": False,
            "detail": "-",
            "stats": {"count": 12},
            "targeted": ([("big.exe", 2 * GB, 4242)] if switch["tgt"] else []),
            "before": {"avail_phys": 4 * GB, "commit_pct": 70.0},
            "after": {"avail_phys": 7 * GB, "commit_pct": 60.0},
        }

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])

    g = _guard(_state(phys_pct=86.0))
    notes = []
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._auto_clean(123.0, "自动", g.state)
    assert "定向清理大户" in notes[0][1]
    assert "big.exe" in notes[0][1]

    switch["tgt"] = False
    g._auto_clean(124.0, "自动", g.state)
    assert "定向清理大户" not in notes[1][1]


# ---------------------------------------------------------------- 档位自调优（v1.6.0：自动优化主链路）

def _adapt_cfg(**kw):
    """构造一份「保守档 + 频繁升档」的归一化配置，可按关键字覆盖单项。"""
    base = {"auto_level_adapt": True, "level_adapt_done": False,
            "stats": {"count": 10, "escalated": 6}}
    base.update(kw)
    return config_mod.normalize_config(base)


def test_auto_level_adapt_switches_to_aggressive_once(monkeypatch):
    """频繁升档 → 自动切激进并落闩：一次生效、有日志有通知；第二次调用不再动配置。"""
    changes, logs, notes = [], [], []
    monkeypatch.setattr(tray, "update_config",
                        lambda ch: changes.append(dict(ch)) or {
                            "clean_level": "aggressive", "level_adapt_done": True})
    monkeypatch.setattr(tray, "log", logs.append)

    g = _guard(_state())
    g.cfg = _adapt_cfg()
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._maybe_adapt_level()
    assert changes == [{"clean_level": "aggressive", "level_adapt_done": True}]
    assert g.cfg["clean_level"] == "aggressive"
    assert g.cfg["level_adapt_done"] is True
    assert any("自动优化" in m for m in logs)
    assert notes and notes[0][0] == "MemGuard 自动优化"

    g._maybe_adapt_level()
    assert len(changes) == 1, "闩 level_adapt_done 生效：不重复自动覆盖用户手动选择"


def test_auto_level_adapt_skips_when_off_latched_or_rare(monkeypatch):
    """开关关闭 / 已自动切过 / 升档不频繁：三种情形都不写配置（最小打扰）。"""
    changes = []
    monkeypatch.setattr(tray, "update_config",
                        lambda ch: changes.append(dict(ch)) or dict(ch))
    monkeypatch.setattr(tray, "log", lambda m: None)

    for kw in ({"auto_level_adapt": False}, {"level_adapt_done": True},
               {"stats": {"count": 10, "escalated": 2}},
               {"stats": {"count": 10, "short_relief": 2}}):
        g = _guard(_state())
        g.cfg = _adapt_cfg(**kw)
        g._maybe_adapt_level()
        assert changes == [], kw


def test_auto_level_adapt_failure_only_logs(monkeypatch):
    """update_config 抛异常只记日志：自调优失败绝不能带崩监控循环。"""
    logs = []

    def _boom(_changes):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(tray, "update_config", _boom)
    monkeypatch.setattr(tray, "log", logs.append)

    g = _guard(_state())
    g.cfg = _adapt_cfg()
    g._maybe_adapt_level()
    assert any("自动档位适配异常" in m for m in logs)


# ---------------------------------------------------------------- 清理效果闭环（v1.9.0）


def _effect_cfg(**kw):
    """构造一份开了效果度量的归一化配置，可按关键字覆盖单项。"""
    base = {"effect_track": True, "effect_min_relief_sec": 600}
    base.update(kw)
    return config_mod.normalize_config(base)


def test_auto_clean_marks_short_relief_when_last_clean_was_recent(monkeypatch):
    """距上次自动清理不足下限：本次记 low_relief，通知注明间隔多久（效果闭环）。"""
    seen = {}

    def _fake_do_clean(reason="自动", low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        seen["low_relief"] = low_relief
        return {"ok": True, "freed": 2 * GB, "level": "conservative",
                "escalated": False, "detail": "-", "stats": {"count": 4},
                "before": {}, "after": {}}

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=86.0))
    g.cfg = _effect_cfg()
    g.last_clean = 1000.0
    notes = []
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._auto_clean(1000.0 + 300, "自动", g.state)   # 距上次仅 5 分钟（<600 秒）

    assert seen["low_relief"] is True
    assert any("距上次自动清理 5.0 分钟" in t for _, t in notes)
    assert any("（偏短）" in t for _, t in notes)


def test_auto_clean_skips_effect_measure_without_prev_clean(monkeypatch):
    """上次清理来自手动/CLI（last_clean=0）：没有可对比基线，不测效果也不提间隔。"""
    seen = {}

    def _fake_do_clean(reason="自动", low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        seen["low_relief"] = low_relief
        return {"ok": True, "freed": 2 * GB, "level": "conservative",
                "escalated": False, "detail": "-", "stats": {"count": 4},
                "before": {}, "after": {}}

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=86.0))
    g.cfg = _effect_cfg()
    notes = []
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._auto_clean(1000.0, "自动", g.state)

    assert seen["low_relief"] is False
    assert all("距上次自动清理" not in t for _, t in notes)


def test_auto_clean_skips_effect_measure_when_disabled(monkeypatch):
    """effect_track 关掉：不度量间隔、不累计短效，通知里也不提这茬。"""
    seen = {}

    def _fake_do_clean(reason="自动", low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        seen["low_relief"] = low_relief
        return {"ok": True, "freed": 2 * GB, "level": "conservative",
                "escalated": False, "detail": "-", "stats": {"count": 4},
                "before": {}, "after": {}}

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=86.0))
    g.cfg = _effect_cfg(effect_track=False)
    g.last_clean = 1000.0
    notes = []
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._auto_clean(1000.0 + 60, "自动", g.state)   # 只隔 1 分钟，但度量已关

    assert seen["low_relief"] is False
    assert all("距上次自动清理" not in t for _, t in notes)


def test_auto_clean_effect_boundary_is_strictly_below_threshold(monkeypatch):
    """边界口径：正好等于下限不算短效（判据是严格小于），差 1 秒才算。"""
    seen = {}

    def _fake_do_clean(reason="自动", low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        seen["low_relief"] = low_relief
        return {"ok": True, "freed": 2 * GB, "level": "conservative",
                "escalated": False, "detail": "-", "stats": {"count": 4},
                "before": {}, "after": {}}

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=86.0))
    g.cfg = _effect_cfg()
    g.last_clean = 1000.0

    g._auto_clean(1000.0 + 600, "自动", g.state)   # 正好 600 秒
    assert seen["low_relief"] is False
    g._auto_clean(1000.0 + 600 + 599, "自动", g.state)   # 距上次 599 秒
    assert seen["low_relief"] is True


def test_auto_level_adapt_switches_on_short_relief_criterion(monkeypatch):
    """短效判据独立触发自调优：日志与通知都要点明「距上次清理不足 N 秒」。"""
    changes, logs, notes = [], [], []
    monkeypatch.setattr(tray, "update_config",
                        lambda ch: changes.append(dict(ch)) or {
                            "clean_level": "aggressive", "level_adapt_done": True})
    monkeypatch.setattr(tray, "log", logs.append)

    g = _guard(_state())
    g.cfg = _adapt_cfg(stats={"count": 10, "short_relief": 6})
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._maybe_adapt_level()
    assert changes == [{"clean_level": "aggressive", "level_adapt_done": True}]
    assert g.cfg["clean_level"] == "aggressive"
    assert any("距上次清理不足 600 秒" in m for m in logs)
    assert notes and notes[0][0] == "MemGuard 自动优化"
    assert "距上次清理不足 600 秒" in notes[0][1]


# ---------------------------------------------------------------- 趋势预防式清理（v1.8.0）

def _predict_cfg(**kw):
    """构造一份「开了预防式清理」的归一化配置，可按关键字覆盖单项。"""
    base = {"predict_clean": True, "auto_clean": True, "cooldown": 300,
            "phys_threshold": 85, "predict_window_min": 5}
    base.update(kw)
    return config_mod.normalize_config(base)


def _rise(n=8, step=10.0, end=79.0, slope_pm=1.5):
    """匀速上涨的采样历史：(t, phys, commit)，最后一个是「当前」占用。"""
    per_step = slope_pm * step / 60.0
    t0 = 1_000_000.0
    return [(t0 + i * step, end - (n - 1 - i) * per_step, 50.0) for i in range(n)]


def test_maybe_predictive_clean_notifies_with_trend_note(monkeypatch):
    """趋势即将触阈：提前清一次，通知里带「斜率 / 预计分钟 / 阈值」解释。"""
    seen = {}

    def _fake_do_clean(reason="自动", preventive=False, low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        seen["reason"] = reason
        seen["preventive"] = preventive
        return {"ok": True, "freed": 2 * GB, "level": "conservative",
                "escalated": False, "detail": "-", "stats": {"count": 3},
                "before": {}, "after": {}}

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=79.0))
    g.cfg = _predict_cfg()
    g.history = _rise()
    notes = []
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    assert g._maybe_predictive_clean(1000.0, g.state) is True
    assert seen == {"reason": "预防式", "preventive": True}
    assert notes[0][0] == "MemGuard 预防式清理"
    assert "趋势" in notes[0][1] and "分钟" in notes[0][1] and "阈值" in notes[0][1]
    assert g._warned is True, "同一次 tick 不用再弹接近阈值预警"


def test_maybe_predictive_clean_respects_cooldown(monkeypatch):
    """冷却内不提前清理：预防式也不该比普通自动清理更频繁。"""
    calls = []

    def _fake_do_clean(reason="自动", preventive=False, low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        calls.append(reason)
        return {"ok": True, "freed": 2 * GB, "level": "conservative",
                "escalated": False, "detail": "-", "stats": {"count": 3},
                "before": {}, "after": {}}

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=79.0))
    g.cfg = _predict_cfg()
    g.history = _rise()
    g.last_clean = 900.0            # 距上次清理才 100s，冷却 300s

    assert g._maybe_predictive_clean(1000.0, g.state) is False
    assert calls == []


def test_maybe_predictive_clean_skips_when_trend_flat(monkeypatch):
    """占用平稳：交给预警气泡和超阈值清理，别提前动。"""
    calls = []

    def _fake_do_clean(reason="自动", preventive=False, low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
        calls.append(reason)
        return {"ok": True, "freed": 0, "level": "conservative",
                "escalated": False, "detail": "-", "stats": {"count": 3},
                "before": {}, "after": {}}

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=79.0))
    g.cfg = _predict_cfg()
    g.history = _rise(slope_pm=0.0)

    assert g._maybe_predictive_clean(1000.0, g.state) is False
    assert calls == []


def test_maybe_predictive_clean_skips_when_disabled(monkeypatch):
    """预防开关 / 自动清理任一关闭都不提前清理（最小打扰）。"""
    for kw in ({"predict_clean": False}, {"auto_clean": False}):
        calls = []

        def _fake_do_clean(reason="自动", preventive=False, low_relief=False, growth_rows=None, sticky=False, skip=(), **kw):
            calls.append(reason)
            return {"ok": True, "freed": 0, "level": "conservative",
                    "escalated": False, "detail": "-", "stats": {"count": 3},
                    "before": {}, "after": {}}

        monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
        monkeypatch.setattr(tray, "log", lambda m: None)

        g = _guard(_state(phys_pct=79.0))
        g.cfg = _predict_cfg(**kw)
        g.history = _rise()
        assert g._maybe_predictive_clean(1000.0, g.state) is False, kw
        assert calls == [], kw


def test_maybe_predictive_clean_failure_only_logs(monkeypatch):
    """do_clean 抛异常只记日志：预防式清理失败绝不能带崩监控循环。"""
    logs = []

    def _boom(reason="自动", preventive=False, low_relief=False):
        raise RuntimeError("clean gone")

    monkeypatch.setattr(tray, "do_clean", _boom)
    monkeypatch.setattr(tray, "log", logs.append)

    g = _guard(_state(phys_pct=79.0))
    g.cfg = _predict_cfg()
    g.history = _rise()

    assert g._maybe_predictive_clean(1000.0, g.state) is False
    assert any("预防式清理异常" in m for m in logs)

# ------------------------------------------------- 自适应冷却与自调优重武装（v1.10.0）


def _cooldown_cfg(**kw):
    "构造一份开了自适应冷却的归一化配置，可按关键字覆盖单项。"
    base = {"cooldown": 300, "adaptive_cooldown": True,
            "adaptive_cooldown_floor": 30, "effect_track": True,
            "effect_min_relief_sec": 600, "scheduled_minutes": 30}
    base.update(kw)
    return config_mod.normalize_config(base)


def test_cooldown_gap_shrinks_on_short_relief_then_recovers():
    """短效复发把最小间隔减半，连续两次达标后恢复用户值（自适应冷却）。"""
    g = _guard(_state())
    g.cfg = _cooldown_cfg()

    assert g._cooldown_gap() == 300, "没短效记录：恒为用户 cooldown"

    g._note_relief(300, True)
    assert g._relief_short_streak == 1
    assert g._cooldown_gap() == 150, "短效复发：压到 cooldown 的一半"

    g._note_relief(120, True)
    assert g._cooldown_gap() == 150, "连续短效继续压缩（只减不增）"

    g._note_relief(900, False)
    assert g._cooldown_gap() == 150, "一次达标还不够：等连续 _COOLDOWN_OK_STREAK 次"
    g._note_relief(900, False)
    assert g._cooldown_gap() == 300, "连续达标：恢复完整冷却"
    assert g._relief_short_streak == 0


def test_cooldown_gap_never_exceeds_user_cooldown():
    """floor 高于用户 cooldown 也不放大：压缩只减不增，clamp 到 cooldown 以内。"""
    g = _guard(_state())
    g.cfg = _cooldown_cfg(cooldown=60, adaptive_cooldown_floor=3600)

    g._note_relief(10, True)
    assert g._cooldown_gap() == 60


def test_cooldown_gap_keeps_user_value_when_disabled():
    """adaptive_cooldown 关掉：即使有短效记录也不改最小间隔。"""
    g = _guard(_state())
    g.cfg = _cooldown_cfg(adaptive_cooldown=False)

    g._note_relief(10, True)
    assert g._relief_short_streak == 1
    assert g._cooldown_gap() == 300


def test_note_relief_ignores_missing_baseline():
    """上次清理来自手动/CLI（relief=None）：不判定好坏，冷却不被压缩。"""
    g = _guard(_state())
    g.cfg = _cooldown_cfg()

    g._note_relief(None, True)
    assert g._relief_short_streak == 0
    assert g._relief_ok_streak == 0
    assert g._cooldown_gap() == 300


def test_scheduled_due_uses_shrunk_gap_after_short_relief():
    """定时清理也走自适应冷却：短效复发后 user cooldown 内也能提前复查。"""
    g = _guard(_state())
    g.cfg = _cooldown_cfg()
    now = time.time()
    g.last_scheduled = now - 30 * 60
    g.last_clean = now - 200

    assert g._scheduled_due(now) is False, "用户冷却 300s 内：仍跳过"

    g._note_relief(200, True)
    assert g._cooldown_gap() == 150
    assert g._scheduled_due(now) is True, "压缩后 150s 已过：允许复查"


def test_sync_level_seen_rearms_latch_on_manual_switch_back(monkeypatch):
    """用户手动把档位切回保守：清 level_adapt_done 闩重新武装，只写一次盘。"""
    changes, logs = [], []
    monkeypatch.setattr(tray, "update_config",
                        lambda ch: changes.append(dict(ch)) or
                        {"clean_level": "conservative", "level_adapt_done": False})
    monkeypatch.setattr(tray, "log", logs.append)
    monkeypatch.setattr(tray.os.path, "exists", lambda p: False)

    g = _guard(_state())
    g.cfg = _adapt_cfg(clean_level="conservative", level_adapt_done=True)
    g._level_seen = "aggressive"

    assert g._sync_level_seen() is True
    assert changes == [{"level_adapt_done": False}]
    assert g.cfg["level_adapt_done"] is False
    assert g._level_seen == "conservative"
    assert any("重新武装" in m for m in logs)

    assert g._sync_level_seen() is False, "已回到保守：不再写盘"
    assert len(changes) == 1


def test_sync_level_seen_ignores_other_transitions(monkeypatch):
    """只有 激进→保守 才重武装：自动切换的过程、闩未落、开关关闭都不动配置。"""
    changes = []
    monkeypatch.setattr(tray, "update_config",
                        lambda ch: changes.append(dict(ch)) or dict(ch))
    monkeypatch.setattr(tray, "log", lambda m: None)
    monkeypatch.setattr(tray.os.path, "exists", lambda p: False)

    g = _guard(_state())

    g.cfg = _adapt_cfg(clean_level="conservative", level_adapt_done=True)
    g._level_seen = "conservative"
    assert g._sync_level_seen() is False

    g.cfg = _adapt_cfg(clean_level="aggressive", level_adapt_done=True)
    g._level_seen = "aggressive"
    assert g._sync_level_seen() is False, "自动切激进本身不该误判成用户手动切回"

    g.cfg = _adapt_cfg(clean_level="conservative", level_adapt_done=False)
    g._level_seen = "aggressive"
    assert g._sync_level_seen() is False, "没自动切过：没有闩可清"

    g.cfg = _adapt_cfg(clean_level="conservative", level_adapt_done=True,
                       auto_level_adapt=False)
    g._level_seen = "aggressive"
    assert g._sync_level_seen() is False, "自调优已关：不重新武装"

    assert changes == []


def test_hot_reload_rearms_level_adapt_and_refreshes_mtime(monkeypatch, tmp_path):
    """热重载链路：外部把档位改回保守即清闩写盘，_cfg_mtime 取写盘后的新值。"""
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text('{"clean_level": "conservative", "level_adapt_done": true}',
                        encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(tray, "CONFIG_PATH", str(cfg_file))
    written = {}

    def _fake_update_config(changes):
        written.update(changes)
        # 模拟 update_config 的读-改-写持锁：落盘并把 mtime 推后
        touched = time.time() + 10
        os.utime(cfg_file, (touched, touched))
        return {"clean_level": "conservative", "level_adapt_done": False}

    monkeypatch.setattr(tray, "update_config", _fake_update_config)
    logs = []
    monkeypatch.setattr(tray, "log", logs.append)

    guard = Guard()
    guard.cfg = _adapt_cfg(clean_level="aggressive", level_adapt_done=True)
    guard._level_seen = "aggressive"
    t0 = time.time()
    os.utime(cfg_file, (t0, t0))
    guard._cfg_mtime = t0 - 5        # 上次看到的 mtime 更早：跑一次热重载

    guard.maybe_reload_config()
    # 外部把档位改回保守：清闩写盘
    assert any("重新武装" in m for m in logs)
    assert written == {"level_adapt_done": False}
    assert guard._cfg_mtime == os.path.getmtime(str(cfg_file))
    guard.maybe_reload_config()
    assert logs.count("配置已热重载（来自 mem_guard.json）") == 1, \
        "_cfg_mtime 已跟上写盘后的 mtime：不该再白重载一次"


def test_advice_refresh_identifies_leak_processes(monkeypatch):
    """v1.11.0：同一份 top15 采样顺手拟合斜率，连涨的进程进 leaks 并喂给 analyze。"""
    t0 = 1000.0
    step = 100.0
    calls = {"n": 0}

    def _top(n=15):
        k = calls["n"]
        return [("leaky.exe", 256 * 1024 ** 2 + k * 64 * 1024 ** 2, 42),
                ("steady.exe", 512 * 1024 ** 2, 7)]

    monkeypatch.setattr(tray, "top_processes_list", _top)
    got = {}

    def _analyze(cfg, mem=None, top=None, leaks=None):
        got["leaks"] = leaks
        return []

    monkeypatch.setattr(tray, "analyze", _analyze)

    g = Guard()
    g.cfg["advice_refresh_sec"] = 0
    g._advice_at = 0.0
    for i in range(6):
        calls["n"] = i
        g._refresh_advice(t0 + i * step)

    assert [n for n, _, _ in g.leaks] == ["leaky.exe"], "只该报连涨的那个"
    assert got["leaks"] == g.leaks, "leaks 要作为快照传给 analyze"


def test_auto_clean_passes_leak_rows_to_do_clean(monkeypatch):
    """识别出的泄漏进程随自动清理带下去：growth_rows 透传给 do_clean。"""
    seen = {}

    def _fake_do_clean(reason="自动", growth_rows=None, **kw):
        seen["rows"] = growth_rows
        return {"ok": False, "freed": 0, "level": "conservative",
                "escalated": False, "detail": "-", "stats": {},
                "before": {}, "after": {}}

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    g = _guard(_state(phys_pct=86.0))
    g.leaks = [("leaky.exe", 2 * GB, 42)]
    g._auto_clean(1000.0, "自动", g.state)

    assert seen["rows"] == [("leaky.exe", 2 * GB, 42)]

# ---------------------------------------- 持续压力粘滞激进 + 阶段自学习（v1.12.0）

def _sticky_r(**over):
    """假 do_clean 的结果：v1.12.0 新增的键一次给全，别让缺项误导判定。"""
    base = {"ok": True, "freed": 1024, "level": "conservative", "escalated": False,
            "targeted": [], "bg_trim": 0, "detail": "-", "stats": {"count": 9},
            "before": {}, "after": {}, "still": False, "sticky": False,
            "targeted_freed": 0, "bg_freed": 0, "escalated_freed": 0}
    base.update(over)
    return base

def _sticky_fake(got, results):
    """记录每次收到的 sticky 形参，并按给定序列回吐结果。"""
    def _fake_do_clean(reason="自动", low_relief=False, growth_rows=None,
                       sticky=False, skip=(), **kw):
        got.append(sticky)
        res = dict(results[len(got) - 1])
        res.setdefault("sticky", sticky)
        return _sticky_r(**res)
    return _fake_do_clean

def test_auto_clean_enters_sticky_after_pressure_persists(monkeypatch):
    """完整阶梯后压力仍在：下一轮直接按激进档，进入说明只弹一次。"""
    got = []
    notes = []
    monkeypatch.setattr(tray, "do_clean",
                         _sticky_fake(got, [{"still": True},
                                            {"still": True,
                                             "level": "aggressive"}]))
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=86.0))
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._auto_clean(1000.0, "自动", g.state)
    g._auto_clean(1001.0, "自动", g.state)

    assert got == [False, True], "第二轮才该以 sticky=True 调 do_clean"
    assert g._sticky_aggr is True
    assert any("持续高压：粘滞激进" in t for _, t in notes)
    assert sum("直接按激进档" in t for _, t in notes) == 1, "进入说明只弹一次"


def test_auto_clean_leaves_sticky_once_relieved_and_rearms(monkeypatch):
    """压力一解除就退出粘滞、下次又从保守档起重（重武装），不赖在激进档。"""
    got = []
    logs = []
    monkeypatch.setattr(tray, "do_clean",
                         _sticky_fake(got, [{"still": True}, {"still": True},
                                            {"still": False}, {"still": True}]))
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", logs.append)

    g = _guard(_state(phys_pct=86.0))
    g.icon.notify = lambda text, title=None: None

    for i in range(3):
        g._auto_clean(1000.0 + i, "自动", g.state)
    assert got == [False, True, True]
    assert g._sticky_aggr is False, "第三轮压力已解除：应退出粘滞"
    assert any("粘滞激进退出" in m for m in logs)

    g._auto_clean(1003.0, "自动", g.state)
    assert got == [False, True, True, False], "退出后下一轮回到保守档起重"
    assert g._sticky_aggr is True, "压力仍在则重新进入粘滞"


def test_auto_clean_skips_sticky_when_switch_off(monkeypatch):
    """sticky_aggressive 关掉：压力再持续也不强制激进（用户不想让它自己动档）。"""
    got = []
    monkeypatch.setattr(tray, "do_clean",
                         _sticky_fake(got, [{"still": True}, {"still": True}]))
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=86.0))
    g.cfg["sticky_aggressive"] = False
    g.icon.notify = lambda text, title=None: None

    g._auto_clean(1000.0, "自动", g.state)
    g._auto_clean(1001.0, "自动", g.state)

    assert got == [False, False]
    assert g._sticky_aggr is False


def test_auto_clean_skips_stage_after_zero_release_streak(monkeypatch):
    """某阶段连续多次没释放出东西就跳过它：到 strikes 才生效，之前照跑。"""
    got, skips = [], []
    rows = [("big.exe", 2 * GB, 4242)]

    def _fake_do_clean(reason="自动", low_relief=False, growth_rows=None,
                       sticky=False, skip=(), **kw):
        got.append(sticky)
        skips.append(set(skip))
        # skip 命中了这一级就真的没跑：回吐空 targeted，学习计数不该继续累加
        hit = [] if "targeted" in skip else rows
        return _sticky_r(still=True, sticky=sticky, targeted=hit,
                         targeted_freed=0)

    monkeypatch.setattr(tray, "do_clean", _fake_do_clean)
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=86.0))
    g.cfg["stage_learn_strikes"] = 2
    g.cfg["sticky_aggressive"] = False      # 隔离变量：只验阶段跳过
    g.icon.notify = lambda text, title=None: None

    for i in range(3):
        g._auto_clean(1000.0 + i, "自动", g.state)

    assert got == [False, False, False], "隔离了粘滞，不关阶段学习的事"
    assert skips == [set(), set(), {"targeted"}]
    assert g._stage_zero["targeted"] == 2


def test_auto_clean_notify_marks_sticky_and_stage_release(monkeypatch):
    """粘滞轮通知要点明「持续高压」，并按阶段只列真的释放出来的量。"""
    monkeypatch.setattr(tray, "do_clean",
                         _sticky_fake([], [{"sticky": True,
                                            "level": "aggressive",
                                            "escalated": True,
                                            "escalated_freed": int(2.5 * GB)}]))
    monkeypatch.setattr(tray, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(tray, "log", lambda m: None)

    g = _guard(_state(phys_pct=86.0))
    notes = []
    g.icon.notify = lambda text, title=None: notes.append((title, text))

    g._auto_clean(1000.0, "自动", g.state)

    body = notes[0][1]
    assert "持续高压：粘滞激进" in body
    assert "分阶段释放: 升档 2.5GB" in body
    assert "定向" not in body, "没清到东西的阶段不必列出来"
