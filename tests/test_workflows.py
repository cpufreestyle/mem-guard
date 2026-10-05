# -*- coding: utf-8 -*-
"""两个 workflow 必须能解析、且都跑同两条闸门、发布带上 checksums.txt。


release.yml 是唯一真正会发布的路径，而 YAML 语法错误只有推 tag 才会暴露——那时
已经来不及了。所以在这里先把两个文件解析一遍，再把三条硬要求钉住：

  1. 两个 workflow 都能被 YAML 解析器读进来；
  2. 版本闸门与行尾闸门**两边都跑**——只给 push/PR 装、不给发布路径装，等于把
     最容易出事的那个入口漏掉；
  3. checksums.txt 既要生成、也要进构建产物与 Release 附件——客户端在跑引导批处理
     之前会拿它校验，缺一份就拒绝安装。
"""
import io
import os

import pytest

ruamel = pytest.importorskip("ruamel.yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(ROOT, ".github", "workflows")


def _load(name):
    # importorskip 返回的就是 ruamel.yaml 模块本身
    yaml = ruamel.YAML(typ="safe")
    with io.open(os.path.join(WORKFLOW_DIR, name), encoding="utf-8") as f:
        return yaml.load(f)


def _steps(name):
    doc = _load(name)
    job = next(iter((doc.get("jobs") or {}).values()))
    return job.get("steps") or []


def _runs(name):
    return [str(s.get("run", "")) for s in _steps(name)]


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflow_parses(name):
    doc = _load(name)
    assert doc.get("jobs"), name + " 里没有 job"


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_both_workflows_run_both_gates(name):
    runs = _runs(name)
    assert any("check_version_sync.py" in r for r in runs), \
        name + " 没跑版本一致性闸门"
    assert any("check_line_endings.py" in r for r in runs), \
        name + " 没跑行尾闸门"


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_both_workflows_run_tests_and_selftest(name):
    runs = _runs(name)
    assert any("pytest" in r for r in runs), name + " 没跑单元测试"
    assert any("--selftest" in r for r in runs), name + " 没跑自检"


def test_release_generates_and_publishes_checksums():
    steps = _steps("release.yml")
    assert any("checksums.txt" in str(s.get("run", "")) for s in steps), \
        "release.yml 必须在打包之后生成 checksums.txt"
    upload = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))]
    assert upload and "dist/checksums.txt" in str(upload[0].get("with", {}).get("path")), \
        "checksums.txt 必须进构建产物"
    release = [s for s in steps
               if str(s.get("uses", "")).startswith("softprops/action-gh-release")]
    assert release, "release.yml 缺少发布 Release 的步骤"
    files = str(release[0].get("with", {}).get("files"))
    assert "dist/checksums.txt" in files, \
        "checksums.txt 必须随 Release 发布，否则客户端会拒绝安装"


def test_release_gate_runs_before_packaging():
    """闸门要跑在打包之前：等产物出来了再发现版本不一致就白跑一趟。"""
    names = [str(s.get("name", s.get("uses", ""))) for s in _steps("release.yml")]
    gate = next(i for i, n in enumerate(names) if "版本与交接文档一致性" in n)
    pack = next(i for i, n in enumerate(names) if "打包单文件 exe" in n)
    assert gate < pack, (gate, pack)


def test_release_reads_version_from_the_gate_script():
    """安装器的 AppVersion 必须从 check_version_sync.py --print-version 读。

    原先那一步自己写了一条 PowerShell 正则解析 __version__——和版本闸门里的
    Python 正则各一份。两处口径一旦对不上，装出来的安装器版本就不是闸门刚刚校验过
    的那个版本，而症状只是「装完显示的版本号不对」，很难联想到解析。
    """
    steps = _steps("release.yml")
    # workflow 里写的是反斜杠（installer\mem_guard.iss），所以只按文件名找
    install = [s for s in steps if "mem_guard.iss" in str(s.get("run", ""))]
    assert install, "release.yml 里找不到编译安装器的步骤"
    run = str(install[0].get("run", ""))
    assert "check_version_sync.py --print-version" in run, run
    assert "Select-String" not in run, "又回到自己写正则解析 __version__ 了"
