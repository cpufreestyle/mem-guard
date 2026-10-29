# -*- coding: utf-8 -*-
"""update：版本号解析、资产挑选、检查节流、通知契约（全部不发真实网络请求）。

后台静默检查的「不打扰」是硬契约：检查失败 / 已最新都不该有气泡，有新版每个
tag 只提醒一次，打开发布页只在用户显式点了「检查更新」时才发生。
"""
import hashlib
import json
import urllib.error

from memguard import update as update_mod
from memguard.update import (_parse_version, check_and_notify, due_for_check,
                             find_checksums_asset, parse_checksums, pick_asset,
                             portable_boot_cmd, setup_boot_cmd, sha256_of,
                             verify_asset)


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


def test_pick_asset_accepts_normalized_release_shape():
    """真实链路的资产是 fetch_latest_release 归一化过的（键名 url，不是
    browser_download_url）。只认后者时线上永远「没有可用安装包」——v1.29.4 修。
    """
    normalized = [{"name": a["name"], "url": a["browser_download_url"]} for a in _ASSETS]
    assert pick_asset(normalized)["name"] == "MemGuard-Setup-1.5.0.exe"
    assert pick_asset(normalized, prefer="portable")["name"] == "mem_guard.exe"
    assert find_checksums_asset(normalized + [{"name": "checksums.txt",
                                               "url": "https://x/ck"}])["url"] == "https://x/ck"


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


def test_boot_cmds_stay_ascii_so_ansi_codepages_can_write_them():
    """批处理按 ANSI 代码页写盘：正文里混进中文，en-US 机器（cp1252）会抛
    UnicodeEncodeError，更新在「准备切换版本」之后静默中止——CI 上真实踩过。"""
    for text in (portable_boot_cmd(1, "C:\\T\\new.exe", "C:\\App\\mem_guard.exe"),
                 setup_boot_cmd(1, "C:\\T\\Setup.exe", "C:\\App")):
        text.encode("ascii")  # 不是 ASCII 就抛 UnicodeEncodeError


def test_write_boot_cmd_falls_back_to_utf8_when_mbcs_cannot_encode(monkeypatch, tmp_path):
    """路径带非 ANSI 字符时不能整个更新链路失败：退一步用 UTF-8 写出来。"""
    monkeypatch.setattr(update_mod, "_BOOT_ENCODING", "ascii")
    path = tmp_path / "boot.cmd"
    update_mod._write_boot_cmd(str(path), "echo 中文路径")
    assert path.read_text(encoding="utf-8") == "echo 中文路径"


def test_find_checksums_asset_picks_manifest_only():
    with_ck = _ASSETS + [{"name": "checksums.txt",
                          "browser_download_url": "https://x/checksums"}]
    assert find_checksums_asset(with_ck) == {"name": "checksums.txt",
                                             "url": "https://x/checksums"}
    assert find_checksums_asset(_ASSETS) is None, "Release 没给清单时要能被发现"
    assert find_checksums_asset(None) is None


# ---------------------------------------------------------------- sha256 校验

_PAYLOAD = b"MemGuard installer payload"
_CK_URL = "https://x/checksums"


def test_sha256_of_streams_and_tolerates_missing_file(tmp_path):
    p = tmp_path / "mem_guard.exe"
    p.write_bytes(_PAYLOAD)
    assert sha256_of(str(p)) == hashlib.sha256(_PAYLOAD).hexdigest()
    assert sha256_of(str(tmp_path / "nope.exe")) == "", "读不到文件返回空串"


def test_verify_asset_reports_mismatch_and_missing_manifest(tmp_path):
    p = tmp_path / "mem_guard.exe"
    p.write_bytes(_PAYLOAD)
    good = hashlib.sha256(_PAYLOAD).hexdigest()
    assert verify_asset(str(p), good)["ok"] is True
    assert verify_asset(str(p), good.upper())["ok"] is True, "大小写不该影响比对"
    bad = verify_asset(str(p), "0" * 64)
    assert bad["ok"] is False and "sha256" in bad["msg"]
    skip = verify_asset(str(p), "")
    assert skip["ok"] is False and skip["skipped"] is True, "没有摘要 = 无从校验"


