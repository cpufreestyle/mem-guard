# -*- coding: utf-8 -*-
"""两条 CI 闸门自身的测试：光「在自己的仓库上是绿的」不算数，还得证明它们拦得住。

闸门没有测试，就等于没有闸门——它自己悄悄坏掉（正则写歪、路径改错、该报的静默
跳过）时，CI 只会一路放行，而没人会发现。所以这里既跑正向（真仓库必须过），也跑
反向（注入每一种坏味道都必须红）。
"""
import io
import os

import subprocess
import sys

import check_line_endings as eol
import check_version_sync as vsync


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VS_SCRIPT = os.path.join(ROOT, "check_version_sync.py")


def _write(path, text):
    with io.open(str(path), "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return str(path)


# ------------------------------------------------ check_version_sync

def test_version_gate_passes_on_the_real_repo():
    assert vsync.main() == 0


def test_version_gate_rejects_handoff_ahead_of_code(monkeypatch, tmp_path):
    """交接文档声明了一个代码里还不存在的版本。"""
    monkeypatch.setattr(vsync, "CONFIG",
                        _write(tmp_path / "config.py", '__version__ = "1.22.0"' + chr(10)))
    monkeypatch.setattr(vsync, "HANDOFF", _write(
        tmp_path / "HANDOFF.md",
        "- 最新已发布 tag：" + chr(96) + "v1.23.0" + chr(96) + chr(10)))
    assert vsync.main() == 1


def test_version_gate_rejects_undocumented_version(monkeypatch, tmp_path):
    """版本号改了却没写交接文档。"""
    monkeypatch.setattr(vsync, "CONFIG",
                        _write(tmp_path / "config.py", '__version__ = "1.99.0"' + chr(10)))
    monkeypatch.setattr(vsync, "HANDOFF", _write(
        tmp_path / "HANDOFF.md",
        "- 最新已发布 tag：" + chr(96) + "v1.22.0" + chr(96) + chr(10)))
    assert vsync.main() == 1


def test_version_gate_rejects_missing_latest_tag_line(monkeypatch, tmp_path):
    monkeypatch.setattr(vsync, "CONFIG",
                        _write(tmp_path / "config.py", '__version__ = "1.23.0"' + chr(10)))
    monkeypatch.setattr(vsync, "HANDOFF",
                        _write(tmp_path / "HANDOFF.md", "# 没有版本行" + chr(10)))
    assert vsync.main() == 1


def test_version_gate_rejects_tag_mismatch_on_tag_push(monkeypatch, tmp_path):
    """推的 tag 与代码版本不一致。"""
    monkeypatch.setattr(vsync, "CONFIG",
                        _write(tmp_path / "config.py", '__version__ = "1.23.0"' + chr(10)))
    monkeypatch.setattr(vsync, "HANDOFF", _write(
        tmp_path / "HANDOFF.md",
        "- 最新已发布 tag：" + chr(96) + "v1.23.0" + chr(96) + chr(10)
        + "v1.23.0 已记录" + chr(10)))
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "v1.24.0")
    assert vsync.main() == 1


def test_version_gate_accepts_matching_tag_on_tag_push(monkeypatch, tmp_path):
    monkeypatch.setattr(vsync, "CONFIG",
                        _write(tmp_path / "config.py", '__version__ = "1.23.0"' + chr(10)))
    monkeypatch.setattr(vsync, "HANDOFF", _write(
        tmp_path / "HANDOFF.md",
        "- 最新已发布 tag：" + chr(96) + "v1.23.0" + chr(96) + chr(10)
        + "v1.23.0 已记录" + chr(10)))
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "v1.23.0")
    assert vsync.main() == 0

