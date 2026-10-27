# -*- coding: utf-8 -*-
"""menutips：托盘菜单的悬停说明表——文案按菜单标签匹配，所以最容易坏的是「对不上」。

菜单文案改了名、自适应注册表加了新条目、或者把说明写在了一个带子菜单的父项上
（父项一展开，左右两侧都是子菜单的地盘，说明窗无处可放，等于白写）——这三种漂移
都不会让任何功能报错，只会让用户永远划不到本该有的解释。所以这里主要测对账：
标签 ↔ 文案表 ↔ 真实菜单树 三者必须始终咬合。
"""
import pystray

from memguard import adapters, menutips, menu
from memguard.config import DEFAULT_CONFIG, normalize_config
from memguard.menu import build_menu
from memguard.tray import Guard


def _menu_labels(guard=None):
    """走一遍真实菜单树，返回 (叶子标签, 带子菜单的父项标签)。

    分隔线（- - - -）两边都不算：它既不能挂说明，也不该混进叶子堆里让对账数字
    看不出真实覆盖率（v1.27.0 分组收纳后菜单里多了十条分隔线）。
    """
    tree = build_menu(guard or Guard())
    leaf, popup = [], []

    def walk(items):
        for item in items:
            if item is pystray.Menu.SEPARATOR:
                continue
            label = str(item.text)
            sub = getattr(item, "submenu", None)
            (popup if sub else leaf).append(label)
            if sub:
                walk(sub)

    walk(tree.items)
    return leaf, popup


def _find_label(tree, label):
    """按文案在整棵树里找项。

    v1.27.0 起菜单分了组（「鼠标悬停显示说明」收进了父项「热键与前台」），
    只查一级会 StopIteration——测试跟着菜单一起改成深度优先，才对得上真实托盘。
    """
    def walk(items):
        for it in items:
            if str(it.text) == label:
                return it
            sub = getattr(it, "submenu", None)
            if sub:
                found = walk(sub)
                if found:
                    return found
        return None

    item = walk(tree.items)
    if item is None:
        raise AssertionError(f"菜单里找不到「{label}」")
    return item


# ---------------------------------------------------------------- 不碰桌面

def test_import_does_not_create_window_or_thread():
    """导入模块不得建窗口、不得起线程：说明窗只在 attach() 里按需创建。"""
    tip = menutips._TIP
    assert tip.hwnd == 0
    assert tip._thread is None


def test_detach_without_attach_is_harmless():
    """没挂过就 detach（托盘启动失败 / 测试收尾）不应抛，也不应顺手起线程。"""
    menutips.MenuTips(lambda: {}).detach()
    assert menutips._TIP.hwnd == 0
    assert menutips._TIP._thread is None


# ---------------------------------------------------------------- 文案表对账

def test_every_adapter_row_carries_a_tip():
    """8 条自适应的说明取自注册表本身：注册表里少一条，菜单上就少一句解释。"""
    missing = [a.label for a in adapters.ADAPTERS if not (a.tip or "").strip()]
    assert not missing, f"ADAPTERS 里这些条目没写 tip: {missing}"


def test_adapter_tips_are_wired_into_the_table():
    """注册表改了文案，查表必须跟着变——两处各说一套是本表最坏的失效方式。"""
    for a in adapters.ADAPTERS:
        assert menutips.TIPS.get(a.label) == a.tip


def test_no_tip_key_is_unreachable_or_named_wrong():
    """每条文案都必须划得到：键要逐个命中菜单里的**叶子项**。

    带子菜单的父项不算命中——划过它只会展开子菜单，说明窗无处安放。
    菜单项改了名、或者说明写在了父项上，都会在这一行红掉。
    """
    leaf, popup = _menu_labels()
    dead = sorted(k for k in menutips.TIPS if k not in leaf)
    assert not dead, (f"这些说明在菜单里划不到（改名了？还是写在带子菜单的父项上了？）: "
                      f"{dead}；其中只出现在父项上的: "
                      f"{sorted(k for k in dead if k in popup)}")


def test_prefix_tips_match_a_real_dynamic_label():
    """带计数的动态项（「优化建议（3 条）」）靠前缀命中，前缀本身也必须有落点。"""
    leaf, _popup = _menu_labels()
    for prefix in menutips._PREFIX_TIPS:
        assert prefix.endswith("（"), f"前缀键应当以全角括号收尾: {prefix}"
        assert any(l.startswith(prefix) for l in leaf), f"没有叶子项以 {prefix} 开头"


