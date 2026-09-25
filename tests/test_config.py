# -*- coding: utf-8 -*-
"""config：配置校验与钳制、黑名单归一化、容量格式化。"""
import json

import pytest

from memguard import config
from memguard.config import (
    DEFAULT_CONFIG,
    _blacklist_stems,
    _clamp_int,
    _norm_proc_name,
    gb,
    normalize_config,
)


def test_default_config_keys_present():
    for key in ("phys_threshold", "commit_threshold", "interval", "cooldown",
                "auto_clean", "debounce_sec", "clean_level", "user_blacklist",
                "warn_margin", "advice_refresh_sec"):
        assert key in DEFAULT_CONFIG


def test_advice_refresh_sec_default_and_clamp():
    """建议条数刷新间隔：默认 60s，钳到 [15, 600]，非法值回落默认。"""
    assert normalize_config({})["advice_refresh_sec"] == 60
    assert normalize_config({"advice_refresh_sec": 1})["advice_refresh_sec"] == 15
    assert normalize_config({"advice_refresh_sec": 99999})["advice_refresh_sec"] == 600
    assert normalize_config({"advice_refresh_sec": "abc"})["advice_refresh_sec"] == 60


@pytest.mark.parametrize("raw,expected", [
    ("csrss.exe", "csrss"),
    ("Chrome.EXE", "chrome"),
    ("  chrome  ", "chrome"),
    ("", ""),
    (None, ""),
    ("a.exe.exe", "a.exe"),   # 只去掉一个 .exe 后缀
])
def test_norm_proc_name(raw, expected):
    assert _norm_proc_name(raw) == expected


def test_blacklist_stems_filters_and_normalizes():
    assert _blacklist_stems(["Chrome.EXE", "chrome", 123, "", None]) == {"chrome"}
    assert _blacklist_stems(None) == set()


@pytest.mark.parametrize("value,expected", [
    (5, 50),        # 低于下限 -> 取下限
    (999, 99),      # 高于上限 -> 取上限
    (85, 85),
    ("80", 80),
    ("abc", 85),    # 非数字 -> 默认值
    (None, 85),
])
def test_clamp_int(value, expected):
    assert _clamp_int(value, 50, 99, 85) == expected


def test_normalize_config_clamps_and_keeps_unknown():
    cfg = normalize_config({
        "clean_level": "XX",
        "phys_threshold": 999,
        "commit_threshold": 1,
        "interval": 0,
        "user_blacklist": ["Chrome.EXE", "chrome"],
        "custom_key": 1,
    })
    assert cfg["clean_level"] == "conservative"
    assert cfg["phys_threshold"] == 99
    assert cfg["commit_threshold"] == 50
    assert cfg["interval"] == 2
    assert cfg["user_blacklist"] == ["chrome"]
    assert cfg["custom_key"] == 1        # 未知键原样保留


def test_normalize_config_accepts_non_dict():
    cfg = normalize_config(None)
    assert cfg["clean_level"] == "conservative"
    assert cfg["phys_threshold"] == DEFAULT_CONFIG["phys_threshold"]


def test_gb_format():
    assert gb(1024 ** 3) == "1.0GB"
    assert gb(0) == "0.0GB"


def test_normalize_config_new_fields_defaults():
    cfg = normalize_config({})
    assert cfg["min_avail_mb"] == 0
    assert cfg["scheduled_minutes"] == 0
    assert cfg["clean_on_start"] is False
    assert cfg["clean_areas"] == {
        "standby": True, "low_priority_standby": True, "modified": True,
        "file_cache": True, "working_sets": True,
    }
    assert cfg["stats"] == {"count": 0, "freed": 0}


def test_normalize_config_new_fields_clamped():
    cfg = normalize_config({
        "clean_areas": {"standby": False, "bogus": True},   # 未知键丢弃，缺失沿用默认
        "min_avail_mb": -5,
        "scheduled_minutes": 99999,
        "stats": {"count": "3", "freed": 1024},
    })
    assert cfg["clean_areas"]["standby"] is False
    assert cfg["clean_areas"]["modified"] is True
    assert "bogus" not in cfg["clean_areas"]
    assert cfg["min_avail_mb"] == 0
    assert cfg["scheduled_minutes"] == 24 * 60
    assert cfg["stats"] == {"count": 3, "freed": 1024}


# ---------------------------------------------------------------- update_config（唯一写盘入口）

def _write_cfg(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_update_config_merges_into_on_disk_config(monkeypatch, tmp_path):
    """以磁盘当前配置为基准做增量合并，而不是拿内存里的旧副本整份覆盖。"""
    cfg_file = tmp_path / "mem_guard.json"
    _write_cfg(cfg_file, {"phys_threshold": 77, "cooldown": 900, "auto_clean": False})
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    merged = config.update_config({"auto_clean": True})

    assert merged["phys_threshold"] == 77, "磁盘上的其它字段不能被默认值覆盖"
    assert merged["cooldown"] == 900
    assert merged["auto_clean"] is True
    assert json.loads(cfg_file.read_text(encoding="utf-8")) == merged, "落盘内容与返回一致"


def test_update_config_survives_stale_snapshot(monkeypatch, tmp_path):
    """拿着过期副本发起变更，也不会把期间别人的写入回滚掉。"""
    cfg_file = tmp_path / "mem_guard.json"
    _write_cfg(cfg_file, {"cooldown": 300, "auto_clean": True})
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    stale = config.load_config()                  # 过期副本（之后文件还会被改）
    config.update_config({"auto_clean": False})   # 别的写入先落盘
    stale.update({"cooldown": 120})               # 基于过期副本算出的变更
    config.update_config({"cooldown": 120})

    final = config.load_config()
    assert final["auto_clean"] is False, "并发/先到的写入不能被回滚"
    assert final["cooldown"] == 120


def test_update_config_normalizes_values(monkeypatch, tmp_path):
    """合并时仍走 normalize_config：越界/脏值被钳制，不会把配置写坏。"""
    cfg_file = tmp_path / "mem_guard.json"
    _write_cfg(cfg_file, {})
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    merged = config.update_config({"phys_threshold": 999, "clean_level": "xx"})

    assert merged["phys_threshold"] == 99
    assert merged["clean_level"] == "conservative"