def test_version_gate_rejects_conflicting_latest_tags(monkeypatch, tmp_path):
    """文档里出现两个不同的「最新已发布 tag」必须红，不能静默取第一个。

    原先 _latest_tag 见到第一个命中就返回。第二处无论是误写还是过期残留，闸门
    都会拿第一行的值去比——读到过期那行时看起来还是绿的。
    """
    monkeypatch.setattr(vsync, "CONFIG",
                        _write(tmp_path / "config.py", '__version__ = "1.23.0"' + chr(10)))
    monkeypatch.setattr(vsync, "HANDOFF", _write(
        tmp_path / "HANDOFF.md",
        "- 最新已发布 tag：" + chr(96) + "v1.23.0" + chr(96) + chr(10)
        + "- 另一处误写：最新已发布 tag：" + chr(96) + "v1.20.0" + chr(96) + chr(10)
        + "v1.23.0 已记录" + chr(10)))
    assert vsync.main() == 1


def test_version_gate_tolerates_repeated_same_tag(monkeypatch, tmp_path):
    """同一个值写两遍只是冗余，不该误报。"""
    monkeypatch.setattr(vsync, "CONFIG",
                        _write(tmp_path / "config.py", '__version__ = "1.23.0"' + chr(10)))
    monkeypatch.setattr(vsync, "HANDOFF", _write(
        tmp_path / "HANDOFF.md",
        "- 最新已发布 tag：" + chr(96) + "v1.23.0" + chr(96) + chr(10)
        + "- 另一处同样写：最新已发布 tag：" + chr(96) + "v1.23.0" + chr(96) + chr(10)
        + "v1.23.0 已记录" + chr(10)))
    assert vsync.main() == 0


# ------------------------------------------------ check_line_endings

def test_no_tracked_file_would_commit_a_bad_eol():
    """任何跟踪文件：模拟 git add 的清洗后都不许带 CRLF 或裸 CR。

    这条是被真实事故逼出来的：README.md 开头曾混进一个裸 CR，git 因此把整个文件
    标成 -text；而行尾闸门读的是**提交内容**，于是本地一路全绿、直到提交后才红。
    git 的清洗只会把 CRLF 换成 LF、洗不掉裸 CR，所以先模拟清洗再判，正好补上闸门
    「只查提交内容」的那个盲区。

    不断言工作树本身就是 LF：core.autocrlf=true 会在 checkout 时把 LF 翻回 CRLF，
    那是正常工作树形态，git 自己也会那么警告。
    """
    bad = []
    for rel in eol._tracked_text_files():
        with io.open(os.path.join(ROOT, rel.replace("/", os.sep)), "rb") as f:
            blob = f.read()
        cleaned = blob.replace(b"\r\n", b"\n")   # git add 会做的事
        problems = []
        eol._check_blob(rel, cleaned, problems)
        bad.extend(problems)
    assert not bad, bad


def test_check_blob_passes_pure_lf():
    """纯 LF 必须放行——这里曾经写成 crlf != lf，把纯 LF 也判成不合规。"""
    problems = []
    eol._check_blob("a.py", b"x = 1\ny = 2\n", problems)
    assert problems == [], problems


def test_check_blob_flags_crlf():
    problems = []
    eol._check_blob("a.py", b"x = 1\r\ny = 2\r\n", problems)
    assert any("CRLF" in p for p in problems), problems


def test_check_blob_flags_bare_cr():
    """裸 CR（老 Mac 行尾）也要红：git 会因此把文件标成 -text。"""
    problems = []
    eol._check_blob("a.py", b"# -*- coding: utf-8 -*-\r\r\nx = 1\r\r\n", problems)
    assert any("裸 CR" in p for p in problems), problems


def test_check_blob_flags_bom_but_exempts_ps1():
    problems = []
    eol._check_blob("a.py", b"\xef\xbb\xbfx = 1\n", problems)
    assert any("BOM" in p for p in problems), problems
    ok = []
    eol._check_blob("build.ps1", b"\xef\xbb\xbfWrite-Host 'x'\n", ok)
    assert ok == [], ok


