# -*- coding: utf-8 -*-
"""生成 Release 的 sha256 清单（checksums.txt）。

自动更新在跑引导批处理之前会拿这份清单比对下载文件的哈希（memguard/update.py 的
_verify_download），所以格式必须正是 parse_checksums 认的那种：coreutils 的
"<sha256>  <文件名>"，两个空格。客户端只认这一种，清单格式错了 = 更新全线拒装。

CI 的 release.yml 与本地 build.ps1 都调本脚本。原先两处各写一遍 PowerShell，格式
漂移时只有一边能装、另一边静默失败——而 tests/test_make_checksums.py 会把本脚本的
输出直接喂给 update.parse_checksums / verify_asset 做往返验证，格式漂移立刻红。

sha256_of 直接 import 自 memguard.update.py——客户端校验下载文件时用的就是同一个函数。
两边各写一份流式哈希，改了一处漏一处，清单就会和校验对不上。

用法：
    python make_checksums.py --dir dist --patterns "mem_guard.exe" "MemGuard-Setup-*.exe" \
        --out dist/checksums.txt
不给 --out 时只打印到标准输出，便于手工核对。退出码非 0 表示清单不完整——宁可让
构建失败，也不要发出一份缺条目的清单。
"""
import argparse
import os
import sys

from memguard.update import sha256_of

SEP = "  "          # coreutils 的两个空格：parse_checksums 按它切分


def collect(directory: str, patterns, strict: bool = False) -> list:
    """按 glob 模式收集目录下的文件，返回 [(路径, 名字)]。

    strict=True 时每个模式都必须匹配到至少一个文件，否则抛错——发布路径用它：
    PyInstaller 或 ISCC 悄悄没产出某个制品时，不能只发一份缺条目的清单出去，
    客户端会因为找不到那个制品的摘要而拒装，而构建一路是绿的。
    本地打包可能是「只出便携、没编译安装器」，所以 build.ps1 用非 strict。
    """
    import fnmatch

    out, seen = [], set()
    for pat in patterns:
        hits = sorted(fnmatch.filter(os.listdir(directory), pat)) \
            if os.path.isdir(directory) else []
        if not hits and strict:
            raise SystemExit(
                "目录 %s 下模式 %s 一个文件都没匹配到：制品缺失，不能发缺条目的清单"
                % (directory, pat))
        for name in hits:
            full = os.path.join(directory, name)
            if os.path.isfile(full) and full not in seen:
                seen.add(full)
                out.append((full, name))
    return out


def build_lines(directory: str, patterns, strict: bool = False) -> list:
    """生成清单行；有任何文件读不到就抛错，绝不输出缺条目的清单。"""
    files = collect(directory, patterns, strict)
    if not files:
        raise SystemExit("目录 %s 下按模式 %s 找不到任何文件" % (directory, list(patterns)))
    lines = []
    for full, name in files:
        digest = sha256_of(full)
        if not digest:
            raise SystemExit("读不到文件，无法计算 sha256：%s" % full)
        lines.append(digest + SEP + name)
    text = "\n".join(lines) + "\n"
    # 清单要经手 ascii/utf-8 两边都安全的通道，非 ascii 文件名先喊出来
    try:
        text.encode("ascii")
    except UnicodeEncodeError as e:
        raise SystemExit("清单里有非 ascii 文件名（%s），先确认客户端能解" % e)
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成 Release 的 sha256 清单")
    ap.add_argument("--dir", default="dist", help="产物所在目录（默认 dist）")
    ap.add_argument("--patterns", nargs="+", required=True,
                    help='文件名 glob，如 "mem_guard.exe" "MemGuard-Setup-*.exe"')
    ap.add_argument("--out", help="输出路径；不给则只打印到标准输出")
    ap.add_argument("--strict", action="store_true",
                    help="每个模式都必须匹配到文件；发布路径用，缺制品即失败")
    args = ap.parse_args(argv)

    lines = build_lines(args.dir, args.patterns, args.strict)
    if args.out:
        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        with open(args.out, "w", encoding="ascii", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
    for line in lines:
        print(line)
    if args.out:
        print("已写入 " + args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
