# -*- coding: utf-8 -*-
"""pytest 公共配置：把仓库根加入 sys.path，使测试可以直接 `import memguard`。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
