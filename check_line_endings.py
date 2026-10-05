# -*- coding: utf-8 -*-
"""仓库行尾 / 编码闸门：校验的是**提交进 git 的内容**，不是工作树副本。


为什么不看工作树：Windows 上 git 的 core.autocrlf 默认 true，检出时会把仓库里的 LF
翻成 CRLF。于是「看工作树」永远看到 CRLF、什么也校验不到——GitHub Actions 的
windows runner 正是这个默认值，放在 CI 里等于空转。

为什么不复用 git ls-files --eol：它会把行尾异常的文件标成 -text（例如每行都是
CR+CRLF 的那种），而照着这个标签跳过就等于放过它们。本仓正好有两个这样的 .py，
一直以 CRLF 存在 git 里没人发现。所以这里直接 git cat-file 读 blob 字节自己判。

本仓约定（现状即如此，不打算改）：文本文件以 **LF** 存入 git，Windows 上检出为
CRLF，转换交给 autocrlf。所以提交 crlf 是要拦的——它会让别人在 autocrlf=true 的
机器上拿到 CRLF、在 autocrlf=false 的机器上拿到 crlf，两边不一致。

每个提交进 git 的文本文件查三件事：
  1. 能按 UTF-8 解码；
  2. 不以 BOM 开头（.ps1 例外，见下）；
  3. 换行全是 LF——既不许 CRLF，也不许裸 CR（老 Mac 行尾）。

未跟踪的新文件只查 BOM 与编码：autocrlf 会在 git add 时把 CRLF 归一成 LF，所以
它们的行尾由 git 负责，而 BOM 不会被动。

.ps1 豁免 BOM：PowerShell 5.1 读不带 BOM 的 UTF-8 脚本会按 ANSI 代码页解中文
引号、把语法解坏，本仓的 .ps1 一律带 BOM。
.qoder 是工具生成的 wiki、tools/upx-* 是 UPX 上游自带目录，不纳入检查。

只读、不发网络请求；退出码 0 = 干净，1 = 有问题（逐条列出文件与原因）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BOM = chr(0xFEFF)

BINARY_EXTS = {".ico", ".png", ".exe", ".zip", ".dll", ".pyd", ".lnk", ".pyc"}
SKIP_DIRS = {
    ".git", ".pytest_cache", "__pycache__", "build", "dist", ".mypy_cache",
    ".ruff_cache", "node_modules",
    # 工具生成的 wiki，行尾由生成方决定，不该由本仓库的规范去改写
    ".qoder",
}
# tools/upx-4.2.4-win64 是 UPX 上游自带的目录，行尾跟着上游走。
VENDORED_DIRS = (os.path.join("tools", "upx-4.2.4-win64"),)

# PowerShell 5.1 需要 BOM 才能正确读中文，所以本仓库的 .ps1 一律带 BOM
BOM_ALLOWED_EXTS = {".ps1"}


def _git_blob(rel):
    """取某个跟踪文件的 blob 字节；取不到返回 None。"""
    r = subprocess.run(["git", "cat-file", "blob", ":" + rel], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.stdout if r.returncode == 0 else None


def _tracked_text_files():
    """所有被跟踪、且扩展名不算二进制的文件（相对路径，正斜杠）。"""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         check=True)
    for rel in out.stdout.decode("utf-8", "replace").split("\0"):
        if rel and os.path.splitext(rel)[1].lower() not in BINARY_EXTS:
            yield rel.replace(os.sep, "/")


def _check_blob(rel, blob, problems):
    """按提交内容查一个文件：UTF-8、BOM、行尾。"""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as e:
        problems.append("%s: 提交内容不是 UTF-8（%s）" % (rel, e))
        return
    if text.startswith(BOM) and \
            os.path.splitext(rel)[1].lower() not in BOM_ALLOWED_EXTS:
        problems.append("%s: 提交内容带 UTF-8 BOM" % rel)
    crlf = blob.count(b"\r\n")
    if crlf:
        problems.append("%s: 提交内容有 %d 个 CRLF 行尾，本仓约定 LF" % (rel, crlf))
    bare_cr = blob.count(b"\r") - crlf
    if bare_cr:
        problems.append("%s: 提交内容有 %d 个裸 CR（既不是 LF 也不是 CRLF）"
                        % (rel, bare_cr))


def _iter_worktree_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, ROOT)
        if rel_dir != "." and any(rel_dir == v or rel_dir.startswith(v + os.sep)
                                  for v in VENDORED_DIRS):
            continue
        for name in filenames:
            if os.path.splitext(name)[1].lower() in BINARY_EXTS:
                continue
            yield os.path.join(dirpath, name)


def main():
    problems = []
    tracked = None
    try:
        tracked = list(_tracked_text_files())
    except (OSError, subprocess.CalledProcessError):
        print("提示：拿不到 git 内容（不在 git 检出里？），本次只查工作树副本")

    if tracked is not None:
        for rel in tracked:
            blob = _git_blob(rel)
            if blob is None:
                problems.append("%s: 读不到提交内容" % rel)
                continue
            _check_blob(rel, blob, problems)

    # 未跟踪的新文件：行尾交给 autocrlf，只查 BOM 与编码
    seen = set(tracked or ())
    for path in _iter_worktree_files():
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        if rel in seen:
            continue
        with open(path, "rb") as f:
            raw = f.read()
        if not raw or not raw.strip(b"\x00"):
            continue  # 空文件（比如占位 .gitkeep）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            problems.append("%s: 不是 UTF-8（%s）" % (rel, e))
            continue
        if text.startswith(BOM) and \
                os.path.splitext(path)[1].lower() not in BOM_ALLOWED_EXTS:
            problems.append("%s: 带 UTF-8 BOM" % rel)

    if problems:
        for p in problems:
            print("文件卫生: " + p)
        print("共 %d 个文件不合规" % len(problems))
        return 1
    print("文件卫生: 提交内容 LF 行尾、UTF-8 无 BOM（.ps1 除外）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
