# -*- coding: utf-8 -*-
"""
全局热键：一键立即清理 / 打开内存概览 / 切换自动清理。

对标 Mem Reduct / ISLC：托盘工具被抱怨最多的是「想清一下还得点开右键菜单」。
全局热键把这动作压成一次按键。实现走 RegisterHotKey(hWnd=None)：WM_HOTKEY 直接
落在调用线程的消息队列，不需要建窗口，也就没有「窗口类回调被 GC」那类坑。

热键是便利功能，不是关键路径：某条注册失败（多半是被别的程序占用）只跳过该条
并记一行日志；一条都没注册上也不影响托盘与监控——最坏情况就是没热键。
"""
from __future__ import annotations

import ctypes
import threading
import time

from . import winapi
from .config import HOTKEY_DEFAULTS, log

WM_HOTKEY = winapi.WM_HOTKEY
WM_QUIT = winapi.WM_QUIT

# 轮询间隔(秒)：PeekMessage 非阻塞取样 + 事件等待，stop() 最多迟这么久生效
POLL_SEC = 0.25

# 注册失败的热键隔这么久重试一次（v1.24.0）：占用它的程序（QQ / 输入法 / 远程工具）
# 退出后就该自动生效，不该要用户重启 MemGuard 才能用上。
RETRY_SEC = 300.0

# 动作名顺序即注册顺序，菜单 / 日志 / 测试共用
ACTIONS = tuple(HOTKEY_DEFAULTS)

# 菜单与日志里的中文名
ACTION_LABELS = {"clean": "立即清理", "overview": "内存概览", "auto": "自动清理"}

# 菜单「快捷键设置」里可选的键位（v1.24.0）：默认值打头，另给几个不容易和常见
# 软件撞车的组合。每一项都必须过 parse_hotkey——菜单给个选了也不生效的组合
# 比不给更糟；跨动作也尽量错开，真撞了由 _set_hotkey 把另一条清掉（见 menu.py）。
HOTKEY_CHOICES = {
    "clean": ("ctrl+alt+m", "ctrl+alt+c", "ctrl+shift+m", "ctrl+alt+d"),
    "overview": ("ctrl+alt+o", "ctrl+alt+v", "ctrl+shift+o", "ctrl+alt+w"),
    "auto": ("ctrl+alt+p", "ctrl+alt+a", "ctrl+shift+p", "ctrl+alt+s"),
}


def choice_specs() -> dict:
    """菜单可选键位表（action -> 可选键位元组），自检与测试共用。"""
    return HOTKEY_CHOICES


def choices_are_usable() -> list:
    """返回不可用的 (action, spec)：解析不了的组合绝不该出现在菜单里。"""
    return [(a, s) for a, specs in HOTKEY_CHOICES.items() for s in specs
            if parse_hotkey(s) is None]


def choice_clashes() -> list:
    """返回跨动作重复的 (action, spec)：同一组合只能登记一条，重复的那条注册必失败。"""
    seen, bad = {}, []
    for action, specs in HOTKEY_CHOICES.items():
        for spec in specs:
            if spec in seen and seen[spec] != action:
                bad.append((action, spec))
            seen[spec] = action
    return bad

_MOD_TOKENS = {
    "ctrl": winapi.MOD_CONTROL, "control": winapi.MOD_CONTROL,
    "alt": winapi.MOD_ALT, "shift": winapi.MOD_SHIFT,
    "win": winapi.MOD_WIN, "windows": winapi.MOD_WIN,
}
_MOD_LABELS = {
    "ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt",
    "shift": "Shift", "win": "Win", "windows": "Win",
}

# "没有消息" 之外的循环终止信号：poll 返回它即退出循环
_QUIT = -1


def _vk_of(key: str) -> int | None:
    """单个主键 → 虚拟键码；不认识返回 None。"""
    if len(key) == 1:
        return ord(key.upper())
    if key[0] == "f" and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 24:
            return 0x6F + n
    return None


def parse_hotkey(spec) -> tuple | None:
    """解析 "ctrl+alt+m" → (修饰位(含 MOD_NOREPEAT), 虚拟键码)；不合法返回 None。

    至少一个修饰键是硬要求：RegisterHotKey 不收无修饰键的普通键，而抢 F1 /
    Ctrl+C 这类系统快捷键只会招来投诉。空串、None、两个主键、未知 token
    一律判非法——宁可少一条热键也不乱注册。
    """
    if not isinstance(spec, str):
        return None
    parts = [p for p in (t.strip().lower() for t in spec.split("+")) if p]
    if not parts:
        return None
    mods = 0
    main = None
    for p in parts:
        if p in _MOD_TOKENS:
            mods |= _MOD_TOKENS[p]
            continue
        if main is not None:
            return None
        main = p
    if main is None or not mods:
        return None
    vk = _vk_of(main)
    if vk is None:
        return None
    return (mods | winapi.MOD_NOREPEAT, vk)


