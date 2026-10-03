# -*- coding: utf-8 -*-
"""update：版本号解析、资产挑选、检查节流、通知契约（全部不发真实网络请求）。

后台静默检查的「不打扰」是硬契约：检查失败 / 已最新都不该有气泡，有新版每个
tag 只提醒一次，打开发布页只在用户显式点了「检查更新」时才发生。
"""
from memguard import update as update_mod
from memguard.update import (_parse_version, check_and_notify, due_for_check,
                             pick_asset, portable_boot_cmd, setup_boot_cmd)


def test_parse_version_basic():
    assert _parse_version("1.3.0") == (1, 3, 0)
    assert _parse_version("v1.3.0") == (1, 3, 0)
    assert _parse_version("V1.3.0") == (1, 3, 0)


def test_parse_version_ignores_non_numeric_suffix():
    assert _parse_version("v1.3.0-beta") == (1, 3, 0)
    assert _parse_version("1.3.0") == _parse_version("v1.3.0")


def test_parse_version_ordering():
    assert _parse_version("v1.10.2") > _parse_version("1.9.9")
    assert _parse_version("1.3.10") > _parse_version("1.3.9")
    assert not _parse_version("1.3.9") > _parse_version("1.3.10")


def test_parse_version_empty():
    assert _parse_version("") == (0,)
    assert _parse_version("v") == (0,)


# ---------------------------------------------------------------- 资产挑选

_ASSETS = [
    {"name": "README.md", "browser_download_url": "https://x/readme"},
    {"name": "mem_guard.exe", "browser_download_url": "https://x/portable"},
    {"name": "MemGuard-Setup-1.5.0.exe", "browser_download_url": "https://x/setup"},
]


def test_pick_asset_prefers_setup_over_portable():
    assert pick_asset(_ASSETS) == {"name": "MemGuard-Setup-1.5.0.exe",
                                   "url": "https://x/setup"}


def test_pick_asset_honors_portable_preference():
    assert pick_asset(_ASSETS, prefer="portable")["name"] == "mem_guard.exe"


def test_pick_asset_falls_back_and_handles_empty():
    assert pick_asset([_ASSETS[1]])["name"] == "mem_guard.exe"
    assert pick_asset([]) is None
    assert pick_asset(None) is None
    assert pick_asset([{"name": "notes.txt", "browser_download_url": "https://x"}]) is None
    assert pick_asset([{"name": "mem_guard.exe"}]) is None, "没有下载地址的资产不能用"


# ---------------------------------------------------------------- 检查节流

def test_due_for_check_gate():
    now = 1000000.0
    assert due_for_check({"auto_update": False}, now) is False
    # cfg 缺失按默认值处理：auto_update 默认开，所以是「该查」
    assert due_for_check(None, now) is True
    base = {"auto_update": True, "update_check_hours": 12}
    assert due_for_check(base, now) is True, "从没查过就该查"
    assert due_for_check({**base, "last_update_check": now - 11 * 3600}, now) is False
    assert due_for_check({**base, "last_update_check": now - 13 * 3600}, now) is True
    # 脏值不能让调用方抛异常：间隔非法按不查，时间戳脏按从没查过
    assert due_for_check({"auto_update": True, "update_check_hours": 0}, now) is False
    assert due_for_check({"auto_update": True, "update_check_hours": "x",
                          "last_update_check": "x"}, now) is True


# ---------------------------------------------------------------- 引导批处理

def test_portable_boot_cmd_carries_pid_paths_and_selfdelete():
    cmd = portable_boot_cmd(4321, "C:\\T\\new.exe", "C:\\App\\mem_guard.exe")
    assert "@echo off" in cmd
    assert "PID eq 4321" in cmd
    assert "set \"new=C:\\T\\new.exe\"" in cmd
    assert "set \"target=C:\\App\\mem_guard.exe\"" in cmd
    assert "move /y" in cmd
    assert "start \"\" \"%target%\"" in cmd
    assert "del \"%~f0\"" in cmd


