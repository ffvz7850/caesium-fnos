#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打飞牛版 1.8.1（x86_64 + arm64 双架构）fpk。

- 源码：_review/caesium 与 _review/caesium-arm（server.py 必须字节一致）
- 引擎：从 1.8.0 已交付 fpk 解出的原生 caesiumclt（一个字节都不改）
- 产物：../dist/caesium_1.8.1_x86_64.fpk / caesium_1.8.1_arm64.fpk
"""
import hashlib
import io
import os
import shutil
import subprocess
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))          # _review
WS = os.path.dirname(HERE)                                  # 工作区根
SRC_PROJECT = os.path.join(HERE, "caesium")
ARM_PROJECT = os.path.join(HERE, "caesium-arm")
BUILD_X86 = os.path.join(HERE, "_build181_x86")
BUILD_ARM = os.path.join(HERE, "_build181_arm")
OUT = os.path.join(WS, "dist")
FNPACK = "D:/PCAPP/DBAO/GONGJU/fnpack.exe"
ENGINE_DIR = os.path.join(HERE, "_extracted_engines")
X86_CLT = os.path.join(ENGINE_DIR, "caesiumclt_x86_64")
ARM_CLT = os.path.join(ENGINE_DIR, "caesiumclt_arm64")
X86_CLT_SHA = "ebe954f796f946d152abfb179b7575b78e7b9064c7f9108bf460143e659f29d2"
ARM_CLT_SHA = "6d092e173e18f1fde272f48722996774e2f45b925aa16c1e30b390833782b2dd"
VER = "1.8.1"
NEW_DESC = ("基于 Caesium 引擎的图片压缩工具，支持 JPG/PNG/WebP/GIF；"
            "多线程并行压缩（自动用满 CPU，可调 1~16 路），大批量图片显著提速；"
            "支持断点续传（中断后接着压、已完成不重压）、跳过已存在文件、"
            "异步扫描可取消、列表分页（每页 5000）、GIF 直通复制；"
            "修复并发落盘竞态、取消可中断在途引擎、全局进程预算防 OOM")


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    with open(path, "rb") as f:
        return sha_bytes(f.read())


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


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


def verify_fpk(fpk, arch_name, expect_clt_sha, expect_srv_sha, expect_platform):
    """解包核对：server.py 与源码逐字节相同、引擎未改、manifest 版本/架构正确。"""
    with tarfile.open(fpk, "r:gz") as t:
        outer = sorted(t.getnames())
        appdata = t.extractfile("app.tgz").read()
        manifest_data = t.extractfile("manifest").read().decode("utf-8", "replace").replace("\r", "")
    assert "app.tgz" in outer and "manifest" in outer, outer
    with tarfile.open(fileobj=io.BytesIO(appdata), mode="r:gz") as t:
        names = sorted(t.getnames())
        srv = t.extractfile("server.py").read()
        clt = t.extractfile("caesiumclt").read()
    got_srv = sha_bytes(srv)
    got_clt = sha_bytes(clt)
    assert got_srv == expect_srv_sha, "包内 server.py 与源码不一致：%s != %s" % (got_srv, expect_srv_sha)
    assert got_clt == expect_clt_sha, "引擎二进制被改动：%s" % got_clt
    text = srv.decode("utf-8")
    for marker in ("FLUSH_LOCK", "RESULTS_GENERATION", "cancel_task_processes",
                   "PROCESS_BUDGET", "def resolve_workers("):
        assert marker in text, "包内 server.py 缺少修复标记 " + marker
    ver = [l.strip() for l in manifest_data.split("\n") if l.strip().startswith("version")]
    plat = [l.strip() for l in manifest_data.split("\n") if l.strip().startswith("platform")]
    assert ver and ("= %s" % VER) in ver[0], ver
    assert plat and ("= %s" % expect_platform) in plat[0], plat
    print("  %s: %d B, 外层 %d 项, app.tgz %d 项" %
          (arch_name, os.path.getsize(fpk), len(outer), len(names)))
    print("    server.py sha256 = %s （与源码一致）" % got_srv)
    print("    engine    sha256 = %s （未改动）" % got_clt)
    print("    %s / %s" % (ver[0], plat[0]))
    return os.path.getsize(fpk)


def main():
    assert os.path.isfile(FNPACK), FNPACK
    assert sha(X86_CLT) == X86_CLT_SHA, "x86 引擎 sha256 不符"
    assert sha(ARM_CLT) == ARM_CLT_SHA, "arm 引擎 sha256 不符"
    server_src = os.path.join(SRC_PROJECT, "app", "server.py")
    srv_sha = sha(server_src)
    assert sha(os.path.join(ARM_PROJECT, "app", "server.py")) == srv_sha, "两份 server.py 不一致"
    print("server.py sha256 =", srv_sha)
    os.makedirs(OUT, exist_ok=True)

    # ---- x86_64 ----
    print("\n== x86_64 ==")
    x86_dst = os.path.join(SRC_PROJECT, "app", "caesiumclt")
    shutil.copy2(X86_CLT, x86_dst)
    assert sha(x86_dst) == X86_CLT_SHA
    set_manifest(SRC_PROJECT, "x86")
    x86_fpk = fnpack_build(SRC_PROJECT, BUILD_X86)
    x86_out = os.path.join(OUT, "caesium_%s_x86_64.fpk" % VER)
    shutil.copy2(x86_fpk, x86_out)
    x86_size = verify_fpk(x86_out, "x86_64", X86_CLT_SHA, srv_sha, "x86")

    # ---- arm64 ----
    print("\n== arm64 ==")
    arm_dst = os.path.join(ARM_PROJECT, "app", "caesiumclt")
    shutil.copy2(ARM_CLT, arm_dst)
    assert sha(arm_dst) == ARM_CLT_SHA
    shutil.copy2(server_src, os.path.join(ARM_PROJECT, "app", "server.py"))
    assert sha(os.path.join(ARM_PROJECT, "app", "server.py")) == srv_sha
    set_manifest(ARM_PROJECT, "arm")
    arm_fpk = fnpack_build(ARM_PROJECT, BUILD_ARM)
    arm_out = os.path.join(OUT, "caesium_%s_arm64.fpk" % VER)
    shutil.copy2(arm_fpk, arm_out)
    arm_size = verify_fpk(arm_out, "arm64", ARM_CLT_SHA, srv_sha, "arm")

    # ---- md5 ----
    print("\n== MD5 ==")
    for p in (x86_out, arm_out):
        print("MD5 %s = %s" % (os.path.basename(p), md5(p)))
    print("\nDONE x86=%d B arm=%d B" % (x86_size, arm_size))


if __name__ == "__main__":
    main()