def test_group_parents_and_guide_lines_stay_untipped():
    """v1.27.0 分组收纳的两条口径：父项不挂说明，组内引导行自己就是说明。

    父项一展开，左右两侧都是子菜单的地盘，给它配说明等于配了句显示不出来的话；
    引导行（每组第一条不可点的那行）本身就是「这组在讲什么」，再浮一层说明只会挡字。
    这一条红了通常意味着菜单又加了组、却没给组补引导行——那是把解释弄丢了，
    不是补个键就能了事。
    """
    _leaf, popup = _menu_labels()
    for parent in ("清理阈值", "清理力度", "清理冷却", "清理区域", "定时清理",
                   "低内存触发", "触发与阶梯", "自适应与进阶", "热键与前台",
                   "快捷键设置", "结束进程", "结束进程（内存占用 Top8）", "更新设置"):
        assert parent in popup, f"「{parent}」已不再是父项，这条对账要跟着菜单改"
        assert menutips.tip_for(parent) is None, f"说明写在了父项「{parent}」上，永远划不出来"

    leaf, _ = _menu_labels()
    guides = [l for l in leaf if "下面每条" in l or "动手线在" in l
              or "六层保护：" in l or "下面三项" in l]
    assert len(guides) == 4, f"分组引导行数量变了: {guides}"
    for g in guides:
        assert menutips.tip_for(g) is None, "引导行自己就是说明，不该再挂悬浮窗"


def test_tip_keys_exposes_both_tables():
    """--selftest 靠 tip_keys 对账：两张表（精确键 + 前缀键）都得从这儿出去。

    前缀键只藏在 _PREFIX_TIPS 里，出口漏了它们，自检就会以为「40 条键全可达」
    而实际放过一组划不到的文案。
    """
    keys = menutips.tip_keys()
    assert set(keys) == set(menutips.TIPS) | set(menutips._PREFIX_TIPS), \
        f"tip_keys 出口与两张表不一致，缺 {sorted(set(menutips.TIPS) | set(menutips._PREFIX_TIPS) - set(keys))}"
    assert len(keys) == menutips.tip_count() == len(set(keys)), \
        f"tip_keys({len(keys)}) / tip_count({menutips.tip_count()}) 口径不一致或有重复键"
    assert menutips.tip_count() >= 40, f"说明条数掉到 {menutips.tip_count()}"


def test_self_evident_items_stay_silent():
    """用户选的是「只覆盖看不懂的」：一眼看得明白的项不许配说明。"""
    for label in ["退出", "打开日志", "检查更新（v1.25.0）", "立即更新到最新版",
                  "1 分钟", "5 分钟", "关闭", "Ctrl+Alt+M", "- - - -",
                  "Qoder CN.exe (380MB, PID 10384)"]:
        assert menutips.tip_for(label) is None, f"「{label}」不该有悬停说明"


def test_tip_for_exact_prefix_and_miss():
    assert menutips.tip_for("立即清理") == menutips.TIPS["立即清理"]
    assert menutips.tip_for("优化建议（3 条）") == menutips._PREFIX_TIPS["优化建议（"]
    assert menutips.tip_for("复位自适应闩（已触发 2/8：触发线自适应）") == \
        menutips._PREFIX_TIPS["复位自适应闩（"]
    assert menutips.tip_for("没这项") is None
    assert menutips.tip_for("") is None
    assert menutips.tip_for(None) is None
    assert menutips.tip_for("  立即清理  ") == menutips.TIPS["立即清理"], "带空白也要命中"


def test_tip_text_shape():
    """说明是画出来的多行文本：首尾空白会顶出边框，超长会顶满屏幕，都得拦住。"""
    assert menutips.tip_count() >= 40, "覆盖数掉回两位数以下说明有项被写丢了"
    for label, text in list(menutips.TIPS.items()) + list(menutips._PREFIX_TIPS.items()):
        assert text == text.strip(), f"「{label}」的说明首尾有空白"
        assert "\r" not in text, f"「{label}」的说明带 \\r"
        assert 12 <= len(text) <= 300, f"「{label}」的说明长度出格: {len(text)}"
        assert not text.endswith(("，", "、", "；")), f"「{label}」的说明没写完"


def test_adapter_tip_keeps_the_three_line_shape():
    """自适应的说明统一是「判据 / 动作 / 关掉」三段——只看字面猜不出判据是它的痛点。"""
    for a in adapters.ADAPTERS:
        for part in ("判据", "动作", "关掉"):
            assert part in a.tip, f"「{a.label}」的说明缺「{part}」这一段"


