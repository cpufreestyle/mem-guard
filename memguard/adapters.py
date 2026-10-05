# -*- coding: utf-8 -*-
"""
自适应适配器注册表：所有「自动调优旋钮」的单一事实来源。

v1.6.0–v1.22.0 一条接一条加自适应，每加一条就要在四处各写一遍同一个名字：
tray 里判据与落闩、advisor 里重复一遍判据、菜单里开关、normalize_config 里钳制。
名字一多，四处漂移只是时间问题——v1.21.0 就把 target_top_adapt_done 从 bool
改成了 int（爬山计数），当时就得回头同步四处。

本模块把每条自适应收敛成一行声明：

    Adapter(key=开关键, latch=闩键, kind=计数/布尔, cap=级数上限, label=菜单文案)

菜单开关、诊断摘要、一键复位闩、normalize_config 的钳制区间都从这张表推导。
新增自适应只需要在这里加一行，再写它自己的判据逻辑（判据是每条自适应唯一
真正不同的部分，收不进表里）。

依赖方向：config ← adapters ← menu / diag，所以本模块不反向 import config。
"""

from __future__ import annotations

# 闩的两种字型：
#   COUNTER —— int，记录「已经爬到第几级」，0=从未，到 cap 即停（爬山型）
#   FLAG    —— bool，整个生命周期最多动一次（一次性型）
COUNTER = "counter"
FLAG = "flag"


class Adapter:
    """一条自适应的声明：开关键 + 闩键 + 字型 + 级数上限 + 菜单文案。"""

    __slots__ = ("key", "latch", "kind", "cap", "label")

    def __init__(self, key: str, latch: str, kind: str, cap: int, label: str):
        self.key = key
        self.latch = latch
        self.kind = kind
        self.cap = cap
        self.label = label

    @property
    def short(self) -> str:
        """短名：去掉菜单文案里的括注，供诊断摘要与提示串用。"""
        return self.label.split("(")[0].split("（")[0]

    def latch_default(self):
        """闩的初始值：计数型回 0，一次性型回 False。"""
        return 0 if self.kind == COUNTER else False

    def fired(self, cfg) -> bool:
        """这条自适应是否已经动过（复位入口据此决定要不要提示）。"""
        v = (cfg or {}).get(self.latch)
        if self.kind == COUNTER:
            try:
                return int(v or 0) > 0
            except (TypeError, ValueError):
                return False
        return bool(v)

    def enabled(self, cfg) -> bool:
        """开关是否打开（默认开：老配置没有这个键时按开着算）。"""
        return bool((cfg or {}).get(self.key, True))

    def progress(self, cfg) -> int:
        """闩的当前进度：计数型返回已爬到的级数（钳 0..cap），一次性型返回 0/1。

        tray / advisor 里原来到处手写 int(cfg.get(latch) or 0)，写得多了就会出现
        「这里钳了、那里没钳」的口径差；收成一个方法，脏值也统一按 0 算。
        """
        v = (cfg or {}).get(self.latch)
        if self.kind == COUNTER:
            try:
                return max(0, min(self.cap, int(v or 0)))
            except (TypeError, ValueError):
                return 0
        return 1 if v else 0

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Adapter({self.key} -> {self.latch}, {self.kind}, cap={self.cap})"


# 级数上限与 clean.py 的硬上限一一对应（config 不依赖 clean，故在这里写死并注明）：
#   TARGET_CLEAN_TOP_MAX=10    target_clean_top 的钳制上限
#   _DEEPEN_MAX_ROUNDS=3       stage_deepen_rounds 的钳制上限
#   _PREDICT_WINDOW_MAX=30     predict_window_min 可被自适应加到的上限
#   _THRESHOLD_MAX_DROPS=2     触发线最多下调次数

# 闩键命名的一处历史例外：v1.6.0 定下的 level_adapt_done 比开关键少了 auto_ 前缀。
# 改键名会让老配置里的闩失效（自适应被重新评估一次），不值得，因此显式记下来，
# 免得后来者以为这里写错了。
_LATCH_NAME_EXCEPTIONS = {"auto_level_adapt": "level_adapt_done"}

