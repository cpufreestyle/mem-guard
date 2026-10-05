# -*- coding: utf-8 -*-
"""adapters：自适应注册表与 DEFAULT_CONFIG / normalize_config 必须始终对得上。

注册表的存在意义就是「单一事实来源」：菜单开关、诊断摘要、一键复位、归一化区间
全都从它推导。所以这里最重要的不是测某一条自适应的业务逻辑，而是测
「注册表 ↔ 默认配置 ↔ 归一化口径」三者没有漂移——加了一条自适应却忘了注册，
或者注册了默认配置里却没有键，都让上面的推导静默失效。
"""
import io
import os
import re

import pytest

from memguard import adapters
from memguard.config import DEFAULT_CONFIG, normalize_config


def test_registry_covers_every_adapt_key_in_defaults():
    """默认配置里每个自适应开关/闩都必须在注册表里，反之亦然。"""
    adapt_keys = set(adapters.ADAPTER_KEYS) | set(adapters.ADAPTER_LATCHES)
    default_adapt = {k for k in DEFAULT_CONFIG
                     if k.endswith("_adapt") or k.endswith("_adapt_done")}
    assert default_adapt == adapt_keys, (
        "注册表与 DEFAULT_CONFIG 不同步：只在一边出现的键 = %s"
        % sorted(default_adapt ^ adapt_keys))


def test_registry_gate_and_latch_are_paired():
    """每条自适应的开关键与闩键成对出现，且闩键名 = 开关键 + _done。"""
    for a in adapters.ADAPTERS:
        expect = adapters._LATCH_NAME_EXCEPTIONS.get(a.key, a.key + "_done")
        assert a.latch == expect, a
        assert a.kind in (adapters.COUNTER, adapters.FLAG), a
        assert a.label and a.short, a


def test_counters_are_int_latches_and_flags_are_bool():
    """计数型闩的复位值是 0，一次性闩的复位值是 False。"""
    for a in adapters.ADAPTERS:
        if a.kind == adapters.COUNTER:
            assert a.latch_default() == 0
            assert a.cap > 1, "计数型自适应至少要能爬一级以上"
        else:
            assert a.latch_default() is False
            assert a.cap == 1


def test_clamp_bounds_match_normalize_config():
    """注册表给的钳制区间必须与 normalize_config 的实际效果一致。

    这条是防漂移的关键：注册表里写了 cap=3，而 normalize 实际钳到 2，菜单上的
    「已加 N/3 级」就会显示出一个程序永远不会达到的数字。
    """
    for a in adapters.ADAPTERS:
        low, high, default = adapters.clamp_bounds(a)
        if a.kind == adapters.COUNTER:
            over = normalize_config({a.latch: high + 5})[a.latch]
            assert over == high, (a, over)
            under = normalize_config({a.latch: -3})[a.latch]
            assert under == low, (a, under)
            dirty = normalize_config({a.latch: "x"})[a.latch]
            assert dirty == default, (a, dirty)
        else:
            coerced = normalize_config({a.latch: 0})[a.latch]
            assert coerced is False, a


def test_legacy_bool_counter_is_coerced_to_int():
    """老配置里 target_top_adapt_done 存的是 bool，归一化后按 0/1 用。"""
    cfg = normalize_config({"target_top_adapt_done": True})
    assert cfg["target_top_adapt_done"] == 1
    assert normalize_config({"target_top_adapt_done": False})["target_top_adapt_done"] == 0


def test_reset_patch_only_touches_latches():
    """复位只抹闩，不动用户手动调过的任何旋钮。"""
    patch = adapters.reset_patch()
    assert set(patch) == set(adapters.ADAPTER_LATCHES)
    assert patch["threshold_adapt_done"] == 0
    assert patch["level_adapt_done"] is False
    merged = normalize_config({"phys_threshold": 92, "cooldown": 900, **patch})
    assert merged["phys_threshold"] == 92, "复位不该动阈值"
    assert merged["cooldown"] == 900, "复位不该动冷却"


def test_reset_status_reports_fired_adapters():
    empty = adapters.reset_status(normalize_config({}))
    assert empty == "自适应闩已全部复位"
    assert "未触发" not in empty
    hit = adapters.reset_status({"level_adapt_done": True, "threshold_adapt_done": 2})
    assert "已触发 2/8" in hit
    assert "保守档不给力自动改激进" in hit and "触发线自适应" in hit


def test_fired_treats_dirty_values_as_not_fired():
    dirty = {"threshold_adapt_done": "abc", "level_adapt_done": 0}
    assert adapters.require("threshold_adapt").fired(dirty) is False
    assert adapters.require("auto_level_adapt").fired(dirty) is False


def test_summary_lines_one_per_adapter_with_progress():
    lines = adapters.summary_lines({"deepen_rounds_adapt_done": 1, "min_mb_adapt": False})
    assert len(lines) == len(adapters.ADAPTERS)
    joined = "\n".join(lines)
    assert "加深轮数自适应" in joined and "已加 1/3 级" in joined
    assert "定向大户下限自适应" in joined and "开关 off" in joined
    assert "开关 on" in joined, "开关缺省按开算"