# ---------------------------------------------------------------- 开关

def test_menu_tips_defaults_on_and_stays_bool():
    assert DEFAULT_CONFIG["menu_tips"] is True
    assert normalize_config({})["menu_tips"] is True
    assert normalize_config({"menu_tips": False})["menu_tips"] is False
    assert normalize_config({"menu_tips": "0"})["menu_tips"] is True, "脏值按 bool 归一"
    assert normalize_config({"menu_tips": None})["menu_tips"] is False


def test_menu_has_the_toggle_and_it_only_flips_config(monkeypatch):
    """菜单里的「鼠标悬停显示说明」只写 menu_tips 一个键，且即时回填 guard.cfg。"""
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))
    guard = Guard()
    tree = build_menu(guard)
    item = _find_label(tree, "鼠标悬停显示说明")
    assert item.checked is True, "默认开"

    item(guard.icon)
    assert merged == [{"menu_tips": False}]
    assert guard.cfg["menu_tips"] is False
    assert item.checked is False, "勾选态跟着当前配置走，不是构建时的快照"


def test_enabled_reads_config_live():
    """开关现读：关掉即「划过什么都不弹」，不需要重启托盘去解挂子类化。"""
    cfg = {"menu_tips": True}
    tips = menutips.MenuTips(lambda: cfg)
    assert tips.enabled() is True
    cfg["menu_tips"] = False
    assert tips.enabled() is False
    assert menutips.MenuTips(lambda: {}).enabled() is True, "老配置没这个键按开算"

    def boom():
        raise RuntimeError("配置读不出来")

    assert menutips.MenuTips(boom).enabled() is True, "取配置失败不该顺手关掉功能"


# ---------------------------------------------------------------- 摆放与量算

def test_window_size_leaves_room_for_the_frame():
    """客户区尺寸必须经 AdjustWindowRectEx 换算成窗口尺寸。

    说明窗带 WS_EX_DLGMODALFRAME：直接拿客户区尺寸去 SetWindowPos，窗口就窄掉边框
    那几像素，DrawText 折行比量出来的多一行，底部那行被切掉。
    """
    w, h = menutips._TipWindow._window_size(396, 54)
    assert w > 396 and h > 54, f"窗口没给边框留位置: {(w, h)}"


def test_place_never_covers_the_menu():
    """放不下就返回 None（不显示），而不是压到菜单上。"""
    tip = menutips._TipWindow()
    monkey = _FakeWorkArea(tip)
    # 菜单贴着屏幕右缘：右侧放不下，应翻到左侧
    assert monkey.place((1800, 300, 1912, 322), 400, 100) == (1394, 300)
    # 菜单几乎占满工作区：两侧都放不下 -> 夹回工作区会压住菜单，干脆不显示
    assert monkey.place((0, 300, 1900, 322), 400, 100) is None


def test_place_keeps_the_tip_inside_the_work_area():
    tip = menutips._TipWindow()
    monkey = _FakeWorkArea(tip)
    x, y = monkey.place((100, 900, 400, 922), 420, 300)
    assert y + 300 <= 1040, "底部越界没被夹住"
    assert x >= 0 and y >= 0


class _FakeWorkArea:
    """把 _work_area 换成固定屏幕（0,0,1920,1040），摆放逻辑就能纯逻辑测。"""

    def __init__(self, tip):
        self._tip = tip
        self._orig = menutips._work_area

    def place(self, rect, w, h):
        menutips._work_area = lambda _point: {"left": 0, "top": 0,
                                              "right": 1920, "bottom": 1040}
        try:
            return self._tip._place(rect, w, h)
        finally:
            menutips._work_area = self._orig


# ---------------------------------------------------------------- 事件判定表

class _Recorder:
    """假说明窗：只记 show/hide，不碰任何窗口。"""

    def __init__(self):
        self.shown = []
        self.hidden = 0

    def show(self, text, rect):
        self.shown.append((text, rect))

    def hide(self):
        self.hidden += 1

    def stop(self):
        pass


def _select(tips, wparam, recorder):
    """喂一条 WM_MENUSELECT 给判定表，返回它做了什么。"""
    tips._on_select(0x1234, wparam, 0x999)
    return recorder.shown[-1] if recorder.shown else None