def test_verify_asset_reports_unreadable_file(tmp_path):
    r = verify_asset(str(tmp_path / "missing.exe"), "a" * 64)
    assert r["ok"] is False and "读取不到" in r["msg"]


def test_parse_checksums_accepts_common_formats():
    good = "a" * 64
    assert parse_checksums(good + "  mem_guard.exe", "mem_guard.exe") == good
    assert parse_checksums(good + " *mem_guard.exe", "mem_guard.exe") == good
    assert parse_checksums("SHA256(mem_guard.exe)= " + good, "mem_guard.exe") == good
    assert parse_checksums("sha256:" + good + "  mem_guard.exe", "mem_guard.exe") == good
    # 多行清单里按文件名对号入座，别拿到别人的摘要
    text = "b" * 64 + "  other.exe" + chr(13) + chr(10) + good + "  mem_guard.exe" + chr(13) + chr(10)
    assert parse_checksums(text, "mem_guard.exe") == good
    assert parse_checksums(text, "other.exe") == "b" * 64


def test_parse_checksums_rejects_missing_and_malformed():
    good = "c" * 64
    assert parse_checksums(good + "  other.exe", "mem_guard.exe") is None
    assert parse_checksums("", "mem_guard.exe") is None
    assert parse_checksums(good + "  mem_guard.exe", "") is None
    assert parse_checksums("# " + good + "  mem_guard.exe", "mem_guard.exe") is None, \
        "注释行不算数"
    assert parse_checksums("deadbeef  mem_guard.exe", "mem_guard.exe") is None, \
        "长度不够的摘要一律不认"


# ------------------------------------------------- 安装前的完整性闸门