def test_require_rejects_unknown_key_loudly():
    """没登记的键必须当场响，不能返回 None。

    tray / advisor 拿到适配器后立刻取 .enabled(cfg)；这里若给 None，现场只会留下
    一句「'NoneType' object has no attribute 'enabled'」，在监控循环的 try/except 里
    被当成一行日志吞掉，很难追到「忘了在 ADAPTERS 里登记」这个真正原因。
    """
    with pytest.raises(KeyError) as e:
        adapters.require("no_such_adapt")
    msg = str(e.value)
    assert "no_such_adapt" in msg and "ADAPTERS" in msg, msg


def test_require_returns_every_registered_adapter():
    for a in adapters.ADAPTERS:
        assert adapters.require(a.key) is a



# ------------------------------------- tray / advisor 与注册表的静态一致性

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADAPT_LITERAL_RE = re.compile(r"[\"']([a-z0-9_]*_adapt(?:_done)?)[\"']")


def _adapter_literals(rel):
    """从源码里抓出所有形如 xxx_adapt / xxx_adapt_done 的字符串字面量。"""
    with io.open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return set(_ADAPT_LITERAL_RE.findall(f.read()))


def test_adapter_names_used_in_code_are_all_registered():
    """tray / advisor 里出现的每个自适应键名都必须在注册表里。

    这是「同一名字写在四处」那个老问题的静态闸门：新加一条自适应时，tray 或
    advisor 里手写的键名与注册表对不上（拼错、改名、忘了登记），这里立刻红，
    不必等到运行时靠用户报告。写盘处的键名同样受这条约束——tray 若还在写一个
    注册表里没有的闩键，那条自适应永远不会被记为「已触发」。
    """
    registered = set(adapters.ADAPTER_KEYS) | set(adapters.ADAPTER_LATCHES)
    for rel in ("memguard/tray.py", "memguard/advisor.py"):
        used = _adapter_literals(rel)
        assert used <= registered, (rel, sorted(used - registered))


def test_every_registered_adapter_is_wired_into_tray_or_advisor():
    """反向：注册表里每条自适应都必须在 tray / advisor 里真的被用到。

    只往注册表加一行、 DEFAULT_CONFIG 加两键，却没写判据与落闩，菜单上会多出
    一个永远显示「未触发」的开关——那是骗人的。这条把 landing 不完整的自适应挡住。
    """
    used = (_adapter_literals("memguard/tray.py")
            | _adapter_literals("memguard/advisor.py"))
    missing = [a.key for a in adapters.ADAPTERS if a.key not in used]
    assert not missing, "注册了却没接线的自适应: %s" % missing


def test_registry_caps_match_clean_hard_limits():
    """注册表的级数上限必须与 clean.py 的硬上限一致。

    菜单上的「已加 N/cap 级」读注册表的 cap，而 tray 真正允许爬到哪一级读的是
    clean.py 的常量。两者不一致时，用户会看到一个程序永远达不到的数字；v1.21.0
    把 target_top_adapt_done 从 bool 改成 int 时，这类不同步正是当时的代价。
    """
    from memguard import clean
    from memguard.config import TARGET_CLEAN_TOP_MAX

    expect = {
        "target_top_adapt": TARGET_CLEAN_TOP_MAX,
        "deepen_rounds_adapt": clean._DEEPEN_MAX_ROUNDS,
        "predict_window_adapt": clean._PREDICT_WINDOW_MAX,
        "threshold_adapt": clean._THRESHOLD_MAX_DROPS,
    }
    for key, cap in expect.items():
        a = adapters.require(key)
        assert a is not None, key
        assert a.kind == adapters.COUNTER, key
        assert a.cap == cap, (key, a.cap, cap)


def test_progress_clamps_and_tolerates_dirty_values():
    a = adapters.require("threshold_adapt")
    assert a.progress({}) == 0
    assert a.progress({a.latch: 1}) == 1
    assert a.progress({a.latch: 2}) == 2
    assert a.progress({a.latch: 99}) == a.cap, "手改出来的越界值按上限算"
    assert a.progress({a.latch: -5}) == 0
    assert a.progress({a.latch: "abc"}) == 0
    assert a.progress({a.latch: True}) == 1, "老配置存 bool 时按 1 算"
    flag = adapters.require("headroom_adapt")
    assert flag.progress({}) == 0
    assert flag.progress({flag.latch: True}) == 1





def test_production_code_only_uses_require():
    """tray / advisor / menu 只准用 require()，不许回到 adapters.get(。

    get() 对没登记的键返回 None，调用方接着取 .enabled() 只会得到一句
    「'NoneType' object has no attribute 'enabled'」，在监控循环的 try/except 里
    被当成一行日志吞掉。这条把「两个入口、一个是错的」这个形态钉死。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("memguard/tray.py", "memguard/advisor.py", "memguard/menu.py"):
        with io.open(os.path.join(root, rel), encoding="utf-8") as f:
            src = f.read()
        assert "adapters.get(" not in src, rel + " 又用回会返回 None 的 get() 了"