def test_check_blob_flags_non_utf8():
    problems = []
    eol._check_blob("a.py", b"\xff\xfe\x00bad\n", problems)
    assert any("UTF-8" in p for p in problems), problems


def test_eol_gate_flags_bom_and_bad_utf8(monkeypatch, tmp_path):
    """未跟踪的新文件：BOM（.ps1 除外）与非 UTF-8 都要红。"""
    (tmp_path / "ok.py").write_bytes(b"x = 1\n")
    (tmp_path / "bom.py").write_bytes(b"\xef\xbb\xbfx = 1\n")
    (tmp_path / "ok_bom.ps1").write_bytes(b"\xef\xbb\xbfWrite-Host 'x'\n")
    (tmp_path / "bin.py").write_bytes(b"\xff\xfe\x00bad\n")
    monkeypatch.setattr(eol, "ROOT", str(tmp_path))
    monkeypatch.setattr(eol, "_tracked_text_files", lambda: [])
    monkeypatch.setattr(eol, "_git_blob", lambda rel: None)
    assert eol.main() == 1


def test_eol_gate_flags_crlf_committed_to_git(monkeypatch, tmp_path):
    """blob 里是 CRLF 就要红——这正是 autocrlf 被关掉的机器会造成的漂移。"""
    monkeypatch.setattr(eol, "_tracked_text_files", lambda: ["a.py"])
    monkeypatch.setattr(eol, "_git_blob",
                        lambda rel: b"x = 1\r\ny = 2\r\n" if rel == "a.py" else None)
    monkeypatch.setattr(eol, "ROOT", str(tmp_path))
    assert eol.main() == 1


def test_eol_gate_ignores_crlf_in_working_tree(monkeypatch, tmp_path):
    """工作树是 crlf 不该红：autocrlf=true 的机器上那就是正确形态。"""
    (tmp_path / "a.py").write_bytes(b"x = 1\r\n")
    monkeypatch.setattr(eol, "_tracked_text_files", lambda: ["a.py"])
    monkeypatch.setattr(eol, "_git_blob", lambda rel: b"x = 1\n")
    monkeypatch.setattr(eol, "ROOT", str(tmp_path))
    assert eol.main() == 0


def test_eol_gate_skips_vendored_and_generated_dirs(monkeypatch, tmp_path, capsys):
    for rel in (".qoder", os.path.join("tools", "upx-4.2.4-win64"), "dist"):
        d = tmp_path / rel
        d.mkdir(parents=True)
        (d / "x.md").write_bytes(b"\xef\xbb\xbfboom\n")
    (tmp_path / "real.md").write_bytes(b"\xef\xbb\xbfboom\n")
    monkeypatch.setattr(eol, "ROOT", str(tmp_path))
    monkeypatch.setattr(eol, "_tracked_text_files", lambda: [])
    capsys.readouterr()
    assert eol.main() == 1
    out = capsys.readouterr().out
    assert "real.md: 带 UTF-8 BOM" in out
    assert ".qoder" not in out and "upx-4.2.4-win64" not in out


# ------------------------------------------------ 版本输出的单一来源

def _print_version():
    r = subprocess.run([sys.executable, "-X", "utf8", VS_SCRIPT, "--print-version"],
                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")
    return r.stdout.decode("utf-8").strip()


def test_print_version_matches_the_module():
    """--print-version 必须与 memguard.config.__version__ 一字不差。

    release.yml 编译安装器时用它拿 AppVersion。这里和模块真实值不一致，装出来的
    安装器版本就不是正在构建的这个代码。
    """
    from memguard.config import __version__

    assert _print_version() == __version__


def test_print_version_matches_the_regex_the_workflow_used_to_have():
    """与 workflow 原先那条 PowerShell 正则的提取结果一致——换实现不改行为。"""
    import re

    with io.open(vsync.CONFIG, encoding="utf-8") as f:
        text = f.read()
    old = re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)
    assert _print_version() == old
