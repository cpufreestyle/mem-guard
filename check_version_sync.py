# -*- coding: utf-8 -*-
"""版本一致性闸门：代码里的 __version__ 必须与 HANDOFF.md 的发布记录对得上。

为什么需要它：__version__（memguard/config.py）与 HANDOFF.md §1 的「最新已发布
tag」是两处人工同步，v1.6.0–v1.22.0 的回填就是因为这类漂移欠下的债。这个脚本把
两条不变量变成 CI 里的一条硬闸：

  1. HANDOFF 记录的「最新已发布 tag」不许领先于代码版本——那意味交接文档声明了
     一个代码里还不存在的版本；
  2. 当前 __version__ 必须能在 HANDOFF 里找到对应记录——版本号改了却没写交接文档，
     下一个接手的人就看不到这个版本做过什么；
  3. 在 tag 推送（GitHub Actions 的 release.yml）里，tag 必须恰好等于 v__version__，
     不允许「tag 打错了版本号」。

只读文件、不发网络请求，退出码 0 = 通过，1 = 不通过（附原因）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "memguard", "config.py")
HANDOFF = os.path.join(ROOT, "HANDOFF.md")

_Q = chr(34)
_TICK = chr(96)


def _latest_tags(text):
    """HANDOFF 里所有「最新已发布 tag」后面的版本号，反引号或双引号都认。

    按行取而不是一条大正则：那一行后面还跟着一长串括注，正则越写越脆。
    返回**所有**命中而不是第一个——出现第二处时调用方必须发现，否则静默取
    第一条有可能读到过期那行，而闸门看起来还是绿的。
    """
    tags = []
    for line in text.splitlines():
        if "最新已发布" not in line:
            continue
        m = re.search(r"最新已发布[^：\n]*：\s*[" + _TICK + _Q + r"]?(v[0-9][0-9A-Za-z.\-]*)", line)
        if m:
            tags.append(m.group(1))
    return tags


_VERSION_RE = re.compile(r"^__version__\s*=\s*" + _Q + "([^" + _Q + "]+)" + _Q, re.MULTILINE)


def _parse(v):
    nums = []
    for part in str(v or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums) if nums else (0,)


def _read(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def _read_version() -> str:
    """从 memguard/config.py 读 __version__；读不到直接退出。

    发布路径（release.yml 编译安装器那一步）也要这个版本号，原先那里自己写了一条
    PowerShell 正则。两处解析口径一旦对不上，装出来的安装器版本就和闸门校验过的
   版本不是同一个——所以这里也提供 --print-version，让发布路径复用同一份解析。
    """
    try:
        return _VERSION_RE.search(_read(CONFIG)).group(1)
    except (OSError, AttributeError):
        raise SystemExit("读不到 memguard/config.py 里的 __version__")


def main():
    problems = []
    try:
        version = _read_version()
    except SystemExit as e:
        print(e)
        return 1
    try:
        handoff = _read(HANDOFF)
    except OSError:
        print("读不到 HANDOFF.md")
        return 1

    tags = _latest_tags(handoff)
    if not tags:
        problems.append("HANDOFF.md §1 里找不到「最新已发布 tag：vX.Y.Z」这一行")
    elif len(set(tags)) > 1:
        problems.append(
            "HANDOFF.md 里「最新已发布 tag」出现了多个不同值 %s：闸门不知道该信哪个"
            % sorted(set(tags)))
    else:
        documented = tags[0]
        if _parse(documented) > _parse(version):
            problems.append(
                "HANDOFF 记录的最新 tag %s 领先于代码版本 %s：交接文档声明了一个代码里还不存在的版本"
                % (documented, version))

    if ("v" + version) not in handoff:
        problems.append(
            "HANDOFF.md 里找不到 v%s 的任何记录：版本号改了却没写交接文档" % version)

    # tag 推送时（release.yml）额外要求 tag 与 __version__ 严格一致
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME") or ""
        if tag != "v" + version:
            problems.append("推送的 tag %s 与代码版本 v%s 不一致" % (tag or "(空)", version))

    if problems:
        for p in problems:
            print("版本不一致: " + p)
        return 1
    print("版本一致: __version__=%s，HANDOFF 已记录" % version)
    return 0


if __name__ == "__main__":
    if "--print-version" in sys.argv[1:]:
        print(_read_version())
        sys.exit(0)
    sys.exit(main())
