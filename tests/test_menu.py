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
        self.history = []
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