ADAPTERS = (
    Adapter("auto_level_adapt", "level_adapt_done", FLAG, 1,
            "保守档不给力自动改激进"),
    Adapter("target_top_adapt", "target_top_adapt_done", COUNTER, 10,
            "定向覆盖面自适应(大户数不足时+1)"),
    Adapter("headroom_adapt", "headroom_adapt_done", FLAG, 1,
            "清理提前量自适应(效果偏短时阈值提前)"),
    Adapter("min_avail_adapt", "min_avail_adapt_done", FLAG, 1,
            "低内存下限自适应(可用内存偏低时抬高)"),
    Adapter("min_mb_adapt", "min_mb_adapt_done", FLAG, 1,
            "定向大户下限自适应(榜上够不着下限时减半)"),
    Adapter("deepen_rounds_adapt", "deepen_rounds_adapt_done", COUNTER, 3,
            "加深轮数自适应(加深卡住时+1轮)"),
    Adapter("predict_window_adapt", "predict_window_adapt_done", COUNTER, 30,
            "预防窗口自适应(没防住时加宽窗口)"),
    Adapter("threshold_adapt", "threshold_adapt_done", COUNTER, 2,
            "触发线自适应(短效反复且温和手段用尽时提前)"),
)

# 开关键 / 闩键清单，供测试与需要按名查找的调用方使用
ADAPTER_KEYS = tuple(a.key for a in ADAPTERS)
ADAPTER_LATCHES = tuple(a.latch for a in ADAPTERS)

_BY_KEY = {a.key: a for a in ADAPTERS}


def require(key: str) -> Adapter:
    """按开关键取 Adapter；没登记过就直接抛错，不返回 None。

    tray / advisor / menu 拿到适配器后立刻就会取 .enabled(cfg) / .fired(cfg)。
    这里若返回 None，现场只会留下一句「'NoneType' object has no attribute
    'enabled'」——在监控循环的 try/except 里被当成一行日志吞掉，很难追到
    「忘了在 ADAPTERS 里登记」这个真正原因。所以让它在出事的当场就喊出来。
    """
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(
            "自适应 %r 没有登记在 adapters.ADAPTERS 里；先在注册表加一行，"
            "再写它的判据与落闩" % key) from None


def clamp_bounds(adapter: Adapter) -> tuple:
    """normalize_config 用的闩钳制区间 (low, high, default)。"""
    if adapter.kind == COUNTER:
        return 0, adapter.cap, adapter.latch_default()
    return 0, 1, adapter.latch_default()


def reset_patch() -> dict:
    """一键复位所有自适应闩的增量：回到「从未自动调优过」的初始口径。

    只清闩、不动别的配置：用户手动调过的档位 / 阈值 / 大户数全部保住，
    抹掉的只是「自适应已经动过一次」这段记忆。
    """
    return {a.latch: a.latch_default() for a in ADAPTERS}


def reset_status(cfg) -> str:
    """复位入口的当前状态一行话，菜单项据此提示「有几条已触发」。"""
    fired = [a for a in ADAPTERS if a.fired(cfg)]
    if not fired:
        return "自适应闩已全部复位"
    return "已触发 " + str(len(fired)) + "/" + str(len(ADAPTERS)) + "：" + "、".join(
        a.short for a in fired)


def summary_lines(cfg) -> list:
    """自适应痕迹：每条一行，供诊断 zip 的正文摘要使用。

    现场报告里能直接看出「自动调优到底动过哪几条、爬到第几级、开关是开是关」，
    不用去翻 mem_guard.json 里的散键，也不用 grep 日志。
    """
    cfg = cfg or {}
    out = []
    for a in ADAPTERS:
        if a.kind == COUNTER:
            try:
                latch = int(cfg.get(a.latch) or 0)
            except (TypeError, ValueError):
                latch = 0
            note = "已加 " + str(latch) + "/" + str(a.cap) + " 级（0=从未）"
        else:
            note = "已触发（一次性）" if a.fired(cfg) else "未触发"
        out.append("adapt " + a.short + " | 开关 " +
                   ("on" if a.enabled(cfg) else "off") + " | " + note)
    return out
