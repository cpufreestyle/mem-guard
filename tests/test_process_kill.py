# -*- coding: utf-8 -*-
"""结束进程（v1.25.0）：保护判定的顺序、成功路径、terminate_process 分支、配置归一。

这条链路直接调 TerminateProcess，测错了就是用户机器上少一个进程——所以这里只验证
「该拒绝的都被拒绝、该放行的才放行」，真实句柄操作全部由注入的假 kernel32 顶掉，
任何用例都不许真的结束进程。"""
import os
import types

import psutil
import pytest

from memguard import actions, clean, config, foreground, menu, ui


# ---------------------------------------------------------------- 测试替身


class _FakeKernel:
    """顶掉 winapi.kernel32：OpenProcess 给假句柄，TerminateProcess 只记账。"""

    def __init__(self, handle=77, open_ok=True, term_ok=True, last_error=5):
        self.handle = handle
        self.open_ok = open_ok
        self.term_ok = term_ok
        self.last_error = last_error
        self.opened = []
        self.terminated = []
        self.closed = []

    def OpenProcess(self, rights, inherit, pid):
        self.opened.append(pid)
        return self.handle if self.open_ok else None

    def TerminateProcess(self, handle, code):
        self.terminated.append(handle)
        return self.term_ok

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True

    def GetLastError(self):
        return self.last_error


class _FakeProc:
    """顶掉 psutil.Process：名字与 RSS 都由用例给定。"""

    def __init__(self, pid, name="victim.exe", rss=256 * 1024 * 1024):
        self.pid = pid
        self._name = name
        self._rss = rss

    def name(self):
        return self._name

    def memory_info(self):
        return types.SimpleNamespace(rss=self._rss)


def _patch_kernel(monkeypatch, **kw):
    fake = _FakeKernel(**kw)
    monkeypatch.setattr(actions, "kernel32", fake)
    return fake


def _patch_proc(monkeypatch, proc):
    monkeypatch.setattr(clean.psutil, "Process", lambda pid: proc)
    monkeypatch.setattr(actions.psutil, "Process", lambda pid: proc)


def _bump(monkeypatch, seen):
    def _fake(freed, **kw):
        seen.append(kw)
        return {"count": 1, "freed": freed}

    monkeypatch.setattr(clean, "_bump_stats", _fake)


# ---------------------------------------------------------------- 保护判定顺序


def test_kill_process_rejects_system_pids(monkeypatch):
    """PID 0 / 4 是系统保留，连进程信息都不查就直接拒绝。"""
    fake = _patch_kernel(monkeypatch)
    for pid in (0, 4):
        r = clean.kill_process(pid, "anything.exe", {})
        assert r["ok"] is False
        assert "系统保留" in r["msg"]
    assert fake.opened == []


def test_kill_process_rejects_self(monkeypatch):
    """本进程 PID 触发自杀保护——杀自己等于让托盘瞬间消失。"""
    fake = _patch_kernel(monkeypatch)
    r = clean.kill_process(os.getpid(), "python.exe", {})
    assert r["ok"] is False
    assert "自杀保护" in r["msg"]
    assert fake.opened == []


def test_kill_process_rejects_foreground_pid(monkeypatch):
    """fs_protect 下用户正在用的前台进程，排在黑名单之前先拒。"""
    fake = _patch_kernel(monkeypatch)
    monkeypatch.setattr(foreground, "foreground_pid", lambda: 4321)
    r = clean.kill_process(4321, "editor.exe", {})
    assert r["ok"] is False
    assert "前台" in r["msg"]
    assert fake.opened == []


def test_kill_process_foreground_probe_failure_is_not_protection(monkeypatch):
    """前台探测失败不能变成「谁都被保护」：按无前台进程继续走后续判定。"""
    fake = _patch_kernel(monkeypatch)

    def _boom(cfg):
        raise OSError("no foreground window")

    monkeypatch.setattr(foreground, "protected_pid", _boom)
    _patch_proc(monkeypatch, _FakeProc(5000))
    r = clean.kill_process(5000, "victim.exe", {})
    assert r["ok"] is True
    assert fake.terminated == [77]


def test_kill_process_rejects_blacklisted_names(monkeypatch):
    """黑名单（含通配符）里的进程一律拒杀，RSS 再大也不下手。"""
    fake = _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(6000, name="csrss.exe"))
    r = clean.kill_process(6000, "csrss.exe", {})
    assert r["ok"] is False
    assert "黑名单" in r["msg"]
    assert fake.opened == []


