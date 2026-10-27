# -*- coding: utf-8 -*-
"""
全局热键：键位解析、监听线程派发，以及它与 Guard 的接线。

真实注册会把 Ctrl+Alt+M 等键抢到桌面上，所以派发一律用注入的
register / unregister / poll 覆盖；conftest 里的护栏还会把 tray.HotkeyListener
换成记录器——测试跑得再勤也不该动用户桌面上的热键。
"""
import json
import threading
import time
from types import SimpleNamespace

from memguard import clean, config, foreground, hotkeys, menu, winapi
from memguard.config import normalize_config
from memguard.menu import build_menu
from memguard.tray import Guard


def _by_label(tree):
    """摊平整棵树按文案建索引。

    v1.27.0 起「快捷键设置」收进了父项「热键与前台」，只查一级会 KeyError。
    """
    found = {}

    def walk(items):
        for it in items:
            found.setdefault(str(it.text), it)
            sub = getattr(it, "submenu", None)
            if sub:
                walk(sub.items)

    walk(tree.items)
    return found


def menu_guard():
    """菜单测试用的桩 Guard：只提供 build_menu 需要的属性，不写真配置。"""

    class _G:
        def __init__(self):
            self.cfg = normalize_config({})
            self.state = {"phys_pct": 50.0, "commit_pct": 50.0,
                          "used_phys": 8 * 1024 ** 3,
                          "total_phys": 16 * 1024 ** 3,
                          "avail_commit": 16 * 1024 ** 3}
            self.advice_count = 0
            self.advice_apply_count = 0
            self.history = []
            self.leaks = []
            self.icon = None

        def refresh(self):
            pass

        def restart_hotkeys(self):
            return None

        def reset_fullscreen_state(self):
            return None

    return _G()


def test_parses_valid_combos():
    mods = (winapi.MOD_CONTROL | winapi.MOD_ALT | winapi.MOD_NOREPEAT)
    assert hotkeys.parse_hotkey("ctrl+alt+m") == (mods, ord("M"))
    assert hotkeys.parse_hotkey("Alt+F5")[1] == 0x74      # VK_F5
    assert hotkeys.parse_hotkey("shift+ctrl+1")[1] == ord("1")
    assert hotkeys.parse_hotkey("win+d")[0] & winapi.MOD_WIN


def test_rejects_specs_that_would_steal_normal_keys():
    for spec in ("", "m", "ctrl+", "ctrl+alt+m+k", "ctrl+meta+m", None, 123, "   "):
        assert hotkeys.parse_hotkey(spec) is None, spec


def test_rejects_function_keys_out_of_range():
    assert hotkeys.parse_hotkey("ctrl+alt+f0") is None
    assert hotkeys.parse_hotkey("ctrl+alt+f25") is None
    assert hotkeys.parse_hotkey("ctrl+alt+f24") is not None


def test_format_hotkey_renders_human_form():
    assert hotkeys.format_hotkey("ctrl+alt+m") == "Ctrl+Alt+M"
    assert hotkeys.format_hotkey("ALT+f5") == "Alt+F5"
    assert hotkeys.format_hotkey("  win + shift + f12 ") == "Win+Shift+F12"
    assert hotkeys.format_hotkey("") == ""


def test_action_specs_reads_config_with_defaults():
    assert hotkeys.action_specs(normalize_config({})) == {
        "clean": "ctrl+alt+m", "overview": "ctrl+alt+o", "auto": "ctrl+alt+p"}


def test_action_specs_blank_string_disables_one_key():
    specs = hotkeys.action_specs(normalize_config({"hotkey_overview": ""}))
    assert "overview" not in specs
    assert set(specs) == {"clean", "auto"}


def test_action_specs_master_switch_wins():
    assert hotkeys.action_specs(normalize_config({"hotkey_enabled": False})) == {}


def test_action_specs_skips_unparseable_key():
    # 「ctrl+alt」没有主键：宁可不注册，也不静默注册个空壳
    specs = hotkeys.action_specs(normalize_config({"hotkey_auto": "ctrl+alt"}))
    assert set(specs) == {"clean", "overview"}


def test_action_specs_drops_duplicate_key_combos():
    """两个动作用了同一组合：只保留前者——后一条必然注册失败，还会进重试刷屏。"""
    specs = hotkeys.action_specs(normalize_config(
        {"hotkey_overview": "ctrl+alt+m"}))     # 与 clean 的默认键位撞车
    assert specs == {"clean": "ctrl+alt+m", "auto": "ctrl+alt+p"}
    assert "overview" not in specs


def test_summary_points_out_the_dropped_duplicate():
    cfg = normalize_config({"hotkey_overview": "ctrl+alt+m"})
    line = hotkeys.summary(cfg)
    assert "1 条键位与别的动作重复" in line
    assert "全局热键：Ctrl+Alt+M→立即清理" in line


