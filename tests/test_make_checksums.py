# -*- coding: utf-8 -*-
"""make_checksums：清单格式必须正是 update.parse_checksums / verify_asset 认的那种。

这条测试存在的理由很直接：清单格式一旦和客户端对不上，更新链路会全线拒装，而
两边各自实现时只有一边会响。所以这里不只看输出长什么样，而是把生成结果直接喂给
客户端的解析与校验函数做往返——格式漂移立刻红。
"""
import hashlib
import os
import subprocess
import sys

import pytest

import make_checksums as mc
from memguard.update import parse_checksums, verify_asset

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(ROOT), "make_checksums.py")
PATTERNS = ["mem_guard.exe", "MemGuard-Setup-*.exe"]


def _run(args):
    return subprocess.run([sys.executable, "-X", "utf8", SCRIPT] + args,
                          cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@pytest.fixture()
def dist(tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    (d / "mem_guard.exe").write_bytes(b"portable payload")
    (d / "MemGuard-Setup-1.23.0.exe").write_bytes(b"installer payload")
    (d / "README.md").write_bytes(b"not an asset")
    return d


def test_manifest_uses_the_format_the_client_parses(dist):
    lines = mc.build_lines(str(dist), PATTERNS)
    assert len(lines) == 2, lines
    for line in lines:
        digest, sep, name = line.partition("  ")
        assert sep == "  ", "必须是 coreutils 的两个空格，客户端按它切分"
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
        assert name in ("mem_guard.exe", "MemGuard-Setup-1.23.0.exe")


def test_manifest_round_trips_through_the_client(dist):
    """生成的清单要能被 parse_checksums 找到、被 verify_asset 校验通过。"""
    text = "\n".join(mc.build_lines(str(dist), PATTERNS))
    for name in ("mem_guard.exe", "MemGuard-Setup-1.23.0.exe"):
        expect = hashlib.sha256((dist / name).read_bytes()).hexdigest()
        assert parse_checksums(text, name) == expect, name
        assert verify_asset(str(dist / name), expect)["ok"] is True, name


def test_manifest_ignores_unmatched_files(dist):
    names = [l.split("  ", 1)[1] for l in mc.build_lines(str(dist), PATTERNS)]
    # 顺序跟着 --patterns 走，可预测；同一模式内按文件名排序
    assert names == ["mem_guard.exe", "MemGuard-Setup-1.23.0.exe"], names
    assert "README.md" not in names


def test_missing_asset_fails_under_strict(dist):
    """strict 下缺一个制品就要失败：否则会发出一份缺条目的清单。

    客户端找不到那个制品的摘要就会拒装，而构建一路是绿的——发布路径必须 strict。
    """
    (dist / "mem_guard.exe").unlink()
    with pytest.raises(SystemExit):
        mc.build_lines(str(dist), PATTERNS, strict=True)


def test_missing_asset_is_tolerated_without_strict(dist):
    """非 strict 允许「只出便携、没编译安装器」：本地打包就是这个形态。"""
    (dist / "mem_guard.exe").unlink()
    names = [l.split("  ", 1)[1] for l in mc.build_lines(str(dist), PATTERNS)]
    assert names == ["MemGuard-Setup-1.23.0.exe"], names


def test_empty_dir_makes_the_build_fail(tmp_path):
    (tmp_path / "dist").mkdir()
    with pytest.raises(SystemExit):
        mc.build_lines(str(tmp_path / "dist"), PATTERNS)


def test_cli_writes_the_file_and_prints_lines(dist, tmp_path):
    out = tmp_path / "dist" / "checksums.txt"
    r = _run(["--dir", str(dist), "--patterns"] + PATTERNS + ["--out", str(out)])
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")
    body = out.read_text(encoding="ascii")
    assert body.endswith("\n") and "\r" not in body
    assert "mem_guard.exe" in body and "MemGuard-Setup-1.23.0.exe" in body
    assert "已写入" in r.stdout.decode("utf-8", "replace")


def test_cli_is_deterministic(dist, tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    for target in (a, b):
        assert _run(["--dir", str(dist), "--patterns"] + PATTERNS
                    + ["--out", str(target)]).returncode == 0
    assert a.read_text(encoding="ascii") == b.read_text(encoding="ascii")


def test_cli_without_out_only_prints(dist):
    r = _run(["--dir", str(dist), "--patterns"] + PATTERNS)
    assert r.returncode == 0
    out = r.stdout.decode("utf-8", "replace")
    assert out.count("\n") == 2 and "已写入" not in out


def test_cli_fails_when_dir_has_nothing(tmp_path):
    (tmp_path / "dist").mkdir()
    r = _run(["--dir", str(tmp_path / "dist"), "--patterns"] + PATTERNS)
    assert r.returncode != 0
    assert "找不到任何文件" in r.stdout.decode("utf-8", "replace")

def test_sha256_is_the_same_function_the_client_uses():
    """清单与客户端校验必须用同一个哈希函数，不能各写一份。

    make_checksums 一开始自己实现了一遍流式 sha256_of——客户端校验下载文件时用的
    是 update.sha256_of。两边各写一份，改了这边忘了那边，清单就会和校验对不上，
    而症状只是「更新失败」，很难联想到格式。
    """
    from memguard.update import sha256_of as client_sha

    assert mc.sha256_of is client_sha


def test_generated_manifest_verifies_through_the_client(tmp_path, monkeypatch):
    """端到端：make_checksums 生成的清单要被 update._verify_download 认下来。

    这是自动更新真正走的那条路：CI 生成 checksums.txt，Release 发布，客户端下载
    安装包，再按清单比对。前面几条只分别验证了生成端和校验端的格式，而格式恰恰是
    两边各写一遍时最容易对不上的——只有把两端串起来才证明它们配套。

    顺带断言清单里没有的包会被拒：那正是「下载到了别的东西」的情形。
    """
    from memguard import update as update_mod

    dist = tmp_path / "dist"
    dist.mkdir()
    names = ("mem_guard.exe", "MemGuard-Setup-1.23.0.exe")
    for name in names:
        (dist / name).write_bytes(b"payload-" + name.encode())
    out = dist / "checksums.txt"
    assert mc.main(["--dir", str(dist), "--patterns", "mem_guard.exe",
                    "MemGuard-Setup-*.exe", "--out", str(out)]) == 0
    manifest = out.read_text(encoding="ascii")

    release_assets = [
        {"name": "mem_guard.exe", "browser_download_url": "https://x/mem_guard.exe"},
        {"name": "MemGuard-Setup-1.23.0.exe",
         "browser_download_url": "https://x/setup"},
        {"name": "checksums.txt", "browser_download_url": "https://x/checksums"},
    ]
    monkeypatch.setattr(update_mod, "fetch_checksums",
                        lambda url, timeout=30: {"ok": True, "text": manifest})

    for name in names:
        r = update_mod._verify_download(
            {"name": name, "url": "https://x/" + name}, release_assets,
            str(dist / name))
        assert r.get("ok") is True, (name, r)

    stranger = tmp_path / "other.exe"
    stranger.write_bytes(b"payload-other")
    r = update_mod._verify_download({"name": "other.exe", "url": "https://x/other"},
                                   release_assets, str(stranger))
    assert r.get("ok") is False and "摘要" in r["msg"], r
