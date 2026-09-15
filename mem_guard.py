# -*- coding: utf-8 -*-
"""MemGuard 启动入口（薄启动器）。

真正的逻辑在 memguard 包内。本文件只负责转发到 cli.main()，
以便保持 build.ps1 / .bat / CI 中 "python mem_guard.py" 的入口不变。
"""
from memguard.cli import main

if __name__ == "__main__":
    main()
