# -*- coding: utf-8 -*-
"""menu / tray：托盘菜单可构建；菜单必须始终读写「当前」配置（热重载后不被旧字典覆盖）。

背景：监控线程热重载时会把 guard.cfg 整体替换成新字典，而菜单闭包是在构建时
捕获 cfg 引用的。若继续用旧引用，勾选菜单时会把构造时那份配置整份写回
mem_guard.json，静默覆盖用户刚改的值。这里断言读写都落在 guard.cfg 上。
"""
import json
import threading

import pystray

from memguard import config, menu
from memguard.config import normalize_config
from memguard.menu import build_menu
from memguard.tray import Guard


class _FakeGuard:
    """只提供 build_menu 需要的属性：不起线程、不连 pystray 后端、不碰真实配置。"""

    def __init__(self, cfg=None):
        self.cfg = normalize_config(cfg or {})
        self.state = {
            "phys_pct": 50.0, "commit_pct": 50.0,
            "used_phys": 8 * 1024 ** 3, "total_phys": 16 * 1024 ** 3,
            "avail_commit": 16 * 1024 ** 3,
        }
        self.advice_count = 3
        self.advice_apply_count = 1
        self.history = []
        self.leaks = []
        self.icon = None
        self.stop = threading.Event()

    def refresh(self):
        pass


def _find_item(menu_tree, label):
    """按文案取一级菜单项（动态文案菜单项会先求值，这里只取静态文案项）。"""
    for item in menu_tree.items:
        if item.text == label:
            return item
    raise AssertionError(f"菜单里找不到「{label}」")


def _find_item_by_text(menu_tree, needle):
    """按文案片段取菜单项：动态文案项会先求值，故只能按包含关系找。"""
    for item in menu_tree.items:
        if needle in str(item.text):
            return item
    raise AssertionError(f"菜单里找不到含「{needle}」的菜单项")


def test_build_menu_returns_menu():
    assert isinstance(build_menu(Guard()), pystray.Menu)


def test_guard_exposes_advice_count():
    guard = Guard()
    assert isinstance(guard.advice_count, int)
    assert isinstance(guard._advice_at, float)


def test_menu_toggle_writes_current_config(monkeypatch):
    """勾选菜单只提交本次变更，合并结果立刻回填 guard.cfg。

    菜单不再把内存里的整份配置写回（那份可能是热重载前的旧快照），改为提交增量，
    由 config.update_config 以磁盘当前配置为基准合并——否则一次勾选就会把用户
    刚手改的字段覆盖回旧值。
    """
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))

    guard = _FakeGuard({"phys_threshold": 92, "commit_threshold": 95})
    menu_tree = build_menu(guard)
    # 模拟监控线程热重载：guard.cfg 被整体替换成新字典
    guard.cfg = normalize_config({"phys_threshold": 80, "commit_threshold": 88, "cooldown": 600})

    item = _find_item(menu_tree, "启动时清理")
    item(guard.icon)   # MenuItem.__call__ 即托盘点击时的回调

    assert merged == [{"clean_on_start": True}], "只提交本次勾选，不带旧快照里的其它字段"
    assert guard.cfg["clean_on_start"] is True, "合并结果要立刻回填，勾选态实时生效"


def test_menu_toggle_escalate_writes_current_config(monkeypatch):
    """「清理未达标自动升档」勾选只提交增量 escalate_clean，并即时回填（读写当前配置）。"""
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))

    guard = _FakeGuard({"escalate_clean": True})
    menu_tree = build_menu(guard)

    item = _find_item(menu_tree, "清理未达标自动升档")
    assert item.checked is True
    item(guard.icon)

    assert merged == [{"escalate_clean": False}], "只提交本次勾选，不带旧快照里的其它字段"
    assert guard.cfg["escalate_clean"] is False, "合并结果要立刻回填，勾选态实时生效"


