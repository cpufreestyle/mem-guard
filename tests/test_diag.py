# -*- coding: utf-8 -*-
"""diag：诊断导出的内容契约。

现场报告的唯一用途是「让人不看机器就能判断自动调优动过什么」，所以这里锁三件
事：打包了哪些文件、正文摘要里有哪几段、以及「自适应决策痕迹」是不是每条自适应
都有一行（开关态 + 爬山进度全带上）。任何一段缺失，导出去的 zip 就回答不了
「这个阈值为什么被自动改了」。
"""
import os
import zipfile

from memguard.adapters import ADAPTERS
from memguard import diag
from memguard.config import __version__, normalize_config


def _fake_env(monkeypatch, tmp_path, with_config=True, with_log=True):
    """把 export_diagnostics 依赖的状态读取与落盘路径全换成假的。"""
    monkeypatch.setattr(diag, "BASE_DIR", str(tmp_path))
    if with_config:
        cfg = tmp_path / "mem_guard.json"
        cfg.write_text('{"phys_threshold": 88}', encoding="utf-8")
        monkeypatch.setattr(diag, "CONFIG_PATH", str(cfg))
    else:
        monkeypatch.setattr(diag, "CONFIG_PATH", str(tmp_path / "nope.json"))
    if with_log:
        log = tmp_path / "mem_guard.log"
        log.write_text("[2026-10-06 00:00:00] hello\n", encoding="utf-8")
        monkeypatch.setattr(diag, "LOG_PATH", str(log))
    else:
        monkeypatch.setattr(diag, "LOG_PATH", str(tmp_path / "nope.log"))
    monkeypatch.setattr(diag, "get_mem", lambda: {
        "phys_pct": 70.0, "used_phys": 8 * 1024 ** 3, "total_phys": 16 * 1024 ** 3,
        "commit_pct": 60.0, "avail_commit": 4 * 1024 ** 3,
        "total_commit": 16 * 1024 ** 3,
    })
    monkeypatch.setattr(diag, "load_config", lambda: normalize_config(
        {"level_adapt_done": True, "threshold_adapt_done": 1, "min_mb_adapt": False}))
    monkeypatch.setattr(diag, "is_admin", lambda: True)
    monkeypatch.setattr(diag, "autostart_enabled", lambda use_cache=True: False)
    monkeypatch.setattr(diag, "privilege_state", lambda p: "on")
    monkeypatch.setattr(diag, "top_processes_list",
                        lambda n: [("chrome.exe", 2 * 1024 ** 3, 1234)])


def _adaptive_segment(txt):
    parts = txt.split("--- 自适应决策痕迹 ---")
    assert len(parts) == 2, "正文里必须恰好有这一段"
    return parts[1].split("--- 物理占用 Top20 ---")[0]


def test_export_packs_zip_and_summary(monkeypatch, tmp_path):
    _fake_env(monkeypatch, tmp_path)
    path = diag.export_diagnostics()
    assert not path.startswith("ERR:"), path
    assert os.path.exists(path)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        txt = z.read("diagnostics.txt").decode("utf-8")
        assert z.read("mem_guard.json").decode("utf-8").startswith("{")
        assert "hello" in z.read("mem_guard.log").decode("utf-8")
    assert set(names) == {"diagnostics.txt", "mem_guard.json", "mem_guard.log"}
    assert ("version=" + __version__) in txt
    assert "物理 70.0%" in txt and "提交 60.0%" in txt
    assert "autostart=False" in txt
    assert "chrome.exe" in txt, "Top20 采样要进正文"


def test_adaptive_trace_has_one_line_per_adapter(monkeypatch, tmp_path):
    """8 条自适应一条不缺，开关态与爬山进度都要在。"""
    _fake_env(monkeypatch, tmp_path)
    path = diag.export_diagnostics()
    with zipfile.ZipFile(path) as z:
        txt = z.read("diagnostics.txt").decode("utf-8")
    lines = [l for l in _adaptive_segment(txt).splitlines() if l.startswith("adapt ")]
    assert len(lines) == len(ADAPTERS), lines
    joined = "\n".join(lines)
    assert "已触发（一次性）" in joined, "level_adapt_done 置位了要能看出来"
    assert "已加 1/2 级" in joined, "threshold_adapt_done 爬到第 1 级要能看出来"
    assert "开关 off" in joined, "min_mb_adapt 关掉要能看出来"
    assert "开关 on" in joined, "没配过的开关按缺省开算"


def test_export_includes_guard_status(monkeypatch, tmp_path):
    """守护状态三行 + 用户空闲秒数进 zip：现场报告不必再问「热键生没生效」。"""
    _fake_env(monkeypatch, tmp_path)
    monkeypatch.setattr(diag.idle, "idle_seconds", lambda: 1900.0)
    lines = ["全局热键：Ctrl+Alt+M→立即清理", "全屏让路：开（未暂停）",
             "前台进程保护：开"]
    path = diag.export_diagnostics(lines)
    assert not path.startswith("ERR:"), path
    with zipfile.ZipFile(path) as z:
        txt = z.read("diagnostics.txt").decode("utf-8")
    assert "--- 守护状态 ---" in txt
    for line in lines:
        assert line in txt
    assert "用户空闲: 1900 秒" in txt
    assert "notify_idle_sec=300" in txt


def test_export_without_status_lines_still_works(monkeypatch, tmp_path):
    """调用方没给（旧调用点 / 手动跑脚本）也要出 zip，只少那三行。"""
    _fake_env(monkeypatch, tmp_path)
    monkeypatch.setattr(diag.idle, "idle_seconds", lambda: None)
    path = diag.export_diagnostics()
    assert not path.startswith("ERR:"), path
    with zipfile.ZipFile(path) as z:
        txt = z.read("diagnostics.txt").decode("utf-8")
    assert "--- 守护状态 ---" in txt
    assert "用户空闲: 未知" in txt


def test_export_survives_missing_config_and_log(monkeypatch, tmp_path):
    """配置文件 / 日志不在时也要出得了 zip，只少两个附件。"""
    _fake_env(monkeypatch, tmp_path, with_config=False, with_log=False)
    path = diag.export_diagnostics()
    assert not path.startswith("ERR:"), path
    with zipfile.ZipFile(path) as z:
        assert z.namelist() == ["diagnostics.txt"]


def test_export_reports_failure_instead_of_raising(monkeypatch, tmp_path):
    """读状态抛异常时返回 ERR:...，绝不把异常甩给菜单回调。"""
    _fake_env(monkeypatch, tmp_path)

    def boom():
        raise RuntimeError("state gone")

    monkeypatch.setattr(diag, "get_mem", boom)
    out = diag.export_diagnostics()
    assert out.startswith("ERR:") and "state gone" in out