def test_kill_process_rejects_below_min_rss(monkeypatch):
    """RSS 低于 process_kill_min_mb 拒杀轻量进程，门槛值由配置给。"""
    fake = _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(7000, rss=32 * 1024 * 1024))
    r = clean.kill_process(7000, "victim.exe", {"process_kill_min_mb": 64})
    assert r["ok"] is False
    assert "下限" in r["msg"]
    assert fake.opened == []


def test_kill_process_unknown_pid_fails_softly(monkeypatch):
    """进程已退出：取不到名字与 RSS，仍然软失败而不是抛异常。"""
    fake = _patch_kernel(monkeypatch)
    monkeypatch.setattr(clean.psutil, "Process",
                        lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)))
    r = clean.kill_process(2 ** 22, "ghost.exe", {})
    assert r["ok"] is False
    assert r["rss"] == 0
    assert fake.opened == []


def test_kill_process_rejects_non_integer_pid(monkeypatch):
    """脏 PID 直接拒绝，不进入任何保护判定。"""
    fake = _patch_kernel(monkeypatch)
    monkeypatch.setattr(clean, "load_config", lambda: {})
    r = clean.kill_process("abc", "victim.exe", {})
    assert r["ok"] is False
    assert "非法 PID" in r["msg"]
    assert fake.opened == []


# ---------------------------------------------------------------- 成功路径


def test_kill_process_success_terminates_and_counts(monkeypatch):
    """放行的进程被真结束，且只累计 process_killed，不写 count/freed 账本。"""
    fake = _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(5000))
    # 结束后要等目标真正退出；这里固定为“已退出”，不依赖真实系统 PID 状态
    monkeypatch.setattr(clean, "_wait_process_gone", lambda pid, timeout=None: True)
    seen = []
    _bump(monkeypatch, seen)
    r = clean.kill_process(5000, "victim.exe", {"process_kill_min_mb": 64})
    assert r["ok"] is True
    assert r["name"] == "victim.exe"
    assert r["rss"] == 256 * 1024 * 1024
    assert "已结束进程" in r["msg"]
    assert fake.opened == [5000]
    assert fake.terminated == [77]
    assert fake.closed == [77]
    assert seen == [{"process_killed": True}]


def test_kill_process_failure_does_not_count(monkeypatch):
    """TerminateProcess 返回 False：只报失败，不累计结束次数。"""
    fake = _patch_kernel(monkeypatch, term_ok=False)
    _patch_proc(monkeypatch, _FakeProc(5000))
    seen = []
    _bump(monkeypatch, seen)
    r = clean.kill_process(5000, "victim.exe", {})
    assert r["ok"] is False
    assert "失败" in r["msg"]
    assert fake.terminated == [77]
    assert fake.closed == [77]
    assert seen == []


def test_kill_process_loads_config_when_cfg_is_none(monkeypatch):
    """cfg=None 时自己读盘，托盘/概览窗都不必先构造配置。"""
    fake = _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(5000))
    monkeypatch.setattr(clean, "load_config",
                        lambda: {"process_kill_min_mb": 1})
    _bump(monkeypatch, [])
    monkeypatch.setattr(clean, "_wait_process_gone", lambda pid, timeout=None: True)
    assert clean.kill_process(5000, "victim.exe")["ok"] is True
    assert fake.terminated == [77]

# ---------------------------------------------------------------- terminate_process


def test_terminate_process_rejects_invalid_pid(monkeypatch):
    fake = _patch_kernel(monkeypatch)
    assert actions.terminate_process("abc") is False
    assert fake.opened == []


def test_terminate_process_rejects_system_and_self(monkeypatch):
    fake = _patch_kernel(monkeypatch)
    assert actions.terminate_process(0) is False
    assert actions.terminate_process(4) is False
    assert actions.terminate_process(os.getpid()) is False
    assert fake.opened == []


def test_terminate_process_rejects_name_mismatch(monkeypatch):
    """PID 已被复用时名对不上：宁可放过，也不能杀错进程。"""
    fake = _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(6000, name="other.exe"))
    assert actions.terminate_process(6000, "victim.exe") is False
    assert fake.opened == []


