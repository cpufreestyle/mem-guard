# -*- coding: utf-8 -*-
"""MemGuard - Windows 内存守护托盘工具（多模块版）。

原本是单文件 mem_guard.py，现拆分为 memguard 包：
    config  winapi  clean  tray  cli
本文件只暴露版本号等最外层常量，具体实现见各子模块。
"""

from .config import REPO_SLUG, __version__

__all__ = ["__version__", "REPO_SLUG"]