def normalize_spec(spec) -> str:
    """配置里的键位串统一成小写规范形；None / 非字符串 → 空串。"""
    if not isinstance(spec, str):
        return ""
    return "+".join(p for p in (t.strip().lower() for t in spec.split("+")) if p)


def format_hotkey(spec) -> str:
    """"ctrl+alt+m" → "Ctrl+Alt+M"（菜单与日志用）；空串返回空串。"""
    parts = [p for p in (t.strip() for t in str(spec or "").split("+")) if p]
    out = []
    for p in parts:
        low = p.lower()
        if low in _MOD_LABELS:
            out.append(_MOD_LABELS[low])
        elif len(p) == 1:
            out.append(p.upper())
        else:
            out.append(p[0].upper() + p[1:].lower())
    return "+".join(out)


def action_specs(cfg: dict) -> dict:
    """从配置取各动作最终键位：总开关关 / 显式空串 / 不合法的一律不注册。

    键缺失（老配置）时回落默认键位；显式给空串表示用户就是要关掉这一条。
    """
    if not cfg.get("hotkey_enabled", True):
        return {}
    out, taken = {}, {}
    for name in ACTIONS:
        raw = cfg.get("hotkey_" + name)
        spec = normalize_spec(HOTKEY_DEFAULTS[name] if raw is None else raw)
        if not spec:
            continue
        if parse_hotkey(spec) is None:
            log(f"全局热键：{ACTION_LABELS.get(name, name)} 的键位「{spec}」不可识别，已跳过")
            continue
        if spec in taken:
            # 同一个组合只能登记一条：后登记的那个必然注册失败，还会落进重试、
            # 每 5 分钟刷一行日志。这通常是用户手改配置把两个动作设成了同一键，
            # 所以这里直接跳过并说清是谁占了它——按 ACTIONS 顺序，先到先得。
            log(f"全局热键：{ACTION_LABELS.get(name, name)} 的键位「{spec}」与"
                f"{ACTION_LABELS.get(taken[spec], taken[spec])} 重复，已跳过")
            continue
        taken[spec] = name
        out[name] = spec
    return out


def _distinct_wanted(cfg: dict) -> int:
    """配置里「想注册几条」：合法且非空的键位个数（未去重）。"""
    n = 0
    for name in ACTIONS:
        raw = cfg.get("hotkey_" + name)
        spec = normalize_spec(HOTKEY_DEFAULTS[name] if raw is None else raw)
        if spec and parse_hotkey(spec) is not None:
            n += 1
    return n


def summary(cfg: dict, failures=()) -> str:
    """菜单 / 日志用的一行摘要；一条都没注册上时说明原因。

    failures 是注册失败（多半被别的程序占用）的展示串列表：热键静默失效最难排查，
    所以在摘要里直接点出来，用户不用去翻日志。
    """
    specs = action_specs(cfg)
    if not specs:
        return "全局热键：已关闭" if not cfg.get("hotkey_enabled", True) else "全局热键：无可用键位"
    line = "全局热键：" + "  ".join(
        f"{format_hotkey(s)}→{ACTION_LABELS.get(name, name)}"
        for name, s in specs.items())
    dropped = _distinct_wanted(cfg) - len(specs)
    if dropped > 0:
        line += f"（{dropped} 条键位与别的动作重复，已按顺序保留前者）"
    if failures:
        line += "（未生效：" + "、".join(failures) + " 被占用）"
    return line


def _register_hotkey(hotkey_id: int, mods: int, vk: int) -> bool:
    """注册一条全局热键；失败（被占用等）返回 False。"""
    return bool(winapi.user32.RegisterHotKey(None, hotkey_id, mods, vk))


def _unregister_hotkey(hotkey_id: int) -> bool:
    return bool(winapi.user32.UnregisterHotKey(None, hotkey_id))


def _poll_message() -> int | None:
    """非阻塞取一条消息：收到 WM_HOTKEY 返回其 id（wParam），否则 None。

    用 PeekMessage 轮询而不是 GetMessage 阻塞：stop() 只要置位事件，最多等
    一个 POLL_SEC 就能退出，不必往线程里 PostThreadMessage（那个调用会因消息
    队列还没建起来而失败，反成坑）。顺带把别处的消息派发掉，别淤在队列里。
    """
    msg = ctypes.wintypes.MSG()
    if not winapi.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, winapi.PM_REMOVE):
        return None
    if msg.message == WM_QUIT:
        return _QUIT
    if msg.message == WM_HOTKEY:
        return int(msg.wParam)
    try:
        winapi.user32.TranslateMessage(ctypes.byref(msg))
        winapi.user32.DispatchMessageW(ctypes.byref(msg))
    except Exception:
        pass
    return None


