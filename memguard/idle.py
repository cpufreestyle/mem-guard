# -*- coding: utf-8 -*-
"""
用户空闲判定：人不在时不弹气泡（「无事不打扰」的最后一环）。

托盘工具最常见的打扰是「人走了，它还在叭叭地报」。这里用 GetLastInputInfo 取
最后一次键鼠输入到现在过了多久，超过 notify_idle_sec 的通知只记账、不弹，
等用户回来时把离开期间的几条汇总成一条——既不漏信息，也不在空办公室里刷屏。

取不到（非 Windows / 调用失败）时返回 None，调用方按「不空闲」处理：宁可照常
打扰，也不能把一次探测失败变成永久静默。
"""
from __future__ import annotations

import ctypes

from . import winapi

# GetTickCount 约 49.7 天回绕一次：用掩码做无符号减法，跨过回绕点也算得对
_TICK_MASK = 0xFFFFFFFF


def idle_seconds() -> float | None:
    """距最后一次键鼠输入多少秒；取不到返回 None。"""
    try:
        info = winapi.LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(winapi.LASTINPUTINFO)
        if not winapi.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        elapsed = (winapi.kernel32.GetTickCount() - info.dwTime) & _TICK_MASK
        return max(0.0, float(elapsed) / 1000.0)
    except Exception:
        return None


def is_user_away(threshold_sec) -> bool:
    """用户是否已离开超过 threshold_sec；阈值 <=0（关）或取不到时一律 False。"""
    try:
        threshold = float(threshold_sec or 0)
    except (TypeError, ValueError):
        return False
    if threshold <= 0:
        return False
    idle = idle_seconds()
    return idle is not None and idle >= threshold