def test_menu_toggle_level_adapt_writes_current_config(monkeypatch):
    """「保守档不给力自动改激进」勾选只提交增量 auto_level_adapt，并即时回填。"""
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))

    guard = _FakeGuard({"auto_level_adapt": True})
    menu_tree = build_menu(guard)

    item = _find_item(menu_tree, "保守档不给力自动改激进")
    assert item.checked is True
    item(guard.icon)

    assert merged == [{"auto_level_adapt": False}], "只提交本次勾选，不带旧快照里的其它字段"
    assert guard.cfg["auto_level_adapt"] is False, "合并结果要立刻回填，勾选态实时生效"


def test_menu_toggle_target_clean_writes_current_config(monkeypatch):
    """「定向清理内存大户」勾选只提交增量 target_clean，并即时回填（读写当前配置）。"""
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))

    guard = _FakeGuard({"target_clean": True})
    menu_tree = build_menu(guard)

    item = _find_item(menu_tree, "定向清理内存大户")
    assert item.checked is True
    item(guard.icon)

    assert merged == [{"target_clean": False}], "只提交本次勾选，不带旧快照里的其它字段"
    assert guard.cfg["target_clean"] is False, "合并结果要立刻回填，勾选态实时生效"


def test_menu_checked_reads_live_config(monkeypatch):
    """勾选态与子菜单单选态都读当前配置，而不是菜单构建那一刻的值。"""
    guard = _FakeGuard()
    menu_tree = build_menu(guard)

    toggle = _find_item(menu_tree, "自动清理")
    conservative = next(i for i in _find_item(menu_tree, "清理力度").submenu.items
                        if i.text.startswith("保守"))
    assert toggle.checked is True
    assert conservative.checked is True

    # 外部改了配置（热重载会走到这一步）：菜单勾选态应随之变化
    guard.cfg = normalize_config({"auto_clean": False, "clean_level": "aggressive"})
    assert toggle.checked is False
    assert conservative.checked is False


def test_menu_toggle_persists_without_clobbering(monkeypatch, tmp_path):
    """端到端：手改配置被热重载后，再点菜单勾选，手改的字段必须保住。"""
    cfg_file = tmp_path / "mem_guard.json"
    cfg_file.write_text(json.dumps({"phys_threshold": 88, "cooldown": 300}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_file))

    guard = _FakeGuard()
    # 用户手改配置 -> 监控线程热重载成新字典
    cfg_file.write_text(json.dumps({"phys_threshold": 92, "cooldown": 900}), encoding="utf-8")
    guard.cfg = config.load_config()

    menu_tree = build_menu(guard)
    _find_item(menu_tree, "启动时清理")(guard.icon)

    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["phys_threshold"] == 92, "手改的阈值不能被勾选写回覆盖"
    assert on_disk["cooldown"] == 900
    assert on_disk["clean_on_start"] is True, "勾选本身要生效"
    assert guard.cfg["clean_on_start"] is True


# ---------------------------------------------------------------- 更新菜单（v1.5.0）

def test_update_menu_items_present_with_live_checked_state():
    """更新三件套必须出现在菜单里，勾选态读当前配置。"""
    guard = _FakeGuard()
    menu_tree = build_menu(guard)
    assert _find_item(menu_tree, "立即更新到最新版")
    assert _find_item(menu_tree, "自动更新（后台检查）").checked is True
    assert _find_item(menu_tree, "下载后自动安装").checked is False

    # 配置被热重载后，勾选态要跟着变
    guard.cfg = normalize_config({"auto_update": False, "auto_install": True})
    assert _find_item(menu_tree, "自动更新（后台检查）").checked is False
    assert _find_item(menu_tree, "下载后自动安装").checked is True