def test_terminate_process_rejects_when_name_unreadable(monkeypatch):
    """读不到进程名（可能已退出）时直接拒绝，不盲杀。"""
    fake = _patch_kernel(monkeypatch)
    monkeypatch.setattr(actions.psutil, "Process",
                        lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)))
    assert actions.terminate_process(2 ** 22, "ghost.exe") is False
    assert fake.opened == []


def test_terminate_process_skips_when_open_fails(monkeypatch):
    fake = _patch_kernel(monkeypatch, open_ok=False)
    _patch_proc(monkeypatch, _FakeProc(6000))
    assert actions.terminate_process(6000, "victim.exe") is False
    assert fake.opened == [6000]
    assert fake.terminated == []


def test_terminate_process_closes_handle_on_success(monkeypatch):
    fake = _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(6000))
    assert actions.terminate_process(6000, "victim.exe") is True
    assert fake.closed == [77]


def test_terminate_process_closes_handle_on_api_failure(monkeypatch):
    """TerminateProcess 失败也要 CloseHandle：假句柄漏一个就是泄漏一个内核对象。"""
    fake = _patch_kernel(monkeypatch, term_ok=False)
    _patch_proc(monkeypatch, _FakeProc(6000))
    assert actions.terminate_process(6000, "victim.exe") is False
    assert fake.closed == [77]


def test_terminate_process_swallows_api_exception(monkeypatch):
    """ctypes 抛异常只记日志返回 False，绝不影响调用方主流程。"""

    class _Boom:
        def OpenProcess(self, rights, inherit, pid):
            raise OSError("access denied")

    monkeypatch.setattr(actions, "kernel32", _Boom())
    assert actions.terminate_process(6000, "victim.exe") is False

# ---------------------------------------------------------------- 配置归一


def test_normalize_config_clamps_process_kill_min_mb():
    assert config.normalize_config({})["process_kill_min_mb"] == 64
    assert config.normalize_config({"process_kill_min_mb": 0})["process_kill_min_mb"] == 1
    assert config.normalize_config(
        {"process_kill_min_mb": 99999})["process_kill_min_mb"] == 32768
    assert config.normalize_config(
        {"process_kill_min_mb": "xx"})["process_kill_min_mb"] == 64


def test_normalize_config_coerces_process_kill_bools():
    cfg = config.normalize_config(
        {"process_kill_enabled": 0, "process_kill_confirm": 1})
    assert cfg["process_kill_enabled"] is False
    assert cfg["process_kill_confirm"] is True

# ---------------------------------------------------------------- 菜单与概览窗


class _FakeGuard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


def _build_menu(monkeypatch, cfg):
    guard = _FakeGuard(cfg)
    monkeypatch.setattr(menu, "update_config", lambda changes: changes)
    return guard, menu.build_menu(guard)


def _find_kill_item(items):
    found = []

    def walk(rows):
        for it in rows:
            sub = getattr(it, "submenu", None)
            if sub:
                walk(sub.items)
            elif "victim.exe" in str(getattr(it, "text", "")):
                found.append(it)

    walk(list(items))
    return found


def test_menu_kill_submenu_disabled_when_feature_off(monkeypatch):
    """总开关关闭后子菜单每一项都置灰，点不动。"""
    rows = [("victim.exe", 256 * 1024 * 1024, 5000)]
    monkeypatch.setattr(menu, "top_processes_list", lambda n: rows)
    guard, m = _build_menu(monkeypatch, {"process_kill_enabled": False})
    found = _find_kill_item(m.items)
    assert len(found) == 1
    assert found[0]._enabled(None) is False


def test_menu_kill_submenu_enabled_when_feature_on(monkeypatch):
    rows = [("victim.exe", 256 * 1024 * 1024, 5000)]
    monkeypatch.setattr(menu, "top_processes_list", lambda n: rows)
    guard, m = _build_menu(monkeypatch, {"process_kill_enabled": True})
    found = _find_kill_item(m.items)
    assert len(found) == 1
    assert found[0]._enabled(None) is True


def test_menu_kill_one_requires_confirmation(monkeypatch):
    """确认框被拒时一个进程都不碰。"""
    monkeypatch.setattr(menu, "confirm_box", lambda t, p: False)
    killed = []
    monkeypatch.setattr(menu, "kill_process",
                        lambda pid, name, cfg: killed.append(pid) or {"ok": False})
    monkeypatch.setattr(menu, "message_box", lambda t, p: None)
    guard = _FakeGuard({"process_kill_confirm": True})
    menu._kill_one(guard, 5000, "victim.exe")
    assert killed == []
    assert guard.refreshed == 0


