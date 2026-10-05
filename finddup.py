#!/usr/bin/env python3
"""finddup - 按内容哈希找出重复文件。

设计取舍（诚实说明）：
- 只读工具：不提供 --delete，因为删除文件是不可逆的决定，
  应该由人来做。--script 会输出可审查的 rm 命令。
- 两阶段算法：先按文件大小分组，只对"同大小>=2 个文件"的组
  做 SHA-256；大小唯一的直接跳过，省掉绝大多数磁盘读取。
- 符号链接：跳过不跟随（避免循环、避免把链接目标算两遍）。
- 硬链接：同一 inode 只计一次（同一个文件有多个名字时，
  它不是重复，是同一个东西）。
"""
import argparse
import fnmatch
import hashlib
import json
import os
import sys

VERSION = "0.1.0"
CHUNK = 1024 * 1024  # 1MB


def parse_size(s):
    """解析 '1M' / '500K' / '2G' / 纯数字（字节）。"""
    s = s.strip().upper()
    mul = 1
    for suffix, m in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if s.endswith(suffix):
            mul = m
            s = s[:-1]
            break
    try:
        return int(float(s) * mul)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "无法解析大小：%r（例：1M、500K、1024）" % s)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def iter_files(root, excludes, min_size):
    """递归列出普通文件：跳过符号链接，同一 inode 只出一次。"""
    seen_inodes = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # 目录级排除
        dirnames[:] = [d for d in dirnames
                       if not any(fnmatch.fnmatch(d, p) for p in excludes)]
        for name in filenames:
            if any(fnmatch.fnmatch(name, p) for p in excludes):
                continue
            path = os.path.join(dirpath, name)
            if any(fnmatch.fnmatch(path, p) for p in excludes):
                continue
            try:
                st = os.lstat(path)
            except OSError:
                print("警告：无法读取 %s，跳过" % path, file=sys.stderr)
                continue
            if not os.path.isfile(path):  # lstat 下链接/管道/设备都不进
                continue
            if (st.st_dev, st.st_ino) in seen_inodes:  # 硬链接去重
                continue
            seen_inodes.add((st.st_dev, st.st_ino))
            if st.st_size < min_size:
                continue
            yield path, st.st_size


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def find_duplicates(root, excludes=(), min_size=1):
    by_size = {}
    scanned = 0
    for path, size in iter_files(root, excludes, min_size):
        scanned += 1
        by_size.setdefault(size, []).append(path)
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue  # 大小唯一的，不可能是重复
        by_hash = {}
        for path in paths:
            try:
                by_hash.setdefault(sha256_of(path), []).append(path)
            except OSError:
                print("警告：无法读取 %s，跳过" % path, file=sys.stderr)
        for digest, dupes in by_hash.items():
            if len(dupes) >= 2:
                dupes.sort()
                groups.append({"hash": digest, "size": size, "files": dupes})
    groups.sort(key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True)
    return groups, scanned


def sh_quote(s):
    if all(c.isalnum() or c in "@%_+=:,./-" for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="finddup",
        description="按 SHA-256 内容哈希找出重复文件（只读，不删除）。")
    ap.add_argument("directory", help="要扫描的目录")
    ap.add_argument("--min-size", type=parse_size, default=1, metavar="SIZE",
                    help="只考虑不小于该大小的文件，如 1M、500K（默认 1 字节）")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="排除模式（fnmatch），可重复，如 --exclude '*.tmp'")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--script", action="store_true",
                    help="输出 rm 命令（每组保留第一个，其余列出），供审查后执行")
    ap.add_argument("--version", action="version",
                    version="finddup %s" % VERSION)
    args = ap.parse_args(argv)

    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        print("error: 不是目录：%s" % args.directory, file=sys.stderr)
        return 2

    groups, scanned = find_duplicates(root, args.exclude, args.min_size)
    wasted = sum(g["size"] * (len(g["files"]) - 1) for g in groups)

    if args.json:
        print(json.dumps({"scanned": scanned, "groups": groups,
                          "wasted_bytes": wasted},
                         ensure_ascii=False, indent=2))
        return 0

    if args.script:
        for g in groups:
            for path in g["files"][1:]:
                print("rm %s" % sh_quote(path))
        return 0

    if not groups:
        print("扫描 %d 个文件，没有发现重复。" % scanned)
        return 0

    print("===== 重复文件（扫描 %d 个文件）=====\n" % scanned)
    for i, g in enumerate(groups, 1):
        w = g["size"] * (len(g["files"]) - 1)
        print("【组 %d】%d 个相同文件，每个 %s，可回收约 %s"
              % (i, len(g["files"]), human(g["size"]), human(w)))
        for path in g["files"]:
            print("  %s" % path)
        print()
    print("共 %d 组重复，总计可回收约 %s。" % (len(groups), human(wasted)))
    print("（本工具不删除文件；用 --script 生成 rm 命令审查后执行。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