def test_on_select_decision_table(monkeypatch):
    """叶子项出说明，其余一律收起：菜单收尾、带子菜单的父项、没配的项、取不到矩形的项。"""
    rec = _Recorder()
    monkeypatch.setattr(menutips, "_TIP", rec)
    monkeypatch.setattr(menutips, "_menu_text", lambda hmenu, cmd: "立即清理")
    monkeypatch.setattr(menutips, "_item_rect",
                        lambda hwnd, hmenu, cmd: (100, 200, 400, 222))
    tips = menutips.MenuTips(lambda: {"menu_tips": True})

    got = _select(tips, (0x8000 << 16) | 1, rec)   # 低 16 位 = 命令 id，高 16 位 = MF_* 标志
    assert got and got[0] == menutips.TIPS["立即清理"]
    assert got[1] == (100, 200, 400, 222)

    rec.shown.clear()
    _select(tips, (0xFFFF << 16) | 0xFFFF, rec)      # 菜单收尾（划过空白 / 退出跟踪）
    assert rec.shown == [] and rec.hidden == 1
    _select(tips, (0x8090 << 16) | 3, rec)           # MF_POPUP：带子菜单的父项
    assert rec.shown == [] and rec.hidden == 2
    _select(tips, (0x8000 << 16) | 0xFFFF, rec)      # 收尾标志也可能只在低 16 位
    assert rec.shown == [] and rec.hidden == 3


def test_on_select_hides_when_label_or_rect_is_missing(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(menutips, "_TIP", rec)
    monkeypatch.setattr(menutips, "_menu_text", lambda hmenu, cmd: "退出")
    monkeypatch.setattr(menutips, "_item_rect",
                        lambda hwnd, hmenu, cmd: (100, 200, 400, 222))
    tips = menutips.MenuTips(lambda: {"menu_tips": True})
    _select(tips, 0x8002, rec)
    assert rec.shown == [] and rec.hidden == 1, "没配说明的项要收起，不能留着上一句"

    monkeypatch.setattr(menutips, "_menu_text", lambda hmenu, cmd: "立即清理")
    monkeypatch.setattr(menutips, "_item_rect", lambda hwnd, hmenu, cmd: None)
    _select(tips, 0x8003, rec)
    assert rec.shown == [] and rec.hidden == 2, "取不到矩形就不摆窗"


def test_disabled_switch_skips_the_whole_lookup(monkeypatch):
    """关掉开关：连文案都不查，划过菜单与没实现该特性时表现一致。"""
    calls = []
    rec = _Recorder()
    monkeypatch.setattr(menutips, "_TIP", rec)
    monkeypatch.setattr(menutips, "_menu_text",
                        lambda hmenu, cmd: calls.append(cmd) or "立即清理")
    tips = menutips.MenuTips(lambda: {"menu_tips": False})
    _select(tips, 0x8001, rec)
    assert calls == [], "关掉后不该再查菜单文案"
    assert rec.shown == [] and rec.hidden == 1


def test_wndproc_swallows_errors_and_still_chains(monkeypatch):
    """子类化窗口过程里异常绝不能越过 ctypes 边界：那会崩在菜单的模态循环里。

    同时第一次异常要落日志——全吞掉会把「划过没反应」变成查不出来的哑病。
    """
    logs = []
    monkeypatch.setattr(menutips, "log", lambda *a: logs.append(a))
    tips = menutips.MenuTips(lambda: {"menu_tips": True})

    def boom(*_args):
        raise RuntimeError("量字炸了")

    monkeypatch.setattr(tips, "_on_select", boom)
    monkeypatch.setattr(menutips.user32, "CallWindowProcW",
                        lambda prev, hwnd, msg, wparam, lparam: 42)
    assert tips._wndproc(0x1234, menutips.WM_MENUSELECT, 0x8001, 0x999) == 42
    assert logs and "量字炸了" in logs[0][0], "第一次异常要能被日志看见"
    assert tips._wndproc(0x1234, menutips.WM_MENUSELECT, 0x8001, 0x999) == 42
    assert len(logs) == 1, "同类异常只记一次，别把日志刷爆"


def test_build_menu_item_is_a_plain_toggle():
    """勾选项必须是叶子：给它配说明等于配了句永远显示不出来的话。"""
    tree = build_menu(Guard())
    item = _find_label(tree, "鼠标悬停显示说明")
    assert getattr(item, "submenu", None) is None
    assert isinstance(item, pystray.MenuItem)