def test_update_toggles_submit_increment_only(monkeypatch):
    """两个更新开关都只提交自己的增量，合并结果立刻回填 guard.cfg。"""
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))
    guard = _FakeGuard()
    menu_tree = build_menu(guard)

    _find_item(menu_tree, "自动更新（后台检查）")(guard.icon)
    assert merged == [{"auto_update": False}]
    assert guard.cfg["auto_update"] is False

    _find_item(menu_tree, "下载后自动安装")(guard.icon)
    assert merged[-1] == {"auto_install": True}
    assert guard.cfg["auto_install"] is True


class _RecordingIcon:
    def __init__(self):
        self.notes = []

    def notify(self, msg, title=None):
        self.notes.append(msg)


class _InlineThreading:
    """把后台线程改成当场跑完，方便同步断言安装结果。"""

    def Thread(self, target=None, **kw):
        target()
        return _NoopThread()


class _NoopThread:
    def start(self):
        pass


def test_update_now_only_notifies_on_failure(monkeypatch):
    """立即更新：成功走安装（进程随后重启）不再打扰，失败才气泡告知。"""
    monkeypatch.setattr(menu, "threading", _InlineThreading())
    guard = _FakeGuard()
    guard.icon = _RecordingIcon()
    menu_tree = build_menu(guard)
    item = _find_item(menu_tree, "立即更新到最新版")

    monkeypatch.setattr(menu, "install_latest",
                        lambda cfg: {"ok": True, "action": "installed"})
    item(guard.icon)
    assert guard.icon.notes == [], "安装成功不该再弹气泡"

    monkeypatch.setattr(menu, "install_latest",
                        lambda cfg: {"ok": False, "msg": "下载失败：网络不通"})
    item(guard.icon)
    assert guard.icon.notes == ["下载失败：网络不通"]


def test_update_now_reads_live_config(monkeypatch):
    """安装读的是当前配置（auto_install 等），不是菜单构建那一刻的快照。"""
    monkeypatch.setattr(menu, "threading", _InlineThreading())
    seen = []
    monkeypatch.setattr(menu, "install_latest",
                        lambda cfg: seen.append(cfg) or {"ok": True})
    guard = _FakeGuard({"auto_install": True})
    menu_tree = build_menu(guard)
    guard.cfg = normalize_config({"auto_install": False})   # 模拟热重载

    _find_item(menu_tree, "立即更新到最新版")(guard.icon)
    assert seen and seen[0]["auto_install"] is False

def test_stats_line_shows_escalation_count():
    """统计行：有升档次数时追加「（升档 N 次）」，一次都没升档时不出现该字样。"""
    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024, "escalated": 3}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "累计清理 5 次" in line
    assert "（升档 3 次）" in line

    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "升档" not in line


def test_stats_line_shows_targeted_count():
    """统计行：有定向清理次数时追加「（定向 N 次）」，没定向过时不出现该字样。"""
    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024, "targeted": 2}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "累计清理 5 次" in line
    assert "（定向 2 次）" in line

    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "定向" not in line


# ---------------------------------------------------------------- 立即清理回显（v1.5.1）

class _FakeIcon:
    """只提供 on_clean_now 需要的 notify()，不起真实气泡。"""

    def __init__(self):
        self.messages = []

    def notify(self, text, title=None):
        self.messages.append((title, text))


def test_clean_now_notify_marks_escalation(monkeypatch):
    """手动清理若补了激进一次，通知要标「（自动升档）」：与托盘自动清理、--once 同口径。"""
    def _fake_do_clean(reason="手动", growth_rows=None):
        return {
            "ok": True,
            "freed": 3 * 1024 ** 3,
            "level": "aggressive",
            "escalated": True,
            "detail": "-",
            "stats": {"count": 12},
            "before": {"avail_phys": 4 * 1024 ** 3, "commit_pct": 70.0},
            "after": {"avail_phys": 7 * 1024 ** 3, "commit_pct": 60.0},
        }

    monkeypatch.setattr(menu, "do_clean", _fake_do_clean)
    monkeypatch.setattr(menu, "top_processes_list", lambda n=15: [])

    icon = _FakeIcon()
    guard = _FakeGuard()
    _find_item(build_menu(guard), "立即清理")(icon)

    _, body = icon.messages[0]
    assert "自动升档" in body
    assert "激进" in body
    assert "定向清理大户" not in body, "没定向过的结果不能出现定向字样（老结果没这个键）"


