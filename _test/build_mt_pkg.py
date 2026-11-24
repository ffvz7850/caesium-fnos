#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打飞牛多线程版 v1.8.0（x86 + arm64 双架构）。

基于 _mt175/caesium（v1.7.5 工程 + 多线程 server.py），注入 x86 引擎；
arm 用现有 caesium-arm 工程（引擎二进制一个字不动），只同步 server.py + 版本号。
"""
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # _mt175
WS = os.path.dirname(ROOT)                        # 工作区根
SRC_PROJECT = os.path.join(ROOT, "caesium")       # 改好的 x86 工程
ARM_PROJECT = os.path.join(WS, "caesium-arm")     # arm 工程
BUILD_X86 = os.path.join(HERE, "build-x86")
BUILD_ARM = os.path.join(HERE, "build-arm")
OUT = os.path.join(WS, "dist")
FNPACK = "D:/PCAPP/DBAO/GONGJU/fnpack.exe"
X86_CLT = os.path.join(WS, "caesium", "app", "caesiumclt")
ARM_CLT_SHA = "6d092e173e18f1fde272f48722996774e2f45b925aa16c1e30b390833782b2dd"
VER = "1.8.0"
NEW_DESC = ("基于 Caesium 引擎的图片压缩工具，支持 JPG/PNG/WebP/GIF；"
            "多线程并行压缩（自动用满 CPU，可调 1~16 路），大批量图片显著提速；"
            "支持断点续传（中断后接着压、已完成不重压）、跳过已存在文件、"
            "异步扫描可取消、列表分页（每页 5000）、GIF 直通复制")


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def clean_pyc(project):
    for root, dirs, _ in os.walk(project):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)


def set_manifest(project, platform):
    p = os.path.join(project, "manifest")
    raw = open(p, "rb").read().decode("utf-8").replace("\r\n", "\n")
    lines = []
    for ln in raw.split("\n"):
        key = ln.split("=", 1)[0].strip()
        if key == "version":
            lines.append("version               = %s" % VER)
        elif key == "platform":
            lines.append("platform              = %s" % platform)
        elif key == "desc":
            lines.append("desc                  = %s" % NEW_DESC)
        elif key == "checksum":
            continue                      # fnpack 会重算
        else:
            lines.append(ln)
    open(p, "w", encoding="utf-8", newline="\n").write("\n".join(lines))


def fnpack_build(project, build_dir):
    shutil.rmtree(build_dir, ignore_errors=True)
    os.makedirs(build_dir)
    clean_pyc(project)
    proc = subprocess.run([FNPACK, "build", "--directory", project], cwd=build_dir,
                          capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    print("  fnpack: %s" % out.strip()[-300:])
    fpk = os.path.join(build_dir, "caesium.fpk")
    if not os.path.isfile(fpk):
        raise SystemExit("打包失败：未生成 " + fpk)
    return fpk


def verify_fpk(fpk, arch_name, expect_clt_sha):
    import io
    import tarfile
    with tarfile.open(fpk, "r:gz") as t:
        outer = sorted(t.getnames())
        appdata = t.extractfile("app.tgz").read()
        manifest = t.extractfile("manifest").read().decode("utf-8", "replace").replace("\r", "")
    assert "app.tgz" in outer and "manifest" in outer, outer
    with tarfile.open(fileobj=io.BytesIO(appdata), mode="r:gz") as t:
        names = sorted(t.getnames())
        srv = t.extractfile("server.py").read()
        clt = t.extractfile("caesiumclt").read()
    assert "def resolve_workers(" in srv.decode("utf-8"), "包内 server.py 没有多线程代码！"
    assert '"workers": t.get("workers", 1)' in srv.decode("utf-8"), "包内任务快照没有 workers 字段！"
    assert hashlib.sha256(clt).hexdigest() == expect_clt_sha, "引擎二进制被改动"
    ver = [l for l in manifest.split("\n") if l.strip().startswith("version")]
    plat = [l for l in manifest.split("\n") if l.strip().startswith("platform")]
    assert ver and VER in ver[0], ver
    print("  %s: %d B, 外层 %d 项, app.tgz %d 项, engine sha %s..., %s %s" % (
        arch_name, os.path.getsize(fpk), len(outer), len(names),
        hashlib.sha256(clt).hexdigest()[:12], ver[0].strip(), plat[0].strip()))
    return os.path.getsize(fpk)


def main():
    assert os.path.isfile(FNPACK), FNPACK
    assert os.path.isfile(X86_CLT), X86_CLT
    server_src = os.path.join(SRC_PROJECT, "app", "server.py")
    assert os.path.isfile(server_src)
    srv_sha = sha(server_src)
    print("server.py sha256 =", srv_sha)

    # ---- x86 ----
    print("\n== x86_64 ==")
    x86_clt_dst = os.path.join(SRC_PROJECT, "app", "caesiumclt")
    shutil.copy2(X86_CLT, x86_clt_dst)
    set_manifest(SRC_PROJECT, "x86")
    x86_fpk = fnpack_build(SRC_PROJECT, BUILD_X86)
    x86_out = os.path.join(OUT, "caesium_%s_x86_64.fpk" % VER)
    shutil.copy2(x86_fpk, x86_out)
    x86_size = verify_fpk(x86_out, "x86_64", sha(X86_CLT))

    # ---- arm64 ----
    print("\n== arm64 ==")
    arm_clt = os.path.join(ARM_PROJECT, "app", "caesiumclt")
    assert sha(arm_clt) == ARM_CLT_SHA, "arm 引擎被改动，拒绝打包"
    shutil.copy2(server_src, os.path.join(ARM_PROJECT, "app", "server.py"))
    assert sha(os.path.join(ARM_PROJECT, "app", "server.py")) == srv_sha
    set_manifest(ARM_PROJECT, "arm")
    arm_fpk = fnpack_build(ARM_PROJECT, BUILD_ARM)
    arm_out = os.path.join(OUT, "caesium_%s_arm64.fpk" % VER)
    shutil.copy2(arm_fpk, arm_out)
    arm_size = verify_fpk(arm_out, "arm64", ARM_CLT_SHA)

    # ---- md5 ----
    for p in (x86_out, arm_out):
        print("MD5 %s = %s" % (os.path.basename(p), hashlib.md5(open(p, "rb").read()).hexdigest()))
    print("\nDONE x86=%d B arm=%d B" % (x86_size, arm_size))


if __name__ == "__main__":
    main()