class _Installer:
    """把 install_latest 的网络与子进程边界换成假的，只留 sha256 校验是真的。"""

    def __init__(self, monkeypatch, tmp_path, digest=None, no_checksums=False):
        self.state = {"popen": 0, "exit": None}
        monkeypatch.setattr(update_mod, "_frozen_exe", lambda: str(tmp_path))
        monkeypatch.setattr(update_mod, "is_installed_layout", lambda: False)
        monkeypatch.setattr(update_mod.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(update_mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(update_mod.os, "_exit",
                            lambda code: self.state.__setitem__("exit", code))
        monkeypatch.setattr(
            update_mod.subprocess, "Popen",
            lambda *a, **k: self.state.__setitem__("popen", self.state["popen"] + 1))
        assets = [dict(x) for x in _ASSETS]
        text = ""
        if not no_checksums:
            assets.append({"name": "checksums.txt", "browser_download_url": _CK_URL})
            # 清单里的文件名必须与 pick_asset 真正挑中的那个一致，否则校验的是别的包
            picked = pick_asset(assets)["name"]
            text = (digest or hashlib.sha256(_PAYLOAD).hexdigest()) + "  " + picked + chr(13) + chr(10)
        monkeypatch.setattr(
            update_mod, "fetch_latest_release", lambda timeout=8, **kw: {
                "ok": True, "tag": "v9.9.9", "url": "https://example/release",
                "newer": True, "assets": assets})
        monkeypatch.setattr(
            update_mod, "fetch_checksums", lambda url, timeout=30, **kw: {
                "ok": True, "text": text})

        def fake_download(url, dest, timeout=300, **kw):
            with open(dest, "wb") as f:
                f.write(_PAYLOAD)
            return {"ok": True}

        monkeypatch.setattr(update_mod, "download_asset", fake_download)

    def cfg(self, verify=True):
        return {"verify_update_sha256": verify}


def test_install_latest_refuses_without_manifest(monkeypatch, tmp_path):
    """Release 没给 sha256 清单：宁可不动手，也不装一个无从校验的包。"""
    it = _Installer(monkeypatch, tmp_path, no_checksums=True)
    r = update_mod.install_latest(it.cfg())
    assert r["ok"] is False and "sha256 清单" in r["msg"]
    assert it.state["popen"] == 0, "一条引导批处理都不该启动"
    assert it.state["exit"] is None
    assert not list(tmp_path.glob("mem_guard_update_*.exe")), \
        "校验不过的包必须当场删掉，不能留在临时目录"


def test_install_latest_refuses_on_digest_mismatch(monkeypatch, tmp_path):
    it = _Installer(monkeypatch, tmp_path, digest="0" * 64)
    r = update_mod.install_latest(it.cfg())
    assert r["ok"] is False and "sha256 校验不通过" in r["msg"]
    assert it.state["popen"] == 0 and it.state["exit"] is None
    assert not list(tmp_path.glob("mem_guard_update_*.exe"))


def test_install_latest_refuses_when_manifest_lacks_the_asset(monkeypatch, tmp_path):
    """清单在、但没有这个文件的摘要：同样装不了。"""
    it = _Installer(monkeypatch, tmp_path, digest="a" * 64)
    monkeypatch.setattr(
        update_mod, "fetch_checksums",
        lambda url, timeout=30, **kw:
        {"ok": True, "text": "b" * 64 + "  other.exe" + chr(13) + chr(10)})
    r = update_mod.install_latest(it.cfg())
    assert r["ok"] is False and "摘要" in r["msg"]
    assert it.state["popen"] == 0


def test_install_latest_refuses_when_manifest_unreachable(monkeypatch, tmp_path):
    it = _Installer(monkeypatch, tmp_path, digest="a" * 64)
    monkeypatch.setattr(update_mod, "fetch_checksums",
                        lambda url, timeout=30, **kw: {"ok": False, "msg": "网络不通"})
    r = update_mod.install_latest(it.cfg())
    assert r["ok"] is False and "网络不通" in r["msg"]
    assert it.state["popen"] == 0


def test_install_latest_proceeds_after_digest_matches(monkeypatch, tmp_path):
    it = _Installer(monkeypatch, tmp_path)
    ready = []
    update_mod.install_latest(it.cfg(), on_ready=lambda: ready.append(1))
    assert it.state["popen"] == 1, "校验通过才启动引导批处理"
    assert it.state["exit"] == 0, "引导批处理已接管，进程以 0 退出"
    assert ready == [1], "on_ready 要在启动批处理之后、退出之前回调"


def test_install_latest_can_skip_verification_when_configured(monkeypatch, tmp_path):
    """关掉校验是显式逃生门：老 Release 没清单时用户仍要能升级。"""
    it = _Installer(monkeypatch, tmp_path, no_checksums=True)
    update_mod.install_latest(it.cfg(verify=False))
    assert it.state["popen"] == 1 and it.state["exit"] == 0


def test_install_latest_source_mode_explains_instead_of_opening_browser(monkeypatch):
    """源码态没有可替换的 exe：说清楚为什么，别把人甩到 GitHub 网页上。"""
    monkeypatch.setattr(update_mod, "_frozen_exe", lambda: None)
    r = update_mod.install_latest({})
    assert r["ok"] is False and r["action"] == "none"
    assert "源码运行" in r["msg"]


def test_install_latest_reports_missing_asset_without_page_jump(monkeypatch, tmp_path):
    """Release 里没有可用安装包：给一句人话，同样不跳网页。"""
    monkeypatch.setattr(update_mod, "_frozen_exe", lambda: str(tmp_path))
    monkeypatch.setattr(update_mod, "fetch_latest_release", lambda timeout=8, **kw: {
        "ok": True, "tag": "v9.9.9", "url": "https://example/release",
        "newer": True,
        "assets": [{"name": "README.md", "browser_download_url": "https://x/r"}]})
    r = update_mod.install_latest({})
    assert r["ok"] is False and r["action"] == "none"
    assert "没有可用安装包" in r["msg"]


# ---------------------------------------------------------------- 断点续传

class _FakeResp:
    """最小的 urlopen 响应：body / status / Content-Length 都由用例指定。"""

    def __init__(self, body: bytes, status: int = 200, length=None):
        self._body = body
        self.status = status
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def read(self, n=-1):
        if not self._body:
            return b""
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_asset_resumes_from_partial_file(monkeypatch, tmp_path):
    """断流后必须从断点续：弱网下整包重来等于永远更不完。"""
    dest = tmp_path / "pkg.exe"
    dest.write_bytes(b"A" * 4)
    seen = {}

    def fake_open(req, timeout=None, cfg=None):
        seen["range"] = req.get_header("Range")
        return _FakeResp(b"B" * 6, status=206)

    monkeypatch.setattr(update_mod, "_open", fake_open)
    r = update_mod.download_asset("https://x/p.exe", str(dest))
    assert r["ok"] is True
    assert dest.read_bytes() == b"A" * 4 + b"B" * 6
    assert seen["range"] == "bytes=4-"


def test_download_asset_restarts_when_server_ignores_range(monkeypatch, tmp_path):
    """服务端不认 Range（回 200 而不是 206）时必须整包重写，不能追加成两份。"""
    dest = tmp_path / "pkg.exe"
    dest.write_bytes(b"A" * 4)

    monkeypatch.setattr(update_mod, "_open",
                        lambda req, timeout=None, cfg=None: _FakeResp(b"C" * 3, status=200))
    r = update_mod.download_asset("https://x/p.exe", str(dest))
    assert r["ok"] is True and dest.read_bytes() == b"CCC"


def test_download_asset_retries_after_truncated_response(monkeypatch, tmp_path):
    """对端提前关连接时 read 不抛异常：靠 Content-Length 对账发现缺口并续传。"""
    dest = tmp_path / "pkg.exe"
    calls = []

    def fake_open(req, timeout=None, cfg=None):
        calls.append(req.get_header("Range"))
        if len(calls) == 1:
            return _FakeResp(b"D" * 4, status=200, length=10)
        return _FakeResp(b"E" * 6, status=206)

    monkeypatch.setattr(update_mod, "_open", fake_open)
    monkeypatch.setattr(update_mod.time, "sleep", lambda s: None)
    r = update_mod.download_asset("https://x/p.exe", str(dest))
    assert r["ok"] is True
    assert dest.read_bytes() == b"D" * 4 + b"E" * 6
    assert calls == [None, "bytes=4-"]


def test_download_asset_cleans_up_after_all_attempts_fail(monkeypatch, tmp_path):
    """全部尝试都失败：不留半成品（未校验的 exe 躺在临时目录里更糟）。"""
    dest = tmp_path / "pkg.exe"
    dest.write_bytes(b"A" * 4)

    def boom(req, timeout=None, cfg=None):
        raise OSError("connection reset")

    monkeypatch.setattr(update_mod, "_open", boom)
    monkeypatch.setattr(update_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(update_mod, "resolve_proxy", lambda cfg=None: "")
    r = update_mod.download_asset("https://x/p.exe", str(dest), attempts=2)
    assert r["ok"] is False and "下载失败" in r["msg"]
    assert not dest.exists()


def test_purge_stale_backup_removes_only_the_bak(monkeypatch, tmp_path):
    exe = tmp_path / "mem_guard.exe"
    exe.write_bytes(b"me")
    bak = tmp_path / "mem_guard.exe.bak"
    bak.write_bytes(b"old")
    monkeypatch.setattr(update_mod, "_frozen_exe", lambda: str(exe))
    update_mod.purge_stale_backup()
    assert not bak.exists() and exe.exists(), "只删备份，不能碰正在跑的 exe"
    update_mod.purge_stale_backup(), "没有备份时静默返回"

# ---------------------------------------------------------------- 通知契约

def _cfg(**over):
    cfg = {"auto_update": True, "auto_install": False, "update_check_hours": 12,
           "last_update_check": 0, "update_notified_tag": ""}
    cfg.update(over)
    return cfg


def _fake_release(monkeypatch, tag="v9.9.9", ok=True, msg=""):
    if not ok:
        monkeypatch.setattr(update_mod, "fetch_latest_release",
                            lambda timeout=8, **kw: {"ok": False, "msg": msg or "boom"})
        return
    monkeypatch.setattr(update_mod, "fetch_latest_release", lambda timeout=8, **kw: {
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
    monkeypatch.setattr(update_mod, "fetch_latest_release", lambda timeout=8, **kw: {
        "ok": True, "tag": "v9.9.9", "url": "https://example/release",
        "newer": True, "assets": _ASSETS})
    monkeypatch.setattr(update_mod, "install_latest",
                        lambda cfg, on_ready=None, release=None, progress=None:
                        calls.append(release["tag"]) or {"ok": True})
    bubbles = []
    check_and_notify(_cfg(auto_install=True), notify=lambda m, t: bubbles.append(m),
                     on_ready=lambda: None)
    assert calls == ["v9.9.9"], "开了自动安装就不该只提醒"
    assert bubbles and "正在下载并安装" in bubbles[0]


def test_check_and_notify_never_opens_a_browser(monkeypatch):
    """后台检查只提醒，不抢浏览器焦点；v1.29.3 起连「点了检查更新」也不跳网页。"""
    assert not hasattr(update_mod, "_open_url"), "更新链路里不该再留开浏览器的口子"
    monkeypatch.setattr(update_mod, "update_config", lambda changes: None)
    monkeypatch.setattr(update_mod, "fetch_latest_release", lambda timeout=8, **kw: {
        "ok": True, "tag": "v9.9.9", "url": "https://example/release",
        "newer": True, "assets": _ASSETS})

    r = check_and_notify(_cfg(), notify=lambda m, t: None)
    assert r["action"] == "notify"


# --------------------------------------------------- v1.29.7 代理与下载进度

class _Conn:
    """socket.create_connection 的假返回值：只要求能进 with。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _no_proxy(monkeypatch):
    """清掉进程内缓存并让系统代理与探测都返回空——测「直连」时别受本机影响。"""
    monkeypatch.setattr(update_mod, "_PROXY_CACHE", {})
    monkeypatch.setattr(update_mod, "_detected_proxy", lambda: "")
    monkeypatch.setattr(update_mod, "_probe_local_proxy", lambda: "")
    monkeypatch.setattr(update_mod, "log", lambda m: None)


def test_resolve_proxy_prefers_explicit_config(monkeypatch):
    """显式 update_proxy 最优先：用户写了就照办，不探测、不看系统代理。"""
    monkeypatch.setattr(update_mod, "_PROXY_CACHE", {})
    monkeypatch.setattr(update_mod, "log", lambda m: None)
    monkeypatch.setattr(update_mod, "_detected_proxy",
                        lambda: (_ for _ in ()).throw(AssertionError("不该读系统代理")))
    monkeypatch.setattr(update_mod, "_probe_local_proxy",
                        lambda: (_ for _ in ()).throw(AssertionError("不该探测端口")))
    assert update_mod.resolve_proxy(
        {"update_proxy": " 127.0.0.1:7897 "}) == "http://127.0.0.1:7897"


def test_resolve_proxy_uses_detected_system_proxy_before_probing(monkeypatch):
    monkeypatch.setattr(update_mod, "_PROXY_CACHE", {})
    monkeypatch.setattr(update_mod, "log", lambda m: None)
    monkeypatch.setattr(update_mod, "_detected_proxy", lambda: "http://127.0.0.1:10809")
    monkeypatch.setattr(update_mod, "_probe_local_proxy",
                        lambda: (_ for _ in ()).throw(AssertionError("不该探测端口")))
    assert update_mod.resolve_proxy({}) == "http://127.0.0.1:10809"


def test_resolve_proxy_probes_common_local_ports(monkeypatch):
    """系统代理没开时按常见端口探测：Clash 7897 不通就试 7890。"""
    monkeypatch.setattr(update_mod, "_PROXY_CACHE", {})
    monkeypatch.setattr(update_mod, "log", lambda m: None)
    monkeypatch.setattr(update_mod, "_detected_proxy", lambda: "")
    seen = []

    def fake_connect(addr, timeout=None):
        seen.append(addr)
        if addr[1] == 7890:
            return _Conn()
        raise OSError("connection refused")

    monkeypatch.setattr(update_mod.socket, "create_connection", fake_connect)
    assert update_mod.resolve_proxy({}) == "http://127.0.0.1:7890"
    assert seen == [("127.0.0.1", 7897), ("127.0.0.1", 7890)]


def test_resolve_proxy_is_cached_and_falls_back_to_direct(monkeypatch):
    """一次更新要发三个请求：探测结果必须缓存；一个都没有就直连。"""
    calls = []
    _no_proxy(monkeypatch)

    def fake_probe():
        calls.append(1)
        return ""

    monkeypatch.setattr(update_mod, "_probe_local_proxy", fake_probe)
    assert update_mod.resolve_proxy({}) == ""
    assert update_mod.resolve_proxy({}) == ""
    assert calls == [1], "同一个进程里不该反复探测"


def test_proxy_label_never_leaks_credentials():
    """日志只写 host:port：代理 URL 里的用户名密码绝不进日志。"""
    assert update_mod._proxy_label(
        "http://alice:s3cret@127.0.0.1:7897") == "127.0.0.1:7897"
    assert "s3cret" not in update_mod._proxy_label(
        "http://alice:s3cret@127.0.0.1:7897")


def test_open_routes_through_the_resolved_proxy(monkeypatch):
    """三处 urlopen 统一走 _open：代理最终落进 ProxyHandler 的 http/https。"""
    seen = {}

    class _Opener:
        def open(self, req, timeout=None):
            seen["timeout"] = timeout
            return "resp"

    handlers = []

    def fake_build(*h):
        handlers.extend(h)
        return _Opener()

    monkeypatch.setattr(update_mod.urllib.request, "build_opener", fake_build)
    r = update_mod._open("REQ", 5, {"update_proxy": "http://127.0.0.1:7897"})
    assert r == "resp" and seen["timeout"] == 5
    assert handlers[0].proxies == {"http": "http://127.0.0.1:7897",
                                   "https": "http://127.0.0.1:7897"}


def test_progress_text_is_human_readable():
    assert update_mod.progress_text(
        {"done": 1048576, "total": 2097152}) == "正在下载更新 50%（1.0MB/2.0MB）"
    assert update_mod.progress_text({"done": 1048576}) == "正在下载更新 1.0MB"
    assert update_mod.progress_text(None) == "正在下载更新 0.0MB"


def test_download_asset_logs_progress_and_calls_back(monkeypatch, tmp_path):
    """弱网下要能看出它其实在动：日志一行已下多少，回调同步给托盘。"""
    dest = tmp_path / "pkg.exe"
    logs, seen = [], []
    monkeypatch.setattr(update_mod, "log", logs.append)
    monkeypatch.setattr(update_mod, "_open",
                        lambda req, timeout=None, cfg=None: _FakeResp(b"F" * 3, status=200))
    r = update_mod.download_asset("https://x/p.exe", str(dest), progress=seen.append)
    assert r["ok"] is True
    assert seen and seen[0]["done"] == 3 and seen[0]["total"] == 3
    assert any("更新包下载中" in m for m in logs)
    assert any("更新包下载完成" in m for m in logs)


def test_download_asset_never_breaks_on_a_faulty_progress_callback(monkeypatch, tmp_path):
    """回调是 UI 的事：它抛异常也不能把下载带崩。"""
    dest = tmp_path / "pkg.exe"
    monkeypatch.setattr(update_mod, "log", lambda m: None)
    monkeypatch.setattr(update_mod, "_open",
                        lambda req, timeout=None, cfg=None: _FakeResp(b"G" * 3, status=200))

    def boom(_info):
        raise RuntimeError("ui 炸了")

    r = update_mod.download_asset("https://x/p.exe", str(dest), progress=boom)
    assert r["ok"] is True and dest.read_bytes() == b"GGG"


def test_download_failure_explains_how_to_set_a_proxy(monkeypatch, tmp_path):
    """直连失败时把「怎么配代理」写进失败原因；已经走代理就不再劝人配。"""
    monkeypatch.setattr(update_mod, "log", lambda m: None)
    monkeypatch.setattr(update_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(update_mod, "_open",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("timed out")))
    monkeypatch.setattr(update_mod, "resolve_proxy", lambda cfg=None: "")
    r = update_mod.download_asset("https://x/p.exe", str(tmp_path / "p.exe"), attempts=1)
    assert r["ok"] is False and "update_proxy" in r["msg"]

    monkeypatch.setattr(update_mod, "resolve_proxy",
                        lambda cfg=None: "http://127.0.0.1:7897")
    r2 = update_mod.download_asset("https://x/p.exe", str(tmp_path / "p2.exe"), attempts=1)
    assert r2["ok"] is False and "update_proxy" not in r2["msg"]


class _JsonResp:
    """最小 JSON 响应：fetch_latest_release 只用到 read 与 with。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_latest_release_falls_back_to_direct_when_proxy_is_rate_limited(monkeypatch):
    """真机踩过：共享代理出口把 GitHub 匿名配额用光（403），直连却是 200。

    查询接口只有几 KB，降级直连重试一次即可；下载仍然走代理（那才是慢的一段）。
    """
    calls = []
    reqs = []
    body = json.dumps({"tag_name": "v9.9.9", "html_url": "https://x",
                       "assets": []}).encode()

    def fake_open(req, timeout=None, cfg=None, direct=False):
        calls.append(direct)
        reqs.append(req)
        if not direct:
            raise urllib.error.HTTPError("u", 403, "rate limit", None, None)
        return _JsonResp(body)

    monkeypatch.setattr(update_mod, "_open", fake_open)
    monkeypatch.setattr(update_mod, "resolve_proxy", lambda cfg=None: "http://127.0.0.1:7897")
    monkeypatch.setattr(update_mod, "log", lambda m: None)
    r = update_mod.fetch_latest_release(cfg={})
    assert r["ok"] is True and r["tag"] == "v9.9.9"
    assert calls == [False, True], "先代理、被限流后直连，各一次"
    assert reqs[0] is not reqs[1], \
        "重试必须新建 Request：ProxyHandler 会原地改写请求对象（真机踩过）"


def test_fetch_latest_release_reports_rate_limit_without_a_proxy(monkeypatch):
    """直连也被限流时说清是配额问题，不要含糊成一句「HTTP 403」。"""
    def fake_open(req, timeout=None, cfg=None, direct=False):
        raise urllib.error.HTTPError("u", 403, "rate limit", None, None)

    monkeypatch.setattr(update_mod, "_open", fake_open)
    monkeypatch.setattr(update_mod, "resolve_proxy", lambda cfg=None: "")
    monkeypatch.setattr(update_mod, "log", lambda m: None)
    r = update_mod.fetch_latest_release(cfg={})
    assert r["ok"] is False and "配额" in r["msg"]