def test_clean_now_notify_marks_targeted(monkeypatch):
    """手动清理精确清过大户：通知要单列「定向清理大户」段，与托盘自动清理同口径。"""
    def _fake_do_clean(reason="手动", growth_rows=None):
        return {
            "ok": True,
            "freed": 3 * 1024 ** 3,
            "level": "conservative",
            "escalated": False,
            "targeted": [("big.exe", 2 * 1024 ** 3, 4242)],
            "detail": "-",
            "stats": {"count": 12},
            "before": {"avail_phys": 4 * 1024 ** 3, "commit_pct": 70.0},
            "after": {"avail_phys": 7 * 1024 ** 3, "commit_pct": 60.0},
        }

    monkeypatch.setattr(menu, "do_clean", _fake_do_clean)
    monkeypatch.setattr(menu, "top_processes_list", lambda n=15: [])

    icon = _FakeIcon()
    guard = _FakeGuard()
    _find_item(build_menu(guard), "立即清理")(icon)

    _, body = icon.messages[0]
    assert "定向清理大户" in body
    assert "big.exe" in body
    assert "自动升档" not in body


# ------------------------------------------------ 一键应用优化建议（v1.6.0 增量）

def test_menu_one_click_apply_writes_increment_and_logs(monkeypatch):
    """托盘一键应用：逐条增量写配置、即时回填、只记日志不弹气泡。"""
    merged, logged = [], []
    monkeypatch.setattr(menu, "log", logged.append)
    monkeypatch.setattr(menu, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(menu, "analyze",
                        lambda cfg, mem=None, top=None, leaks=None: [
        {"title": "自动清理已关闭",
         "action": {"label": "开启自动清理", "changes": dict(auto_clean=True)}},
        {"title": "预警未开启",
         "action": {"label": "开启预警(15%)", "changes": dict(warn_margin=15)}},
        {"title": "未以管理员身份运行"},
    ])

    guard = _FakeGuard({"auto_clean": False, "warn_margin": 0})
    guard.advice_apply_count = 2

    def _fake_update(changes):
        merged.append(dict(changes))
        guard.cfg.update(changes)
        return dict(guard.cfg)

    monkeypatch.setattr(menu, "update_config", _fake_update)

    item = _find_item_by_text(build_menu(guard), "一键应用优化建议")
    assert item.enabled is True, "有可应用项时菜单项必须可点"
    item(guard.icon)

    assert merged == [dict(auto_clean=True), dict(warn_margin=15)], "只提交本次改动"
    assert guard.cfg["auto_clean"] is True and guard.cfg["warn_margin"] == 15, "要即时回填"
    assert guard.advice_apply_count == 0, "可应用计数清零，菜单项翻成「0 项」并置灰"
    assert guard.advice_count == 1, "总数只扣掉本次落地的那几条，精确值交给后台刷新"
    assert any("优化建议一键应用 | " in m and "开启自动清理" in m for m in logged)


def test_menu_apply_advice_disabled_without_actionable_items(monkeypatch):
    """建议全是提示级（没带 action）时一键应用项应置灰：点了没反应比没有更糟。"""
    monkeypatch.setattr(menu, "analyze", lambda cfg, mem=None, top=None: [
        {"title": "未以管理员身份运行"}, {"title": "内存充裕"}])

    guard = _FakeGuard()
    guard.advice_apply_count = 0
    item = _find_item_by_text(build_menu(guard), "一键应用优化建议")

    assert "0 项" in str(item.text)
    assert item.enabled is False


def test_menu_apply_advice_skips_failing_single_action(monkeypatch):
    """单条写配置失败只跳过该条：其余建议照样落地，绝不一损俱损。"""
    merged, logged = [], []
    monkeypatch.setattr(menu, "log", logged.append)
    monkeypatch.setattr(menu, "top_processes_list", lambda n=15: [])
    monkeypatch.setattr(menu, "analyze",
                        lambda cfg, mem=None, top=None, leaks=None: [
        {"title": "自动清理已关闭",
         "action": {"label": "开启自动清理", "changes": dict(auto_clean=True)}},
        {"title": "预警未开启",
         "action": {"label": "开启预警(15%)", "changes": dict(warn_margin=15)}},
    ])

    guard = _FakeGuard({"auto_clean": False, "warn_margin": 0})
    guard.advice_apply_count = 2

    def _fake_update(changes):
        if "warn_margin" in changes:
            raise OSError("disk gone")
        merged.append(dict(changes))
        guard.cfg.update(changes)
        return dict(guard.cfg)

    monkeypatch.setattr(menu, "update_config", _fake_update)

    item = _find_item_by_text(build_menu(guard), "一键应用优化建议")
    item(guard.icon)

    assert merged == [dict(auto_clean=True)], "失败那条被跳过，成功那条照写"
    assert guard.advice_apply_count == 1, "计数只减成功的条数"
    assert any("优化建议一键应用 | " in m for m in logged)


def test_menu_toggle_predict_clean_writes_current_config(monkeypatch):
    """「趋势预防式清理」勾选只提交增量 predict_clean，并即时回填（v1.8.0）。"""
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))

    guard = _FakeGuard({"predict_clean": True})
    menu_tree = build_menu(guard)

    item = _find_item(menu_tree, "趋势预防式清理")
    assert item.checked is True
    item(guard.icon)

    assert merged == [{"predict_clean": False}], "只提交本次勾选，不带旧快照里的其它字段"
    assert guard.cfg["predict_clean"] is False, "合并结果要立刻回填，勾选态实时生效"