def test_menu_kill_one_kills_and_reports(monkeypatch):
    """确认框通过：结束进程、弹结果框、刷新菜单。"""
    asked = []
    monkeypatch.setattr(menu, "confirm_box",
                        lambda t, p: asked.append(p) or True)
    killed = []
    monkeypatch.setattr(menu, "kill_process",
                        lambda pid, name, cfg: killed.append((pid, name))
                        or {"ok": True, "msg": "已结束"})
    shown = []
    monkeypatch.setattr(menu, "message_box", lambda t, p: shown.append(p))
    guard = _FakeGuard({"process_kill_confirm": True})
    menu._kill_one(guard, 5000, "victim.exe")
    assert asked == ["确定要结束进程 victim.exe (PID 5000) 吗？"]
    assert killed == [(5000, "victim.exe")]
    assert shown == ["已结束"]
    assert guard.refreshed == 1


def test_menu_kill_one_skips_confirmation_when_disabled(monkeypatch):
    """关掉确认框后直接执行——用户明确选择了少一次弹窗。"""
    monkeypatch.setattr(menu, "confirm_box",
                        lambda t, p: pytest.fail("不该弹确认框"))
    killed = []
    monkeypatch.setattr(menu, "kill_process",
                        lambda pid, name, cfg: killed.append(pid)
                        or {"ok": True, "msg": "已结束"})
    monkeypatch.setattr(menu, "message_box", lambda t, p: None)
    guard = _FakeGuard({"process_kill_confirm": False})
    menu._kill_one(guard, 5000, "victim.exe")
    assert killed == [5000]


def test_menu_toggles_flip_config(monkeypatch):
    """两个开关各自取反当前值并落盘。"""
    written = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: written.append(changes) or changes)
    guard = _FakeGuard({"process_kill_enabled": True,
                       "process_kill_confirm": True})
    m = menu.build_menu(guard)

    def find(items, label):
        for it in items:
            if getattr(it, "text", None) == label:
                return it
            sub = getattr(it, "submenu", None)
            if sub:
                got = find(sub.items, label)
                if got:
                    return got
        return None

    find(list(m.items), "关闭进程功能")(None)
    find(list(m.items), "结束进程前弹确认框")(None)
    assert written == [{"process_kill_enabled": False},
                       {"process_kill_confirm": False}]


def test_overview_kill_path_blocked_when_disabled(monkeypatch):
    """Top10 右键在总开关关闭时给出提示并返回，不调 kill_process。"""
    shown = []
    monkeypatch.setattr(ui, "load_config",
                        lambda: {"process_kill_enabled": False})
    monkeypatch.setattr(ui, "message_box", lambda t, p: shown.append((t, p)))
    monkeypatch.setattr(ui, "confirm_box",
                        lambda t, p: pytest.fail("不该弹确认框"))
    monkeypatch.setattr(ui, "kill_process",
                        lambda *a, **k: pytest.fail("不该结束进程"))
    refreshed = []
    ui.run_kill([("victim.exe", 5000)], lambda: refreshed.append(1))
    assert shown and "已关闭" in shown[0][1]
    assert refreshed == []


def test_overview_kill_path_reports_each_result(monkeypatch):
    """确认框通过后逐个结束，成功与失败分别汇总成一句人话。"""
    monkeypatch.setattr(ui, "load_config",
                        lambda: {"process_kill_enabled": True,
                        "process_kill_confirm": True})
    monkeypatch.setattr(ui, "confirm_box", lambda t, p: True)
    results = {
        5000: {"ok": True, "msg": "已结束"},
        5001: {"ok": False, "msg": "拒绝"},
    }
    monkeypatch.setattr(ui, "kill_process",
                        lambda pid, nm, cfg: results[pid])
    shown = []
    monkeypatch.setattr(ui, "message_box", lambda t, p: shown.append(p))
    refreshed = []
    ui.run_kill([("victim.exe", 5000), ("victim.exe", 5001)],
                lambda: refreshed.append(1))
    assert "已结束：victim.exe(PID 5000)" in shown[0]
    assert "失败：victim.exe(PID 5001): 拒绝" in shown[0]
    assert refreshed == [1]