def test_listener_never_retries_a_dropped_duplicate():
    """重复的那条在注册前就被丢掉：不会留下每 5 分钟重试一次的幽灵。"""
    calls = []
    specs = hotkeys.action_specs(normalize_config(
        {"hotkey_overview": "ctrl+alt+m"}))
    listener = hotkeys.HotkeyListener(
        specs, {}, poll_sec=0.01, retry_sec=60,
        register=lambda hid, *a: calls.append(hid) or True,
        unregister=lambda *a: None, poll=lambda: None)
    listener.start()
    assert _wait_for(lambda: len(calls) >= 2)
    time.sleep(0.05)
    listener.stop()
    assert calls == [1, 2], "只注册 clean 与 auto 两条"
    assert listener.failures == []


def test_summary_tells_the_user_why_nothing_is_available():
    assert hotkeys.summary(normalize_config({"hotkey_enabled": False})) == "全局热键：已关闭"
    assert "Ctrl+Alt+M" in hotkeys.summary(normalize_config({}))


class _Script:
    """按脚本逐个吐出热键 id，吐完返回 None（没有消息）。"""

    def __init__(self, *ids):
        self._ids = list(ids)

    def __call__(self):
        return self._ids.pop(0) if self._ids else None


def _wait_for(pred, timeout=2.0):
    """等异步回调跑齐：派发在另一个线程里，先 start 再 stop 有竞态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _listener(specs, callbacks=None, *, register, unregister, poll):
    return hotkeys.HotkeyListener(specs, callbacks, poll_sec=0.01,
                                   register=register, unregister=unregister,
                                   poll=poll)


def test_listener_dispatches_by_id_in_registration_order():
    seen = []
    listener = _listener(
        {"clean": "ctrl+alt+m", "overview": "ctrl+alt+o"},
        {"clean": lambda: seen.append("clean"),
         "overview": lambda: seen.append("overview")},
        register=lambda *a: True, unregister=lambda *a: True,
        poll=_Script(1, 2, 1))
    listener.start()
    assert _wait_for(lambda: len(seen) == 3)
    listener.stop()
    assert seen == ["clean", "overview", "clean"]


def test_listener_skips_unsuccessful_registration_and_only_frees_its_own():
    unregistered, calls = [], []
    listener = _listener(
        {"clean": "ctrl+alt+m", "overview": "ctrl+alt+o"},
        {"clean": lambda: calls.append("c"),
         "overview": lambda: calls.append("o")},
        register=lambda hid, *a: hid == 2,   # 第一条没注册上
        unregister=unregistered.append, poll=_Script(1, 2))
    listener.start()
    assert _wait_for(lambda: len(calls) == 1)
    listener.stop()
    assert calls == ["o"], "没注册成功的键位绝不能再被派发"
    assert unregistered == [2]


def test_listener_gives_up_when_nothing_registers():
    unregistered = []
    listener = _listener({"clean": "ctrl+alt+m"}, {},
                        register=lambda *a: False,
                        unregister=unregistered.append, poll=_Script(1))
    listener.start()
    listener.stop()
    assert unregistered == [], "一条都没注册上，没什么可解的"


def test_dispatch_failure_does_not_kill_the_loop():
    hits = []
    done = threading.Event()

    def flaky():
        hits.append(1)
        if len(hits) >= 3:
            done.set()
        else:
            raise RuntimeError("boom")

    listener = _listener({"clean": "ctrl+alt+m"}, {"clean": flaky},
                        register=lambda *a: True, unregister=lambda *a: None,
                        poll=_Script(1, 1, 1, None))
    listener.start()
    assert done.wait(2.0), "回调抛异常只该拖累那一次派发"
    listener.stop()
    assert len(hits) == 3


def test_no_specs_means_no_thread_and_start_is_idempotent():
    empty = hotkeys.HotkeyListener({}, {})
    empty.start()
    assert empty._thread is None

    started = _listener({"clean": "ctrl+alt+m"}, {},
                       register=lambda *a: True, unregister=lambda *a: None,
                       poll=_Script(None))
    started.start()
    started.start()
    assert started._thread is not None
    started.stop()


def test_hotkey_choices_are_all_usable_and_never_clash():
    """菜单里出现的每个组合都必须能注册，且同一组合只出现在一个动作下。"""
    assert hotkeys.choices_are_usable() == []
    assert hotkeys.choice_clashes() == []
    for action, specs in hotkeys.choice_specs().items():
        assert specs, "空表会让菜单只剩一个「关闭」"
        assert len(specs) == len(set(specs)), f"{action} 里有重复项"
        assert specs[0] == hotkeys.HOTKEY_DEFAULTS[action], "默认键位要排在第一位"


def test_menu_hotkey_picker_writes_only_that_key(monkeypatch):
    merged, restarts = [], []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))
    guard = menu_guard()
    monkeypatch.setattr(guard, "restart_hotkeys", lambda: restarts.append(1))
    groups = _by_label(build_menu(guard))
    sub = groups["快捷键设置"]
    by_label = {c.text: c for c in sub.submenu.items}
    pick = {c.text: c for c in by_label["立即清理"].submenu.items}
    pick["Ctrl+Alt+C"](guard.icon)
    assert merged[-1] == {"hotkey_clean": "ctrl+alt+c"}
    assert guard.cfg["hotkey_clean"] == "ctrl+alt+c", "合并结果要立刻回填"
    assert restarts, "改完键位必须重启监听，否则旧注册还占着键"


def test_menu_hotkey_picker_clears_the_clashing_key(monkeypatch):
    """占了别的动作的键就把那条清掉：不留「设了却不生效」的影子键位。"""
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))
    guard = menu_guard()
    monkeypatch.setattr(guard, "restart_hotkeys", lambda: None)
    sub = _by_label(build_menu(guard))["快捷键设置"]
    pick = {c.text: c for c in
            {c.text: c for c in sub.submenu.items}["立即清理"].submenu.items}
    # 先把「内存概览」误设成 Ctrl+Alt+C（本属「立即清理」组），再从本组选它
    guard.cfg["hotkey_overview"] = "ctrl+alt+c"
    pick["Ctrl+Alt+C"](guard.icon)
    assert merged[-1]["hotkey_clean"] == "ctrl+alt+c"
    assert merged[-1]["hotkey_overview"] == "", "占了别的动作的键就要把那条清掉"
    assert guard.cfg["hotkey_overview"] == ""


def test_menu_hotkey_picker_can_disable_one_action(monkeypatch):
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))
    guard = menu_guard()
    monkeypatch.setattr(guard, "restart_hotkeys", lambda: None)
    sub = _by_label(build_menu(guard))["快捷键设置"]
    pick = {c.text: c for c in
            {c.text: c for c in sub.submenu.items}["自动清理"].submenu.items}
    pick["关闭"](guard.icon)
    assert merged[-1] == {"hotkey_auto": ""}
    assert "auto" not in hotkeys.action_specs(guard.cfg)


# -- 与 Guard 的接线（真实注册被 conftest 的护栏换成了记录器）----

def test_guard_starts_listener_with_three_callbacks(no_real_hotkeys):
    guard = Guard()
    guard._start_hotkeys()
    assert len(no_real_hotkeys) == 1
    assert sorted(no_real_hotkeys[0].specs) == ["auto", "clean", "overview"]
    assert set(no_real_hotkeys[0].callbacks) == {"clean", "overview", "auto"}
    guard._stop_hotkeys()
    assert no_real_hotkeys[0].stopped is True


def test_listener_records_failed_registration_for_the_menu():
    """注册失败要能被看见：菜单摘要直接点出哪条被占用，不用翻日志。"""
    listener = _listener(
        {"clean": "ctrl+alt+m", "overview": "ctrl+alt+o"}, {},
        register=lambda hid, *a: hid == 1,   # 第二条被别的程序占用
        unregister=lambda *a: None, poll=_Script(None))
    listener.start()
    assert _wait_for(lambda: listener.failures)
    listener.stop()
    assert listener.failures == ["Ctrl+Alt+O"]
    assert "被占用" in hotkeys.summary(normalize_config({}), listener.failures)
    # 没有失败时不加尾巴，摘要保持原样
    assert "被占用" not in hotkeys.summary(normalize_config({}), [])


def test_status_lines_describe_the_current_guards(no_real_hotkeys):
    """概览窗 / 回退文案的「守护状态」三行：热键、全屏让路、前台保护。"""
    guard = Guard()
    lines = guard._status_lines()
    assert len(lines) == 3
    assert "Ctrl+Alt+M" in lines[0]
    assert lines[1].startswith("全屏让路：开") and "未暂停" in lines[1]
    assert lines[2] == "前台进程保护：开"

    guard.cfg = normalize_config({"fs_pause": False, "hotkey_enabled": False})
    lines = guard._status_lines()
    assert "已关闭" in lines[0]
    assert "全屏让路：关" in lines[1]
    guard._fs_paused = True
    assert "已暂停" in guard._status_lines()[1]


def test_guard_hotkey_failures_defaults_to_empty():
    guard = Guard()
    assert guard.hotkey_failures() == []
    guard._hotkeys = SimpleNamespace(failures=["Ctrl+Alt+O"])
    assert guard.hotkey_failures() == ["Ctrl+Alt+O"]
    guard._hotkeys = None
    assert guard.hotkey_failures() == []


def test_listener_retries_until_the_key_is_free():
    """占键的程序退出后自动恢复：不必重启 MemGuard 才能用上热键。"""
    calls = {"n": 0}
    clock = {"t": 0.0}

    def register(hotkey_id, *args):
        calls["n"] += 1
        return calls["n"] >= 3          # 前两次被占用，第三次起成功

    def poll():
        clock["t"] += 60.0              # 每次轮询当作过了一分钟
        return None

    listener = hotkeys.HotkeyListener(
        {"clean": "ctrl+alt+m", "overview": "ctrl+alt+o"}, {},
        poll_sec=0.01, retry_sec=60, clock=lambda: clock["t"],
        register=register, unregister=lambda *a: None, poll=poll)
    listener.start()
    assert _wait_for(lambda: calls["n"] >= 4), "两条失败的热键都要重试到成功"
    listener.stop()
    assert listener.failures == []


def test_listener_keeps_reporting_when_the_key_stays_taken():
    """一直占着就继续留在 failures 里：菜单摘要继续点出来，不装成好的。"""
    listener = hotkeys.HotkeyListener(
        {"clean": "ctrl+alt+m"}, {}, poll_sec=0.01, retry_sec=60,
        clock=lambda: 0.0, register=lambda *a: False,
        unregister=lambda *a: None, poll=lambda: None)
    listener.start()
    assert _wait_for(lambda: listener.failures == ["Ctrl+Alt+M"])
    listener.stop()
    assert listener.failures == ["Ctrl+Alt+M"]


def test_guard_hotkeys_off_starts_nothing(no_real_hotkeys):
    guard = Guard()
    guard.cfg = normalize_config({"hotkey_enabled": False})
    guard._start_hotkeys()
    assert no_real_hotkeys == []


def test_restart_replaces_the_listener(no_real_hotkeys):
    guard = Guard()
    guard._start_hotkeys()
    guard.cfg = normalize_config({"hotkey_clean": "ctrl+alt+n"})
    guard.restart_hotkeys()
    assert len(no_real_hotkeys) == 2
    assert no_real_hotkeys[0].stopped is True and no_real_hotkeys[1].started is True
    assert no_real_hotkeys[1].specs["clean"] == "ctrl+alt+n"
    guard._stop_hotkeys()


def test_hotkey_toggles_auto_clean_in_config(monkeypatch, tmp_path):
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text(json.dumps({"auto_clean": True}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))
    guard = Guard()
    guard.cfg = config.load_config()
    guard._toggle_auto_from_hotkey()
    assert guard.cfg["auto_clean"] is False
    guard._toggle_auto_from_hotkey()
    assert guard.cfg["auto_clean"] is True
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["auto_clean"] is True


def test_fullscreen_gate_notifies_once_per_transition(monkeypatch):
    notified = []
    guard = Guard()
    monkeypatch.setattr(guard, "_notify_balloon",
                        lambda m, t: notified.append((m, t)))
    reasons = ["全屏应用运行中"] * 3 + [None]
    monkeypatch.setattr(foreground, "pause_reason", lambda cfg: reasons.pop(0))
    for _ in range(3):
        assert guard._fullscreen_gate() == "全屏应用运行中"
    assert guard._fullscreen_gate() is None
    assert len(notified) == 1, "同一段全屏里只通知一次"
    assert "自动清理已暂停" in notified[0][0]
    assert guard._fs_paused is False
    assert guard.reset_fullscreen_state() is None
    assert guard._fs_paused is False


def test_monitor_skips_every_trigger_while_fullscreen(monkeypatch):
    """全屏期间整轮跳过：连内存采样都不做，更别说四类自动清理触发源。"""
    guard = Guard()
    guard.cfg = normalize_config({"interval": 60})
    refreshed, cleaned = [], []
    monkeypatch.setattr(guard, "refresh", lambda: refreshed.append(1))
    monkeypatch.setattr(clean, "do_clean",
                        lambda *a, **k: cleaned.append(1) or {"ok": True})
    monkeypatch.setattr(foreground, "pause_reason",
                        lambda cfg: "全屏应用运行中")
    monkeypatch.setattr(guard.stop, "wait", lambda timeout: guard.stop.set())
    guard.monitor()
    assert refreshed == [] and cleaned == []
    assert guard._fs_paused is True


def test_monitor_still_runs_when_not_fullscreen(monkeypatch):
    guard = Guard()
    guard.cfg = normalize_config({"interval": 60, "auto_clean": False})
    refreshed = []
    monkeypatch.setattr(guard, "refresh", lambda: refreshed.append(1))
    monkeypatch.setattr(foreground, "pause_reason", lambda cfg: None)
    monkeypatch.setattr(guard.stop, "wait", lambda timeout: guard.stop.set())
    guard.monitor()
    assert refreshed, "不在全屏时监控照常采样"