class HotkeyListener:
    """在独立线程里注册并派发全局热键；stop() 解注册并收尾。

    register / unregister / poll 三个可注入点只为单测服务：不建真热键、真消息
    队列也能覆盖「注册失败跳过」「按 id 派发」「回调抛异常不拖垮循环」。
    """

    def __init__(self, specs, callbacks=None, poll_sec: float = POLL_SEC,
                 register=None, unregister=None, poll=None,
                 retry_sec: float = RETRY_SEC, clock=None):
        self._items = []
        for name, spec in dict(specs or {}).items():
            parsed = parse_hotkey(spec)
            if parsed is None:
                continue
            self._items.append((name, spec, parsed))
        # retry_sec / clock 也只服务单测：测试能用一个假时钟把「5 分钟后」压缩成一次轮询
        self._retry_sec = max(30.0, float(retry_sec))
        self._clock = clock or time.monotonic
        self._cb = dict(callbacks or {})
        self._poll_sec = max(0.01, float(poll_sec))
        self._register = register or _register_hotkey
        self._unregister = unregister or _unregister_hotkey
        self._poll = poll or _poll_message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ids: list[int] = []
        self._by_id: dict[int, str] = {}
        # 注册失败的展示串（被别的程序占用）：交给菜单摘要，别让热键静默失效
        self.failures: list[str] = []

    @property
    def actions(self) -> list:
        """已解析的 [(动作名, 展示键位), ...]，菜单摘要与日志共用。"""
        return [(name, format_hotkey(spec)) for name, spec, _ in self._items]

    def start(self):
        """没有可用键位 / 已在跑时直接返回自身（幂等）。"""
        if not self._items or self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="memguard-hotkeys",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def _run(self) -> None:
        # 先确保本线程有消息队列：RegisterHotKey(hWnd=None) 把 WM_HOTKEY 投到
        # 调用线程的队列，队列不存在时注册会失败；PeekMessage 会顺手把它建起来
        try:
            winapi.user32.PeekMessageW(ctypes.byref(ctypes.wintypes.MSG()), None, 0, 0,
                                       winapi.PM_NOREMOVE)
        except Exception:
            pass
        self._ids = []
        self._by_id = {}
        self.failures = []
        for hotkey_id, (name, spec, (mods, vk)) in enumerate(self._items, start=1):
            if not self._register(hotkey_id, mods, vk):
                log(f"全局热键：{format_hotkey(spec)} 注册失败（可能被别的程序占用），已跳过")
                self.failures.append(format_hotkey(spec))
                continue
            self._ids.append(hotkey_id)
            self._by_id[hotkey_id] = name
        if not self._ids:
            # 一条都没注册上也不退出：占键的程序（输入法 / 远程工具 / QQ）退出后
            # 要能自动恢复，否则用户只能重启 MemGuard。循环里没有可派发的 id，
            # 空转成本只是每 poll_sec 一次 PeekMessage。
            log("全局热键：一条都没注册上，稍后自动重试（托盘与监控不受影响）")
        else:
            log("全局热键已注册：" + "、".join(
                f"{format_hotkey(spec)} {ACTION_LABELS.get(name, name)}"
                for name, spec, _ in self._items))
        next_retry = self._clock() + self._retry_sec
        try:
            while not self._stop.is_set():
                got = self._poll()
                if got is None:
                    self._stop.wait(self._poll_sec)
                    # 有注册失败的热键：隔 retry_sec 再试，占键的程序退出后自动恢复
                    if self.failures and self._clock() >= next_retry:
                        self._retry_failed()
                        next_retry = self._clock() + self._retry_sec
                    continue
                if got == _QUIT:
                    break
                self._dispatch(got)
        finally:
            for hotkey_id in self._ids:
                try:
                    self._unregister(hotkey_id)
                except Exception:
                    pass

    def _retry_failed(self) -> None:
        """重试注册失败的热键：占用它的程序退出后应当自动生效，不必重启本程序。

        只重试失败过的那些（`self.failures` 里的展示串），成功的原样不动；
        再次失败就继续留在 failures 里，菜单摘要也就继续把它点出来。
        """
        still = set(self.failures)
        self.failures = []
        for hotkey_id, (name, spec, (mods, vk)) in enumerate(self._items, start=1):
            shown = format_hotkey(spec)
            if shown not in still:
                continue
            if self._register(hotkey_id, mods, vk):
                self._ids.append(hotkey_id)
                self._by_id[hotkey_id] = name
                label = f"{shown} {ACTION_LABELS.get(name, name)}"
                log(f"全局热键恢复：{label}（此前被别的程序占用）")
            else:
                self.failures.append(shown)

    def _dispatch(self, hotkey_id: int) -> None:
        name = self._by_id.get(int(hotkey_id))
        if name is None:
            return
        cb = self._cb.get(name)
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            log(f"全局热键 {name} 执行失败: {e!r}")