def test_overview_kill_path_requires_confirmation(monkeypatch):
    """确认框被拒：连 kill_process 都不调。"""
    monkeypatch.setattr(ui, "load_config",
                        lambda: {"process_kill_enabled": True,
                        "process_kill_confirm": True})
    monkeypatch.setattr(ui, "confirm_box", lambda t, p: False)
    monkeypatch.setattr(ui, "kill_process",
                        lambda *a, **k: pytest.fail("不该结束进程"))
    monkeypatch.setattr(ui, "message_box",
                        lambda t, p: pytest.fail("不该弹结果框"))
    refreshed = []
    ui.run_kill([("victim.exe", 5000)], lambda: refreshed.append(1))
    assert refreshed == []

def test_kill_process_waits_for_real_exit(monkeypatch):
    """TerminateProcess 返回成功但进程还活着：不得上报成功。

    TerminateProcess 是异步的，返回 True 只代表终止请求被接受。若此时就报
    “已结束”，用户会看到进程还在，看起来就像关不掉。
    """
    fake = _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(5000))
    seen = []
    _bump(monkeypatch, seen)
    monkeypatch.setattr(clean, "_wait_process_gone", lambda pid, timeout=None: False)
    r = clean.kill_process(5000, "victim.exe", {})
    assert r["ok"] is False
    assert fake.terminated == [77]
    assert seen == []


def test_wait_process_gone_polls_until_gone(monkeypatch):
    """等待函数自己：进程在前几轮还活着，之后消失时返回 True。"""
    seq = iter([True, True, True, False])
    monkeypatch.setattr(clean.psutil, "pid_exists", lambda pid: next(seq))
    monkeypatch.setattr(clean.time, "sleep", lambda s: None)
    assert clean._wait_process_gone(5000) is True


def test_wait_process_gone_times_out(monkeypatch):
    """超过上限还活着时返回 False，不无限等下去。"""
    monkeypatch.setattr(clean.psutil, "pid_exists", lambda pid: True)
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(clean.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(clean.time, "sleep", lambda s: None)
    assert clean._wait_process_gone(5000, timeout=3.0) is False


# ------------------------------------------------ v1.28.0 失败必须带原因（用户报「无法删除」）


def test_open_denied_reason_reaches_the_user(monkeypatch):
    """OpenProcess 被拒（错误码 5）：对话框要说清是权限不足，不能只回一句「失败」。

    v1.27.0 真机反馈：点 vmmemWSL 只弹「结束进程 vmmemWSL (PID 29300) 失败」，
    用户据此反复点、并判定软件坏了。原因在系统那侧（Session 0 / SYSTEM 拥有）。
    """
    fake = _patch_kernel(monkeypatch, open_ok=False, last_error=5)
    _patch_proc(monkeypatch, _FakeProc(29300, name="someproc.exe"))
    monkeypatch.setattr(clean, "_wait_process_gone", lambda pid, timeout=None: True)
    r = clean.kill_process(29300, "someproc.exe", {})
    assert r["ok"] is False
    assert "权限不足" in r["msg"], r["msg"]
    assert "SYSTEM" in r["msg"]
    assert fake.opened == [29300]


def test_unknown_error_code_still_reports_the_code(monkeypatch):
    """没列进翻译表的错误码也要带出来：一句「失败」和一句「失败（错误码 1168）」差很多。"""
    _patch_kernel(monkeypatch, open_ok=False, last_error=1167)
    _patch_proc(monkeypatch, _FakeProc(6000))
    r = clean.kill_process(6000, "victim.exe", {})
    assert r["ok"] is False
    assert "1167" in r["msg"], r["msg"]


def test_terminate_rejected_reason_and_handle_still_closed(monkeypatch):
    """句柄开到了但 TerminateProcess 被拒：同样要说清原因，且句柄照样关掉。"""
    fake = _patch_kernel(monkeypatch, term_ok=False, last_error=5)
    _patch_proc(monkeypatch, _FakeProc(6000))
    r = clean.kill_process(6000, "victim.exe", {})
    assert r["ok"] is False
    assert "权限不足" in r["msg"]
    assert fake.closed == [77]


def test_wait_timeout_says_the_exit_is_still_pending(monkeypatch):
    """终止请求已接受却没按时退出：要说「还在退出」，不是笼统的失败。"""
    _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(5000))
    monkeypatch.setattr(clean, "_wait_process_gone", lambda pid, timeout=None: False)
    r = clean.kill_process(5000, "victim.exe", {})
    assert r["ok"] is False
    assert "终止请求" in r["msg"] and "没有退出" in r["msg"]


