# -*- coding: utf-8 -*-
"""update：版本号解析与比较（纯函数，不发网络请求）。"""
from memguard.update import _parse_version


def test_parse_version_basic():
    assert _parse_version("1.3.0") == (1, 3, 0)
    assert _parse_version("v1.3.0") == (1, 3, 0)
    assert _parse_version("V1.3.0") == (1, 3, 0)


def test_parse_version_ignores_non_numeric_suffix():
    assert _parse_version("v1.3.0-beta") == (1, 3, 0)
    assert _parse_version("1.3.0") == _parse_version("v1.3.0")


def test_parse_version_ordering():
    assert _parse_version("v1.10.2") > _parse_version("1.9.9")
    assert _parse_version("1.3.10") > _parse_version("1.3.9")
    assert not _parse_version("1.3.9") > _parse_version("1.3.10")


def test_parse_version_empty():
    assert _parse_version("") == (0,)
    assert _parse_version("v") == (0,)
