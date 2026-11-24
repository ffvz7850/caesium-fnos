#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 dist/caesium-1.8.1-src.zip：_review 完整工程（不含引擎二进制 / fpk / 构建产物）。"""
import hashlib
import os
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
OUT = os.path.join(WS, "dist", "caesium-1.8.1-src.zip")

SRC_TREES = ["caesium", "caesium-arm"]
TEST_TREES = ["_test"]
SKIP_DIR = {"__pycache__", "build", "build-x86", "build-arm",
            "_build181_x86", "_build181_arm", "_extracted_engines", "artifacts",
            "node_modules"}     # QA 前端测试的第三方依赖不进源码包（package.json 保留）
SKIP_SUFFIX = (".log", ".pyc", ".fpk", ".zip", ".DO-NOT-INSTALL")
SKIP_NAME_PREFIX = ("_probe", "_scan_probe", "_verify_dist", "_final_report_probe", "_mutation_check")
DOC_FILES = ["说明.md", "docs/HANDOVER-1.7.2.md", "build_181_pkg.py",
             "_verify_181.py", "_make_src_zip_181.py",
             "dist/1.8.1 修复说明.md"]
# dist 在 _review 的上一级；src zip 里仍用 dist/... 这个逻辑路径
DOC_SOURCE = {"dist/1.8.1 修复说明.md": os.path.join(WS, "dist", "1.8.1 修复说明.md")}


def want(rel):
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
    for tree in SRC_TREES + TEST_TREES:
        base = os.path.join(HERE, tree)
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR and not d.startswith(".")]
            for n in sorted(names):
                p = os.path.join(root, n)
                rel = os.path.relpath(p, HERE).replace("\\", "/")
                if want(rel):
                    files.append(rel)
    for rel in DOC_FILES:
        if os.path.isfile(DOC_SOURCE.get(rel, os.path.join(HERE, rel))):
            files.append(rel)
    files = sorted(set(files))

    def zipinfo(arcname, mode, mtime, is_dir=False):
        """显式构造 ZipInfo：Windows 上 os.stat 无执行位，必须手动写入 Unix 模式位。"""
        zi = zipfile.ZipInfo(arcname, date_time=time.localtime(mtime)[:6])
        ftype = 0o040000 if is_dir else 0o100000
        zi.external_attr = (ftype | mode) << 16
        if is_dir:
            zi.external_attr |= 0x10          # MS-DOS 目录标志
        zi.compress_type = zipfile.ZIP_DEFLATED
        return zi

    # 目录条目（与 1.8.0 源码包一致：目录 755）
    dirs = set()
    for rel in files:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for d in sorted(dirs):
            z.writestr(zipinfo(d + "/", 0o755, time.time(), is_dir=True), b"")
        for rel in files:
            src = DOC_SOURCE.get(rel, os.path.join(HERE, rel))
            with open(src, "rb") as f:
                data = f.read()
            mode = 0o755 if ("/cmd/" in rel or rel.startswith("cmd/")) else 0o644
            z.writestr(zipinfo(rel, mode, os.path.getmtime(src)), data)
    b = open(OUT, "rb").read()
    print("wrote %s" % OUT)
    print("  files  = %d" % len(files))
    print("  size   = %d B" % len(b))
    print("  sha256 = %s" % hashlib.sha256(b).hexdigest())
    print("  md5    = %s" % hashlib.md5(b).hexdigest())
    for rel in ("caesium/app/server.py", "caesium-arm/app/server.py",
                "caesium/manifest", "caesium-arm/manifest",
                "dist/1.8.1 修复说明.md"):
        print("  contains %-32s = %s" % (rel, rel in files))
    print("  contains caesiumclt = %s" % any(n.endswith("/caesiumclt") for n in files))


if __name__ == "__main__":
    main()