def test_terminate_process_bool_shell_unchanged(monkeypatch):
    """老外壳仍是纯布尔：别为了带原因把每个调用点都改成解包。"""
    fake = _patch_kernel(monkeypatch, open_ok=False)
    _patch_proc(monkeypatch, _FakeProc(6000))
    assert actions.terminate_process(6000, "victim.exe") is False
    ok, why = actions.terminate_process_with_reason(6000, "victim.exe")
    assert ok is False and "权限不足" in why
    assert fake.opened == [6000, 6000]


# ------------------------------------------------ v1.28.0 虚拟机宿主：挡结束，不挡清理


def test_vm_host_names_are_kill_protected():
    """vmmem / vmmemWSL / vmmemNetProxy / vmcompute / vmwp 全部拒杀，普通进程照旧放行。"""
    for nm in ("vmmem", "vmmemWSL", "vmmemNetProxy", "vmcompute.exe", "vmwp"):
        assert clean.can_kill(nm) is False, nm
        reason = clean.kill_protect_reason(nm)
        assert "虚拟机宿主" in reason and "wsl --shutdown" in reason, nm
    for nm in ("chrome.exe", "Qoder CN.exe", "comet.exe"):
        assert clean.can_kill(nm) is True, nm


def test_vm_host_is_not_clean_blacklisted():
    """只挡结束，绝不能挡清理：清空 vmmem 的工作集是这台机器上最划算的一刀。"""
    for nm in ("vmmem", "vmmemWSL"):
        assert config.name_in_blacklist(nm, config.CLEAN_BLACKLIST_STEMS) is False, nm
        assert config.name_in_blacklist(nm, config.PROCESS_KILL_VMHOST_STEMS) is True, nm


def test_kill_process_rejects_vm_host_before_touching_handles(monkeypatch):
    """命中虚拟机宿主保护：一个句柄都不开，话术给出「用立即清理 / wsl --shutdown」。"""
    fake = _patch_kernel(monkeypatch)
    _patch_proc(monkeypatch, _FakeProc(29300, name="vmmemWSL", rss=2 * 1024 ** 3))
    r = clean.kill_process(29300, "vmmemWSL", {})
    assert r["ok"] is False
    assert fake.opened == []
    assert "虚拟机宿主" in r["msg"] and "黑名单" not in r["msg"]


def test_menu_lists_vm_host_as_unprotected_free_but_unkillable(monkeypatch):
    """Top8 里 vmmem 仍然列出来（占榜首，删掉会让人以为没看见），但标「受保护」点不动。"""
    rows = [("vmmemWSL", 2 * 1024 ** 3, 29300),
            ("victim.exe", 256 * 1024 * 1024, 5000)]
    monkeypatch.setattr(menu, "top_processes_list", lambda n: rows)
    guard, m = _build_menu(monkeypatch, {"process_kill_enabled": True})

    def submenu_items(label):
        """深度找到父项并取其子菜单——按**求值后**的文案找。

        pystray 把字符串也存成 callable，所以要比 `.text`；而根菜单头几行是实时状态
        （要 guard.state），读它们的 `.text` 会抛 AttributeError，getattr 的默认值
        正好把它们跳过去——这个假 guard 只带 cfg，逐行求值整棵树会在无关处炸掉。
        """
        def find(node):
            for it in node.items:
                sub = getattr(it, "submenu", None)
                if str(getattr(it, "text", "")) == label and sub is not None:
                    return sub
                if sub is not None:
                    got = find(sub)
                    if got is not None:
                        return got
            return None

        got = find(m)
        assert got is not None, f"菜单里没有「{label}」"
        return list(got.items)

    items = submenu_items("结束进程（内存占用 Top8）")
    found = {str(it.text): it for it in items}
    vm = next(t for t in found if t.startswith("vmmemWSL"))
    vic = next(t for t in found if t.startswith("victim.exe"))
    assert vm.endswith("· 受保护"), vm
    assert found[vm]._enabled(None) is False
    assert found[vic]._enabled(None) is True
    # 只有能安全结束的行才点得动
    assert [t for t in found if found[t]._enabled(None)] == [vic]
