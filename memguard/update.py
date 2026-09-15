# -*- coding: utf-8 -*-
"""
更新检查：查询 GitHub 最新 Release 并比较版本号。

只依赖标准库（urllib）+ config；被 tray 菜单（后台线程）与 cli 自检调用。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import REPO_SLUG, __version__


def _parse_version(v: str) -> tuple:
    """把 'v1.3.0' / '1.3.0' 解析为可比较的整数元组（非数字后缀按 0 处理）。"""
    nums = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums) if nums else (0,)


def fetch_latest_release(timeout: int = 8) -> dict:
    """查询 GitHub 最新 Release。

    返回 {"ok": True, "tag", "url", "newer"} 或 {"ok": False, "msg"}。
    仅用标准库 urllib，PyInstaller 打包后无需额外依赖。
    """
    url = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"MemGuard/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "msg": "该仓库暂无 Release（或不可访问）"}
        return {"ok": False, "msg": f"检查更新失败：HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "msg": f"检查更新失败：{e}"}

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return {"ok": False, "msg": "最新 Release 缺少版本号"}
    return {
        "ok": True,
        "tag": tag,
        "url": data.get("html_url") or f"https://github.com/{REPO_SLUG}/releases",
        "newer": _parse_version(tag) > _parse_version(__version__),
    }