def test_stats_line_shows_preventive_count():
    """统计行：有预防次数时追加「（预防 N 次）」，没预防过时不出现该字样。"""
    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024, "preventive": 2}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "累计清理 5 次" in line
    assert "（预防 2 次）" in line

    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "预防" not in line


def test_stats_line_shows_short_relief_count():
    """统计行：有效果偏短次数时追加「（短效 N 次）」，没短效过时不出现该字样。"""
    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024, "short_relief": 2}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "累计清理 5 次" in line
    assert "（短效 2 次）" in line

    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "短效" not in line

# ------------------------------------------------ 后台进程工作集清理菜单项（v1.11.0）


def test_menu_toggle_bg_trim_writes_current_config(monkeypatch):
    """「后台进程工作集清理」勾选只提交增量 bg_trim，并即时回填（读写当前配置）。"""
    merged = []
    monkeypatch.setattr(menu, "update_config",
                        lambda changes: merged.append(dict(changes)) or dict(changes))

    guard = _FakeGuard({"bg_trim": True})
    menu_tree = build_menu(guard)

    item = _find_item(menu_tree, "后台进程工作集清理")
    assert item.checked is True
    item(guard.icon)

    assert merged == [{"bg_trim": False}], "只提交本次勾选，不带旧快照里的其它字段"
    assert guard.cfg["bg_trim"] is False, "合并结果要立刻回填，勾选态实时生效"


def test_stats_line_shows_bg_trim_count():
    """统计行：有后台工作集清理次数时追加「（后台 N 次）」，没清理过时不出现该字样。"""
    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024, "bg_trimmed": 2}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "累计清理 5 次" in line
    assert "（后台 2 次）" in line

    guard = _FakeGuard({"stats": {"count": 5, "freed": 1024}})
    line = next(i.text for i in build_menu(guard).items
                if isinstance(i.text, str) and "累计清理" in i.text)
    assert "后台" not in line
