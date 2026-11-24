#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 dist/caesium-1.7.5-src.zip：源码工程 + 测试 + 说明（不含引擎二进制/大体积产物）。"""
import hashlib
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist", "caesium-1.7.5-src.zip")

# 源码工程（x86）：与 fpk 里的 app.tgz 同源，但不含 6MB 的引擎二进制
SRC_TREE = ["caesium"]
# 测试工程（去掉 build 产物 / 日志 / 快照 / 临时探针）
TEST_TREE = ["_test"]
SKIP_DIR = {"__pycache__", "build", "build-arm", "artifacts"}
SKIP_SUFFIX = (".log", ".pyc", ".fpk", ".zip", ".DO-NOT-INSTALL")
SKIP_NAME_PREFIX = ("_probe", "_scan_probe", "_verify_dist", "_final_report_probe", "_mutation_check")
DOC_FILES = ["dist/caesium-1.7.5-说明.md", "README.md", "docs/HANDOVER-1.7.2.md"]


def want(rel, is_dir=False):
    parts = rel.replace("\\", "/").split("/")
    if any(p in SKIP_DIR for p in parts):
        return False
    name = parts[-1]
    if name.startswith(".") or name.endswith(SKIP_SUFFIX):
        return False
    if name.startswith(SKIP_NAME_PREFIX):
        return False
    if name.startswith("last_run") or name.startswith("asdelivered"):
        return False
    if name == "caesiumclt":          # 引擎二进制不进源码包
        return False
    return True


def main():
    files = []
    for tree in SRC_TREE + TEST_TREE:
        base = os.path.join(ROOT, tree)
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR and not d.startswith(".")]
            for n in sorted(names):
                p = os.path.join(root, n)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                if want(rel):
                    files.append(rel)
    for rel in DOC_FILES:
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            files.append(rel)
    files = sorted(set(files))
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            z.write(os.path.join(ROOT, rel), rel)
    b = open(OUT, "rb").read()
    print("wrote %s" % OUT)
    print("  files  = %d" % len(files))
    print("  size   = %d B" % len(b))
    print("  sha256 = %s" % hashlib.sha256(b).hexdigest())
    print("  md5    = %s" % hashlib.md5(b).hexdigest())
    for rel in ("caesium/app/server.py", "caesium/manifest", "caesium/LICENSE",
                "dist/caesium-1.7.5-说明.md"):
        print("  contains %-34s = %s" % (rel, rel in files))


if __name__ == "__main__":
    main()