def test_setup_boot_cmd_silent_install_and_restart():
    cmd = setup_boot_cmd(777, "C:\\T\\MemGuard-Setup-1.5.0.exe", "C:\\App")
    assert "@echo off" in cmd
    assert "PID eq 777" in cmd
    assert "\"C:\\T\\MemGuard-Setup-1.5.0.exe\" /VERYSILENT /SP- /NORESTART" in cmd
    assert "start \"\" \"C:\\App\\mem_guard.exe\"" in cmd
    assert "del \"%~f0\"" in cmd


# ---------------------------------------------------------------- 通知契约

def _cfg(**over):
    cfg = {"auto_update": True, "auto_install": False, "update_check_hours": 12,
           "last_update_check": 0, "update_notified_tag": ""}
    cfg.update(over)
    return cfg


def _fake_release(monkeypatch, tag="v9.9.9", ok=True, msg=""):
    if not ok:
        monkeypatch.setattr(update_mod, "fetch_latest_release",
                            lambda timeout=8: {"ok": False, "msg": msg or "boom"})
        return
    monkeypatch.setattr(update_mod, "fetch_latest_release", lambda timeout=8: {
        "ok": True, "tag": tag, "url": "https://example/release",
        "newer": _parse_version(tag) > _parse_version("1.5.0"), "assets": _ASSETS})


def test_check_and_notify_notifies_once_per_tag(monkeypatch):
    written, bubbles = [], []
    monkeypatch.setattr(update_mod, "update_config",
                        lambda changes: written.append(dict(changes)))
    _fake_release(monkeypatch, "v9.9.9")
    r = check_and_notify(_cfg(), notify=lambda m, t: bubbles.append(m))
    assert r["newer"] is True and r["action"] == "notify"
    assert len(bubbles) == 1 and "v9.9.9" in bubbles[0]
    assert "立即更新" in bubbles[0]
    assert any(c.get("last_update_check") for c in written)
    assert written[-1].get("update_notified_tag") == "v9.9.9", "提醒过的 tag 要记下去重"

    bubbles.clear()
    check_and_notify(_cfg(update_notified_tag="v9.9.9"),
                     notify=lambda m, t: bubbles.append(m))
    assert bubbles == [], "同一版本不该反复提醒"


def test_check_and_notify_stays_quiet_on_latest_and_error(monkeypatch):
    monkeypatch.setattr(update_mod, "update_config", lambda changes: None)
    bubbles = []
    _fake_release(monkeypatch, "v1.5.0")
    r = check_and_notify(_cfg(), notify=lambda m, t: bubbles.append(m))
    assert r["newer"] is False and bubbles == [], "已最新不该打扰"

    _fake_release(monkeypatch, ok=False, msg="网络不通")
    r2 = check_and_notify(_cfg(), notify=lambda m, t: bubbles.append(m))
    assert r2["ok"] is False and bubbles == [], "检查失败不该打扰"


def test_check_and_notify_auto_install_takes_over(monkeypatch):
    calls = []
    monkeypatch.setattr(update_mod, "update_config", lambda changes: None)
    monkeypatch.setattr(update_mod, "fetch_latest_release", lambda timeout=8: {
        "ok": True, "tag": "v9.9.9", "url": "https://example/release",
        "newer": True, "assets": _ASSETS})
    monkeypatch.setattr(update_mod, "install_latest",
                        lambda cfg, on_ready=None, release=None:
                        calls.append(release["tag"]) or {"ok": True})
    bubbles = []
    check_and_notify(_cfg(auto_install=True), notify=lambda m, t: bubbles.append(m),
                     on_ready=lambda: None)
    assert calls == ["v9.9.9"], "开了自动安装就不该只提醒"
    assert bubbles and "正在下载并安装" in bubbles[0]


def test_check_and_notify_open_page_only_when_asked(monkeypatch):
    opened = []
    monkeypatch.setattr(update_mod, "update_config", lambda changes: None)
    monkeypatch.setattr(update_mod, "fetch_latest_release", lambda timeout=8: {
        "ok": True, "tag": "v9.9.9", "url": "https://example/release",
        "newer": True, "assets": _ASSETS})
    monkeypatch.setattr(update_mod, "_open_url", lambda url: opened.append(url))

    check_and_notify(_cfg(), notify=lambda m, t: None)
    assert opened == [], "后台静默检查不该抢浏览器焦点"
    check_and_notify(_cfg(), notify=None, open_page=True)
    assert opened == ["https://example/release"]
