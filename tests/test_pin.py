# -*- coding: utf-8 -*-
r"""任务栏/桌面快捷方式脚本与进程侧 AppUserModelID 的对账。

pin_taskbar.ps1 里的 System.AppUserModel.ID 与 memguard\winapi.py 的 APP_USER_MODEL_ID
必须同值，否则任务栏上会出现两枚按钮：快捷方式一枚、运行中的程序一枚。
这里把两边读出来对账，任何一边漂移测试立刻红。
"""
import os

from memguard.winapi import APP_USER_MODEL_ID

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "pin_taskbar.ps1")


def _script_text() -> str:
    # ps1 是 UTF-8 with BOM：utf-8-sig 才能把签名吃掉，否则第一行带着乱码
    with open(_SCRIPT, encoding="utf-8-sig") as f:
        return f.read()


def test_script_exists_and_has_param_block() -> None:
    text = _script_text()
    assert text.startswith("#"), "脚本应以注释头开头"
    assert "param(" in text
    assert "$ErrorActionPreference = 'Stop'" in text


def test_app_user_model_id_matches_runtime() -> None:
    """两份 AUMID 必须逐字相同：这是任务栏只出现一枚按钮的前提。"""
    text = _script_text()
    assert APP_USER_MODEL_ID in text
    assert APP_USER_MODEL_ID == "MemGuard.MemoryGuard"


def test_script_defines_pkey_app_user_model_id() -> None:
    """属性键必须是 PKEY_AppUserModel_ID，GUID 或 propertyId 写错会静默不生效。"""
    text = _script_text()
    assert "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3" in text
    assert "propertyId = 5" in text


def test_cli_sets_aumid_before_launching_tray() -> None:
    """源码运行时 tk 进程的身份是 python.exe，不设 AUMID 会显示 Python 的图标。"""
    with open(os.path.join(_ROOT, "memguard", "cli.py"), encoding="utf-8") as f:
        cli = f.read()
    assert "set_app_user_model_id()" in cli
    assert cli.index("set_app_user_model_id()") < cli.index("Guard().run(")


def test_shortcut_passes_show_flag() -> None:
    """不带 --show 的话，点快捷方式只起一个托盘实例，用户以为"点了没反应"。"""
    text = _script_text()
    assert "--show" in text
    assert "$ScriptArgs" in text
    assert "$sc.Arguments = $ScriptArgs" in text


def test_shortcut_runs_as_administrator() -> None:
    """清理要动别的进程的工作集，快捷方式应与（管理员）.bat、计划任务同权限。"""
    text = _script_text()
    assert "SLDF_RUNAS_USER" in text
    assert "0x2000" in text
    assert "LINK_HEADER_SIZE" in text


def test_script_covers_taskbar_and_desktop() -> None:
    text = _script_text()
    assert "User Pinned" in text
    assert "TaskBar" in text
    assert "GetFolderPath('Desktop')" in text
    for mode in ("pin", "unpin"):
        assert "'" + mode + "'" in text
    for where in ("taskbar", "desktop", "both"):
        assert "'" + where + "'" in text


def test_script_encoding_is_utf8_bom_with_crlf() -> None:
    """仓库约定：.ps1 用 UTF-8 with BOM + CRLF，PS 5.1 按 BOM 判编码，否则中文乱码。"""
    with open(_SCRIPT, "rb") as f:
        raw = f.read()
    assert raw[:3] == b"\xef\xbb\xbf", ".ps1 需要 UTF-8 BOM"
    body = raw[3:].decode("utf-8")
    assert "\r\n" in body
    assert "\n" not in body.replace("\r\n", ""), "不能有孤立的 LF"
    assert "\r" not in body.replace("\r\n", ""), "不能有孤立的 CR"

