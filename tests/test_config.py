# -*- coding: utf-8 -*-
"""config：配置校验与钳制、黑名单归一化、容量格式化。"""
import json

import pytest

from memguard import config
from memguard.config import (
    DEFAULT_CONFIG,
    TARGET_CLEAN_TOP_MAX,
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


def test_load_config_tolerates_utf8_bom(monkeypatch, tmp_path):
    """BOM config file (Set-Content artifact) must still load."""
    cfg_file = tmp_path / "mem_guard.json"
    body = json.dumps({"clean_level": "aggressive"}, ensure_ascii=False)
    cfg_file.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    cfg = config.load_config()

    assert cfg["clean_level"] == "aggressive", "BOM must not fall back"


def test_load_config_reads_plain_utf8(monkeypatch, tmp_path):
    """Plain UTF-8 (no BOM) config still loads with utf-8-sig reader."""
    cfg_file = tmp_path / "mem_guard.json"
    _write_cfg(cfg_file, {"clean_level": "conservative"})
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    assert config.load_config()["clean_level"] == "conservative"


# ---------------------------------------------------------------- 自动更新（v1.5.0）

def test_normalize_config_auto_update_defaults():
    cfg = normalize_config({})
    assert cfg["auto_update"] is True
    assert cfg["auto_install"] is False
    assert cfg["update_check_hours"] == 12
    assert cfg["last_update_check"] == 0.0
    assert cfg["update_notified_tag"] == ""


@pytest.mark.parametrize("raw,expected", [
    (0, 1),            # 低于下限 -> 1 小时
    (99999, 168),      # 高于上限 -> 1 周
    (36, 36),
    ("24", 24),
    ("abc", 12),       # 非数字 -> 默认
    (None, 12),
])
def test_normalize_config_update_check_hours_clamp(raw, expected):
    assert normalize_config({"update_check_hours": raw})["update_check_hours"] == expected


def test_normalize_config_auto_update_toggles_are_booleans():
    cfg = normalize_config({"auto_update": 0, "auto_install": 1})
    assert cfg["auto_update"] is False
    assert cfg["auto_install"] is True


def test_normalize_config_escalate_defaults_true():
    """自动升档清理默认开启（用户要的自动优化内存能力）。"""
    assert normalize_config({})["escalate_clean"] is True


def test_normalize_config_escalate_is_boolean():
    cfg = normalize_config({"escalate_clean": 0})
    assert cfg["escalate_clean"] is False


def test_normalize_config_last_update_check_tolerates_dirty_values():
    """时间戳是程序自己维护的字段：脏值不能把加载配置这一步带崩。"""
    assert normalize_config({"last_update_check": "not-a-number"})["last_update_check"] == 0.0
    assert normalize_config({"last_update_check": "123.5"})["last_update_check"] == 123.5
    assert normalize_config({"last_update_check": -5})["last_update_check"] == 0.0
    assert normalize_config({"update_notified_tag": None})["update_notified_tag"] == ""

def test_normalize_config_stats_escalated_roundtrip():
    """升档计数只持久化非 0 值：0/缺失都不落键，统计行按有无决定显示。"""
    cfg = normalize_config({"stats": {"count": 3, "freed": 100, "escalated": 2}})
    assert cfg["stats"] == {"count": 3, "freed": 100, "escalated": 2}
    assert normalize_config({"stats": {"count": 3, "freed": 100,
                                       "escalated": 0}})["stats"] == {
        "count": 3, "freed": 100}
    assert normalize_config({"stats": {"count": 3, "freed": 100}})["stats"] == {
        "count": 3, "freed": 100}


def test_normalize_config_level_adapt_defaults_and_coercion():
    """档位自调优（v1.6.0）：开关默认开、闩默认关，脏值一律 bool 强转不崩。"""
    cfg = normalize_config({})
    assert cfg["auto_level_adapt"] is True
    assert cfg["level_adapt_done"] is False
    assert normalize_config({"auto_level_adapt": 0})["auto_level_adapt"] is False
    assert normalize_config({"auto_level_adapt": 1})["auto_level_adapt"] is True
    assert normalize_config({"level_adapt_done": 1})["level_adapt_done"] is True


def test_default_config_target_clean_keys_present():
    """定向清理内存大户（v1.7.0）三个键必须有默认值，否则老配置行为漂移。"""
    for key in ("target_clean", "target_clean_min_mb", "target_clean_top"):
        assert key in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["target_clean"] is True
    assert d["target_clean_min_mb"] == 1024
    assert d["target_clean_top"] == 3


def test_normalize_config_target_clean_coercion_and_clamp():
    """开关 bool 强转；阈值钳在 [128, 32768]MB、条数钳在 [1, 10]，脏值回落默认。"""
    assert normalize_config({"target_clean": 0})["target_clean"] is False
    assert normalize_config({"target_clean": 1})["target_clean"] is True
    assert normalize_config({"target_clean_min_mb": 16})["target_clean_min_mb"] == 128
    assert normalize_config({"target_clean_min_mb": 99999})["target_clean_min_mb"] == 32768
    assert normalize_config({"target_clean_min_mb": "abc"})["target_clean_min_mb"] == 1024
    assert normalize_config({"target_clean_top": 0})["target_clean_top"] == 1
    assert normalize_config({"target_clean_top": 99})["target_clean_top"] == 10
    assert normalize_config({"target_clean_top": None})["target_clean_top"] == 3


def test_normalize_config_stats_targeted_roundtrip():
    """定向计数只持久化非 0 值：0/缺失都不落键，统计行按有无决定显示。"""
    cfg = normalize_config({"stats": {"count": 3, "freed": 100, "targeted": 2}})
    assert cfg["stats"] == {"count": 3, "freed": 100, "targeted": 2}
    zero = normalize_config({"stats": {"count": 3, "freed": 100, "targeted": 0}})
    assert zero["stats"] == {"count": 3, "freed": 100}
    plain = normalize_config({"stats": {"count": 3, "freed": 100}})
    assert plain["stats"] == {"count": 3, "freed": 100}


def test_default_config_stage_deepen_key_present():
    """定向加深（v1.13.0）开关必须有默认值。"""
    assert "stage_deepen" in DEFAULT_CONFIG
    assert normalize_config({})["stage_deepen"] is True


def test_normalize_config_stage_deepen_coercion():
    """stage_deepen bool 强转，真值语义与其它开关一致。"""
    assert normalize_config({"stage_deepen": 0})["stage_deepen"] is False
    assert normalize_config({"stage_deepen": "off"})["stage_deepen"] is True


def test_normalize_config_stage_deepen_rounds_clamped():
    """加深轮数（v1.14.0）默认 1 = 单轮加深，钳在 [1, 3]，脏值回落默认。"""
    assert "stage_deepen_rounds" in DEFAULT_CONFIG
    assert normalize_config({})["stage_deepen_rounds"] == 1
    assert normalize_config({"stage_deepen_rounds": 3})["stage_deepen_rounds"] == 3
    assert normalize_config({"stage_deepen_rounds": 9})["stage_deepen_rounds"] == 3
    assert normalize_config({"stage_deepen_rounds": 0})["stage_deepen_rounds"] == 1
    assert normalize_config({"stage_deepen_rounds": -2})["stage_deepen_rounds"] == 1
    assert normalize_config({"stage_deepen_rounds": None})["stage_deepen_rounds"] == 1
    assert normalize_config({"stage_deepen_rounds": "x"})["stage_deepen_rounds"] == 1


def test_normalize_config_target_headroom_pct_clamped():
    """降压余量（v1.14.0）默认 0=关，钳在 [0, 20]，负值与脏值都回 0。"""
    assert "target_headroom_pct" in DEFAULT_CONFIG
    assert normalize_config({})["target_headroom_pct"] == 0
    assert normalize_config({"target_headroom_pct": 5})["target_headroom_pct"] == 5
    assert normalize_config({"target_headroom_pct": 99})["target_headroom_pct"] == 20
    assert normalize_config({"target_headroom_pct": -5})["target_headroom_pct"] == 0
    assert normalize_config({"target_headroom_pct": None})["target_headroom_pct"] == 0
    assert normalize_config({"target_headroom_pct": "x"})["target_headroom_pct"] == 0


def test_normalize_config_stats_deepen_roundtrip():
    """加深计数只持久化非 0 值：0/缺失都不落键，统计行按有无决定显示。"""
    cfg = normalize_config({"stats": {"count": 3, "freed": 100, "deepen": 2}})
    assert cfg["stats"] == {"count": 3, "freed": 100, "deepen": 2}
    zero = normalize_config({"stats": {"count": 3, "freed": 100, "deepen": 0}})
    assert zero["stats"] == {"count": 3, "freed": 100}
    plain = normalize_config({"stats": {"count": 3, "freed": 100}})
    assert plain["stats"] == {"count": 3, "freed": 100}

def test_bg_trim_default_and_toggle():
    """后台进程工作集清理：默认开（打扰小、覆盖面比定向大户全），显式关要认。"""
    assert normalize_config({})["bg_trim"] is True
    assert DEFAULT_CONFIG["bg_trim"] is True
    assert normalize_config({"bg_trim": 0})["bg_trim"] is False
    assert normalize_config({"bg_trim": "no"})["bg_trim"] is True


def test_normalize_config_stats_bg_trimmed_roundtrip():
    """后台清理计数只持久化非 0 值：0/缺失都不落键，统计行按有无决定显示。"""
    cfg = normalize_config({"stats": {"count": 3, "freed": 100, "bg_trimmed": 2}})
    assert cfg["stats"] == {"count": 3, "freed": 100, "bg_trimmed": 2}
    zero = normalize_config({"stats": {"count": 3, "freed": 100, "bg_trimmed": 0}})
    assert zero["stats"] == {"count": 3, "freed": 100}
    plain = normalize_config({"stats": {"count": 3, "freed": 100}})
    assert plain["stats"] == {"count": 3, "freed": 100}


def test_default_config_predict_clean_keys_present():
    """趋势预防式清理（v1.8.0）两个键必须有默认值，否则老配置行为漂移。"""
    for key in ("predict_clean", "predict_window_min"):
        assert key in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["predict_clean"] is True
    assert d["predict_window_min"] == 5


def test_normalize_config_predict_clean_coercion_and_clamp():
    """开关 bool 强转；预测窗口钳在 [1, 60] 分钟，脏值回落默认。"""
    assert normalize_config({"predict_clean": 0})["predict_clean"] is False
    assert normalize_config({"predict_clean": 1})["predict_clean"] is True
    assert normalize_config({"predict_window_min": 0})["predict_window_min"] == 1
    assert normalize_config({"predict_window_min": -3})["predict_window_min"] == 1
    assert normalize_config({"predict_window_min": 999})["predict_window_min"] == 60
    assert normalize_config({"predict_window_min": "abc"})["predict_window_min"] == 5
    assert normalize_config({"predict_window_min": None})["predict_window_min"] == 5


def test_normalize_config_stats_preventive_roundtrip():
    """预防计数只持久化非 0 值：0/缺失都不落键，统计行按有无决定显示。"""
    cfg = normalize_config({"stats": {"count": 3, "freed": 100, "preventive": 2}})
    assert cfg["stats"] == {"count": 3, "freed": 100, "preventive": 2}
    zero = normalize_config({"stats": {"count": 3, "freed": 100, "preventive": 0}})
    assert zero["stats"] == {"count": 3, "freed": 100}
    plain = normalize_config({"stats": {"count": 3, "freed": 100}})
    assert plain["stats"] == {"count": 3, "freed": 100}


def test_default_config_effect_track_keys_present():
    """清理效果闭环（v1.9.0）两个键必须有默认值，否则老配置行为漂移。"""
    for key in ("effect_track", "effect_min_relief_sec"):
        assert key in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["effect_track"] is True
    assert d["effect_min_relief_sec"] == 600


def test_normalize_config_effect_track_coercion_and_clamp():
    """开关 bool 强转；效果下限钳在 [60, 86400] 秒，脏值回落默认。"""
    assert normalize_config({"effect_track": 0})["effect_track"] is False
    assert normalize_config({"effect_track": 1})["effect_track"] is True
    assert normalize_config({"effect_min_relief_sec": 0})["effect_min_relief_sec"] == 60
    assert normalize_config({"effect_min_relief_sec": -3})["effect_min_relief_sec"] == 60
    assert normalize_config({"effect_min_relief_sec": 999999})["effect_min_relief_sec"] == 86400
    assert normalize_config({"effect_min_relief_sec": "abc"})["effect_min_relief_sec"] == 600
    assert normalize_config({"effect_min_relief_sec": None})["effect_min_relief_sec"] == 600


def test_normalize_config_stats_short_relief_roundtrip():
    """短效计数只持久化非 0 值：0/缺失都不落键，统计行按有无决定显示。"""
    cfg = normalize_config({"stats": {"count": 3, "freed": 100, "short_relief": 2}})
    assert cfg["stats"] == {"count": 3, "freed": 100, "short_relief": 2}
    zero = normalize_config({"stats": {"count": 3, "freed": 100, "short_relief": 0}})
    assert zero["stats"] == {"count": 3, "freed": 100}
    plain = normalize_config({"stats": {"count": 3, "freed": 100}})
    assert plain["stats"] == {"count": 3, "freed": 100}

def test_default_config_sticky_and_stage_learn_keys_present():
    """持续压力粘滞激进 + 阶梯阶段自学习（v1.12.0）三个键必须有默认值。"""
    for key in ("sticky_aggressive", "stage_learn", "stage_learn_strikes"):
        assert key in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["sticky_aggressive"] is True
    assert d["stage_learn"] is True
    assert d["stage_learn_strikes"] == 3

def test_normalize_config_sticky_and_stage_learn_coercion_and_clamp():
    """两个开关 bool 强转；strikes 钳在 [2, 10] 次，脏值回落默认。"""
    assert normalize_config({"sticky_aggressive": 0})["sticky_aggressive"] is False
    assert normalize_config({"stage_learn": "off"})["stage_learn"] is True
    assert normalize_config({"stage_learn_strikes": 1})["stage_learn_strikes"] == 2
    assert normalize_config({"stage_learn_strikes": 99})["stage_learn_strikes"] == 10
    assert normalize_config({"stage_learn_strikes": "x"})["stage_learn_strikes"] == 3
    assert normalize_config({"stage_learn_strikes": None})["stage_learn_strikes"] == 3

def test_normalize_config_stats_sticky_roundtrip():
    """粘滞计数只持久化非 0 值：0/缺失都不落键，统计行按有无决定显示。"""
    cfg = normalize_config({"stats": {"count": 3, "freed": 100, "sticky": 2}})
    assert cfg["stats"] == {"count": 3, "freed": 100, "sticky": 2}
    zero = normalize_config({"stats": {"count": 3, "freed": 100, "sticky": 0}})
    assert zero["stats"] == {"count": 3, "freed": 100}
    plain = normalize_config({"stats": {"count": 3, "freed": 100}})
    assert plain["stats"] == {"count": 3, "freed": 100}

def test_default_config_v15_keys_present():
    """v1.15.0 三键（加深收益衰减 + 覆盖面自适应开关）必须有默认值。"""
    assert "stage_deepen_diminish_pct" in DEFAULT_CONFIG
    assert "target_top_adapt" in DEFAULT_CONFIG
    assert "target_top_adapt_done" in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["stage_deepen_diminish_pct"] == 50
    assert d["target_top_adapt"] is True
    assert d["target_top_adapt_done"] == 0, "爬山计数：0=从未加宽"


def test_normalize_config_stage_deepen_diminish_pct_clamped():
    """加深收益衰减阈值钳在 [0, 100]、缺省 50：越界与脏值都回落。"""
    assert normalize_config(
        {"stage_deepen_diminish_pct": 80})["stage_deepen_diminish_pct"] == 80
    assert normalize_config(
        {"stage_deepen_diminish_pct": 999})["stage_deepen_diminish_pct"] == 100
    assert normalize_config(
        {"stage_deepen_diminish_pct": -5})["stage_deepen_diminish_pct"] == 0
    assert normalize_config(
        {"stage_deepen_diminish_pct": None})["stage_deepen_diminish_pct"] == 50
    assert normalize_config(
        {"stage_deepen_diminish_pct": "x"})["stage_deepen_diminish_pct"] == 50


def test_normalize_config_target_top_adapt_coercion():
    """覆盖面自适应开关 bool 强转；done 是爬山计数 int，clamp 到 [0, TARGET_CLEAN_TOP_MAX]。"""
    assert normalize_config({"target_top_adapt": 0})["target_top_adapt"] is False
    assert normalize_config({"target_top_adapt": "off"})["target_top_adapt"] is True
    assert normalize_config({"target_top_adapt_done": 1})["target_top_adapt_done"] == 1
    assert normalize_config(
        {"target_top_adapt_done": None})["target_top_adapt_done"] == 0
    assert normalize_config(
        {"target_top_adapt_done": 999})["target_top_adapt_done"] == TARGET_CLEAN_TOP_MAX
    assert normalize_config(
        {"target_top_adapt_done": -3})["target_top_adapt_done"] == 0
    assert normalize_config(
        {"target_top_adapt_done": "x"})["target_top_adapt_done"] == 0
    # 老配置里的 bool 闩天然兼容：真当过、假从没加过
    assert normalize_config(
        {"target_top_adapt_done": True})["target_top_adapt_done"] == 1
    assert normalize_config(
        {"target_top_adapt_done": False})["target_top_adapt_done"] == 0


def test_default_config_v21_keys_present():
    """v1.21.0 两键（预防窗口自适应开关与其爬山计数）必须有默认值。"""
    assert "predict_window_adapt" in DEFAULT_CONFIG
    assert "predict_window_adapt_done" in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["predict_window_adapt"] is True
    assert d["predict_window_adapt_done"] == 0, "爬山计数：0=从未加宽"


def test_normalize_config_predict_window_adapt_coercion():
    """预防窗口自适应开关 bool 强转；done 是已爬到的窗口分钟数，clamp 到 [0, 30]。"""
    assert normalize_config(
        {"predict_window_adapt": 0})["predict_window_adapt"] is False
    assert normalize_config(
        {"predict_window_adapt": "off"})["predict_window_adapt"] is True
    assert normalize_config(
        {"predict_window_adapt_done": 10})["predict_window_adapt_done"] == 10
    assert normalize_config(
        {"predict_window_adapt_done": None})["predict_window_adapt_done"] == 0
    assert normalize_config(
        {"predict_window_adapt_done": 999})["predict_window_adapt_done"] == 30
    assert normalize_config(
        {"predict_window_adapt_done": -5})["predict_window_adapt_done"] == 0
    assert normalize_config(
        {"predict_window_adapt_done": "x"})["predict_window_adapt_done"] == 0


def test_default_config_v16_keys_present():
    """v1.16.0 两键（清理提前量自适应及其一次性闩）必须有默认值。"""
    assert "headroom_adapt" in DEFAULT_CONFIG
    assert "headroom_adapt_done" in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["headroom_adapt"] is True
    assert d["headroom_adapt_done"] is False


def test_normalize_config_headroom_adapt_coercion():
    """提前量自适应开关与其一次性闩 bool 强转，语义与其它开关一致。"""
    assert normalize_config({"headroom_adapt": 0})["headroom_adapt"] is False
    assert normalize_config({"headroom_adapt": "off"})["headroom_adapt"] is True
    assert normalize_config({"headroom_adapt_done": 1})["headroom_adapt_done"] is True
    assert normalize_config(
        {"headroom_adapt_done": None})["headroom_adapt_done"] is False


def test_default_config_v17_keys_present():
    """v1.17.0 低内存下限自调优键进默认配置：开关默认开、一次性闩默认未落。"""
    assert "min_avail_adapt" in DEFAULT_CONFIG
    assert "min_avail_adapt_done" in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["min_avail_adapt"] is True
    assert d["min_avail_adapt_done"] is False


def test_normalize_config_min_avail_adapt_coercion():
    """低内存下限自适应开关与其一次性闩 bool 强转，语义与其它开关一致。"""
    assert normalize_config({"min_avail_adapt": 0})["min_avail_adapt"] is False
    assert normalize_config({"min_avail_adapt": "off"})["min_avail_adapt"] is True
    assert normalize_config(
        {"min_avail_adapt_done": 1})["min_avail_adapt_done"] is True
    assert normalize_config(
        {"min_avail_adapt_done": None})["min_avail_adapt_done"] is False


def test_normalize_config_stats_low_mem_roundtrip():
    """低内存触发计数只持久化非 0 值：0/缺失都不落键，统计行按有无决定显示。"""
    cfg = normalize_config({"stats": {"count": 3, "freed": 100, "low_mem": 2}})
    assert cfg["stats"] == {"count": 3, "freed": 100, "low_mem": 2}
    zero = normalize_config({"stats": {"count": 3, "freed": 100, "low_mem": 0}})
    assert zero["stats"] == {"count": 3, "freed": 100}
    plain = normalize_config({"stats": {"count": 3, "freed": 100}})
    assert plain["stats"] == {"count": 3, "freed": 100}


def test_default_config_v19_keys_present():
    """v1.19.0 大户下限自调优键进默认配置：开关默认开、一次性闩默认未落。"""
    assert "min_mb_adapt" in DEFAULT_CONFIG
    assert "min_mb_adapt_done" in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["min_mb_adapt"] is True
    assert d["min_mb_adapt_done"] is False


def test_normalize_config_min_mb_adapt_coercion():
    """大户下限自适应开关与其一次性闩 bool 强转，语义与其它开关一致。"""
    assert normalize_config({"min_mb_adapt": 0})["min_mb_adapt"] is False
    assert normalize_config({"min_mb_adapt": "off"})["min_mb_adapt"] is True
    assert normalize_config(
        {"min_mb_adapt_done": 1})["min_mb_adapt_done"] is True
    assert normalize_config(
        {"min_mb_adapt_done": None})["min_mb_adapt_done"] is False

def test_default_config_v20_keys_present():
    """v1.20.0 加深轮数自调优键进默认配置：开关默认开、已爬到轮数默认 0（从未）。"""
    assert "deepen_rounds_adapt" in DEFAULT_CONFIG
    assert "deepen_rounds_adapt_done" in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["deepen_rounds_adapt"] is True
    assert d["deepen_rounds_adapt_done"] == 0


def test_normalize_config_deepen_rounds_adapt_coercion():
    """加深轮数自适应开关 bool 强转、已爬轮数钳 0..3，语义与其它自调优键一致。"""
    assert normalize_config(
        {"deepen_rounds_adapt": 0})["deepen_rounds_adapt"] is False
    assert normalize_config(
        {"deepen_rounds_adapt": "off"})["deepen_rounds_adapt"] is True
    assert normalize_config(
        {"deepen_rounds_adapt_done": 9})["deepen_rounds_adapt_done"] == 3
    assert normalize_config(
        {"deepen_rounds_adapt_done": -1})["deepen_rounds_adapt_done"] == 0
    assert isinstance(normalize_config(
        {"deepen_rounds_adapt_done": "2"})["deepen_rounds_adapt_done"], int)


def test_default_config_v22_keys_present():
    """v1.22.0 两键（触发阈值自适应开关与其下调计数）必须有默认值。"""
    assert "threshold_adapt" in DEFAULT_CONFIG
    assert "threshold_adapt_done" in DEFAULT_CONFIG
    d = normalize_config({})
    assert d["threshold_adapt"] is True
    assert d["threshold_adapt_done"] == 0, "下调计数：0=从未降过"


def test_normalize_config_threshold_adapt_coercion():
    """触发阈值自适应开关 bool 强转；done 是已下调次数，clamp 到 [0, 2]。"""
    assert normalize_config(
        {"threshold_adapt": 0})["threshold_adapt"] is False
    assert normalize_config(
        {"threshold_adapt": "off"})["threshold_adapt"] is True
    assert normalize_config(
        {"threshold_adapt_done": 9})["threshold_adapt_done"] == 2
    assert normalize_config(
        {"threshold_adapt_done": -1})["threshold_adapt_done"] == 0
    assert isinstance(normalize_config(
        {"threshold_adapt_done": "2"})["threshold_adapt_done"], int)
    assert normalize_config(
        {"threshold_adapt_done": "x"})["threshold_adapt_done"] == 0
    assert normalize_config(
        {"threshold_adapt_done": None})["threshold_adapt_done"] == 0
