#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包校验：用 fnpack 打出 fpk → 解包 → 逐项核对（x86 + arm64 两个包）。

核对项：
1. manifest.version 与 caesium/manifest 一致（动态读取，不硬编码），desc 含新能力说明
2. 外层 fpk 里的文件清单与骨架一致（cmd/config/wizard/ui/icon 不多不少）
3. app.tgz 内 server.py 与源码 **字节级一致**（sha256 比对，防止打进旧文件）
4. 解包后的 server.py 里能 grep 到新增的中文文案（证明新代码真的进包了）
5. 引擎 caesiumclt 与源码一致、manifest 声明的 platform 与产物命名对应
6. 产物落到 dist/caesium_<version>_x86_64.fpk 与 dist/caesium_<version>_arm64.fpk
7. arm 包：server.py 必须与 x86 源码同步、引擎二进制不许被改动（sha256 对基线）

用法： python test_package.py
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROJECT = os.path.join(ROOT, "caesium")
PROJECT_ARM = os.path.join(ROOT, "caesium-arm")
DIST = os.path.join(ROOT, "dist")
# fnpack 在哪：优先工程环境里那个，其次 PATH
FNPACK_CANDIDATES = ["D:/PCAPP/DBAO/GONGJU/fnpack.exe", "/d/PCAPP/DBAO/GONGJU/fnpack.exe", "fnpack.exe"]
FNPACK = next((p for p in FNPACK_CANDIDATES if os.path.isfile(p)), "fnpack.exe")


def _read_manifest_version():
    """版本号以 manifest 为唯一真源（避免测试里硬编码会过期的 1.7.5）。"""
    try:
        with open(os.path.join(PROJECT, "manifest"), "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and line.split("=", 1)[0].strip() == "version":
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return "1.8.1"


VERSION = _read_manifest_version()
TARGET_NAME = "caesium_%s_x86_64.fpk" % VERSION
TARGET_ARM = "caesium_%s_arm64.fpk" % VERSION

EXPECT_OUTER = {"ICON.PNG", "ICON_256.PNG", "LICENSE", "app.tgz", "cmd", "config", "manifest", "wizard"}
NEW_STRINGS = ["已跳过", "继续压缩（已完成", "全部重压", "保留两份", "已存在文件",
               "断点续传", "CaesiumWeb/" + VERSION, "results.json",
               # v1.7.3：列表所属文件夹 + 重复提示
               "已在列表中", "未重复添加", "fdir",
               # v1.7.5：GIF 直通复制
               "已复制", "st-copy", "GIF 不压缩",
               # v1.7.2：大目录扫描异步化
               "正在扫描", "已发现", "取消扫描", "服务未响应或超时", "scan_id",
               # v1.7.5：列表分页 + 队列引用提交
               'id="pager"', "每页", "PAGE_SIZE", "use_queue", "rebuildPathIndex"]
# 官方 v1.7.0 包：用于「顶层清单必须与官方一致」的基线比对（Apache-2.0 再分发要随附 LICENSE）
OFFICIAL_FPK = os.path.join(ROOT, "_dl", "caesium_1.7.0_x86_64.fpk")
# arm 引擎二进制的基线 sha256：本轮只同步 server.py/manifest，**绝不许动引擎**
ARM_CLT_SHA256 = "6d092e173e18f1fde272f48722996774e2f45b925aa16c1e30b390833782b2dd"
ARM_CLT_SIZE = 5135536

FAILS = []


def check(cond, ok_msg, bad_msg):
    if cond:
        print("  PASS %s" % ok_msg)
    else:
        print("  FAIL %s" % bad_msg)
        FAILS.append(bad_msg)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fnpack_build(project, build_dir):
    """调 fnpack 打包，返回产物路径（失败返回 None）。"""
    shutil.rmtree(build_dir, ignore_errors=True)
    os.makedirs(build_dir)
    # 打包前清掉 __pycache__：跑过单测/直接 import 过 server.py 之后会在 app/ 下留 .pyc，
    # 一旦被 fnpack 打进 app.tgz，交付包的文件清单就和官方基线对不上了。
    for root_dir, dirs, _files in os.walk(project):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root_dir, d), ignore_errors=True)
                dirs.remove(d)
    print("\n== fnpack build --directory %s (cwd=%s) ==" % (project, build_dir))
    proc = subprocess.run([FNPACK, "build", "--directory", project], cwd=build_dir,
                          capture_output=True, text=True)
    print(proc.stdout.strip() or proc.stderr.strip())
    fpk = os.path.join(build_dir, "caesium.fpk")
    if not os.path.isfile(fpk):
        print("FAIL 打包产物未生成")
        return None
    print("  产物：%s (%d bytes)" % (fpk, os.path.getsize(fpk)))
    return fpk


def build_arm():
    """arm64 包：只同步 server.py + manifest 版本号，引擎二进制一个字都不许动。"""
    print("\n" + "=" * 70)
    print("== arm64 包（caesium-arm/）==")
    print("=" * 70)
    if not os.path.isdir(PROJECT_ARM):
        check(False, "", "找不到 arm 工程目录 %s" % PROJECT_ARM)
        return
    arm_clt = os.path.join(PROJECT_ARM, "app", "caesiumclt")
    check(os.path.isfile(arm_clt) and sha(arm_clt) == ARM_CLT_SHA256,
          "arm 引擎二进制未被改动 (%s, %d B)" % (ARM_CLT_SHA256[:16], ARM_CLT_SIZE),
          "arm 引擎二进制被改动过（sha256=%s）" % (sha(arm_clt) if os.path.isfile(arm_clt) else "缺失"))
    arm_server = os.path.join(PROJECT_ARM, "app", "server.py")
    x86_server = os.path.join(PROJECT, "app", "server.py")
    check(sha(arm_server) == sha(x86_server),
          "arm 的 server.py 与 x86 源码同步 (%s)" % sha(x86_server)[:16],
          "arm 的 server.py 与 x86 源码不一致！先同步再打包")

    fpk = fnpack_build(PROJECT_ARM, os.path.join(HERE, "build-arm"))
    if not fpk:
        return
    work = tempfile.mkdtemp(prefix="caesium_pkg_arm_")
    try:
        with tarfile.open(fpk, "r:gz") as tf:
            tf.extractall(work)
            top = set(n.rstrip("/").split("/")[0] for n in tf.getnames())
        print("\n== arm 外层清单 ==")
        print("  " + ", ".join(sorted(top)))
        check(top == EXPECT_OUTER, "arm 外层清单与骨架一致（含 LICENSE）",
              "arm 外层清单异常：%s" % sorted(top ^ EXPECT_OUTER))

        fields = {}
        for line in open(os.path.join(work, "manifest"), "r", encoding="utf-8").read().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip()
        print("\n== arm manifest ==")
        for k in ("appname", "version", "platform", "desc"):
            print("  %-10s = %s" % (k, fields.get(k, "")))
        check(fields.get("version") == VERSION, "arm manifest.version = %s" % VERSION,
              "arm manifest.version 不是 %s" % VERSION)
        check(fields.get("platform") == "arm", "arm manifest.platform = arm",
              "arm manifest.platform 不是 arm")
        check("多线程并行压缩" in fields.get("desc", ""), "arm manifest.desc 说明多线程并行压缩",
              "arm manifest.desc 未说明「多线程并行压缩」")

        app_dir = os.path.join(work, "_app")
        os.makedirs(app_dir, exist_ok=True)
        with tarfile.open(os.path.join(work, "app.tgz"), "r:gz") as tf:
            names = sorted(tf.getnames())
            tf.extractall(app_dir)
        check(names == sorted(["caesiumclt", "config", "config/privilege", "config/resource",
                               "server.py", "ui", "ui/config", "ui/images",
                               "ui/images/icon_256.png", "ui/images/icon_64.png",
                               "www", "www/trimjs-web-app.js"]),
              "arm app.tgz 内文件清单与 x86 一致（12 项）", "arm app.tgz 文件清单异常：%s" % names)
        check(sha(os.path.join(app_dir, "server.py")) == sha(x86_server),
              "arm 包内 server.py 与源码一致", "arm 包内 server.py 与源码不一致")
        check(sha(os.path.join(app_dir, "caesiumclt")) == ARM_CLT_SHA256,
              "arm 包内引擎仍是原二进制", "arm 包内引擎被换掉了")
        text = open(os.path.join(app_dir, "server.py"), "r", encoding="utf-8").read()
        for s in ("正在扫描", "取消扫描", "服务未响应或超时", 'server_version = "CaesiumWeb/%s"' % VERSION):
            check(s in text, "arm 包内找到 %-12s" % s, "arm 包内缺少 %r" % s)

        target = os.path.join(DIST, TARGET_ARM)
        shutil.copyfile(fpk, target)
        print("\n== 交付产物 ==\n  %s (%d bytes) sha256=%s" %
              (target, os.path.getsize(target), sha(target)[:32]))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    if not os.path.isfile(FNPACK):
        print("找不到 fnpack: %s" % FNPACK)
        return 2
    build_dir = os.path.join(HERE, "build")
    fpk = fnpack_build(PROJECT, build_dir)
    if not fpk:
        return 1

    work = tempfile.mkdtemp(prefix="caesium_pkg_")
    try:
        with tarfile.open(fpk, "r:gz") as tf:
            outer = sorted(tf.getnames())
            tf.extractall(work)
        print("\n== 外层清单 ==")
        print("  " + ", ".join(outer))
        top = set(n.rstrip("/").split("/")[0] for n in outer)
        check(top == EXPECT_OUTER, "外层文件清单与骨架一致", "外层文件清单异常：%s" % sorted(top ^ EXPECT_OUTER))

        # 与官方 v1.7.0 包的**顶层清单**逐项比对（QA P04：1.7.1 曾漏掉 LICENSE）
        if os.path.isfile(OFFICIAL_FPK):
            with tarfile.open(OFFICIAL_FPK, "r:gz") as tf:
                off_outer = sorted(tf.getnames())
            off_top = set(n.rstrip("/").split("/")[0] for n in off_outer)
            print("\n== 与官方 v1.7.0 包顶层清单比对 ==")
            print("  官方：%s" % sorted(off_top))
            print("  本次：%s" % sorted(top))
            check(top == off_top, "fpk 顶层清单与官方 v1.7.0 完全一致（含 LICENSE）",
                  "fpk 顶层清单与官方不一致：多=%s 少=%s" %
                  (sorted(top - off_top), sorted(off_top - top)))
            lic_pkg = os.path.join(work, "LICENSE")
            check(os.path.isfile(lic_pkg), "包内随附 LICENSE（Apache-2.0 再分发要求）",
                  "包内缺少 LICENSE")
            if os.path.isfile(lic_pkg):
                lic_src = os.path.join(ROOT, "src", "LICENSE")
                check(sha(lic_pkg) == sha(lic_src),
                      "包内 LICENSE 与官方逐字节一致 (%s, %d B)" % (sha(lic_pkg)[:16],
                                                                  os.path.getsize(lic_pkg)),
                      "包内 LICENSE 与官方不一致")
                check("Apache License" in open(lic_pkg, "r", encoding="utf-8", errors="replace").read(400),
                      "LICENSE 正文是 Apache License", "LICENSE 正文异常")
        else:
            print("\n  （跳过官方顶层清单比对：找不到 %s）" % OFFICIAL_FPK)

        manifest_path = os.path.join(work, "manifest")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = f.read()
        fields = {}
        for line in manifest.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip()
        print("\n== manifest ==")
        for k in ("appname", "version", "platform", "desc"):
            print("  %-10s = %s" % (k, fields.get(k, "")))
        check(fields.get("version") == VERSION, "manifest.version = %s" % VERSION,
              "manifest.version 不是 %s" % VERSION)
        check("多线程并行压缩" in fields.get("desc", ""), "manifest.desc 说明多线程并行压缩",
              "manifest.desc 未说明「多线程并行压缩」")
        check("断点续传" in fields.get("desc", ""), "manifest.desc 说明断点续传", "manifest.desc 未补充能力说明")
        check("跳过已存在文件" in fields.get("desc", ""), "manifest.desc 说明跳过已存在文件", "manifest.desc 缺少跳过说明")
        check(fields.get("appname") == "caesium", "appname 未变", "appname 异常")

        app_tgz = os.path.join(work, "app.tgz")
        app_dir = os.path.join(work, "_app")
        os.makedirs(app_dir, exist_ok=True)
        with tarfile.open(app_tgz, "r:gz") as tf:
            names = sorted(tf.getnames())
            tf.extractall(app_dir)
        print("\n== app.tgz 内文件 ==")
        print("  " + ", ".join(names))

        pkg_server = os.path.join(app_dir, "server.py")
        src_server = os.path.join(PROJECT, "app", "server.py")
        check(os.path.isfile(pkg_server), "包内存在 server.py", "包内缺少 server.py")
        check(sha(pkg_server) == sha(src_server),
              "包内 server.py 与源码 sha256 一致 (%s)" % sha(src_server)[:16],
              "包内 server.py 与源码不一致！")

        with open(pkg_server, "r", encoding="utf-8") as f:
            pkg_text = f.read()
        print("\n== 新增文案 grep（解包后的 server.py）==")
        for s in NEW_STRINGS:
            hit = pkg_text.count(s)
            check(hit > 0, "找到 %-14s x%d" % (s, hit), "解包后找不到新增文案 %r" % s)
        check('server_version = "CaesiumWeb/%s"' % VERSION in pkg_text,
              "server_version = CaesiumWeb/%s" % VERSION, "server_version 未更新")
        check('"version": "%s"' % VERSION in pkg_text, "/api/status 版本号 %s" % VERSION,
              "/api/status 版本号未更新")
        check("v1.6.0" not in pkg_text, "文件头旧版本号 v1.6.0 已清除", "仍残留旧的 v1.6.0 版本号")

        clt_pkg = os.path.join(app_dir, "caesiumclt")
        clt_src = os.path.join(PROJECT, "app", "caesiumclt")
        check(sha(clt_pkg) == sha(clt_src), "引擎二进制与骨架一致（未意外改动）", "引擎二进制被改动过")
        check(os.path.getsize(clt_pkg) > 1_000_000, "引擎文件大小正常", "引擎文件异常")

        # 与官方 v1.7.0 基线包逐文件比对权限（官方包里 caesiumclt 也是 0666，
        # 没有执行位 —— 安装时由 fnOS 侧处理，所以这里只校验"与基线一致"，不要求 0755）
        baseline = os.path.join(ROOT, "_buildtest", "out", "app.tgz")
        if not os.path.isfile(baseline) and os.path.isfile(OFFICIAL_FPK):
            # _buildtest 只是当时的解包副本，随时可能被清掉；官方 fpk 里的 app.tgz
            # 才是权威基线 —— 直接从这里取，避免"基线缺失就静默不校验"。
            baseline = os.path.join(work, "_official_app.tgz")
            with tarfile.open(OFFICIAL_FPK, "r:gz") as tf:
                with open(baseline, "wb") as f:
                    f.write(tf.extractfile("app.tgz").read())
        if os.path.isfile(baseline):
            with tarfile.open(app_tgz, "r:gz") as tf:
                mine = dict((m.name, m.mode) for m in tf.getmembers())
            with tarfile.open(baseline, "r:gz") as tf:
                base = dict((m.name, m.mode) for m in tf.getmembers())
            diff_name = set(mine) ^ set(base)
            diff_mode = dict((k, (oct(mine[k]), oct(base[k]))) for k in mine if k in base and mine[k] != base[k])
            print("\n== 与官方 v1.7.0 基线 app.tgz 比对 ==")
            print("  基线文件数 %d，本次 %d" % (len(base), len(mine)))
            check(not diff_name, "文件清单与官方基线完全一致", "文件清单与基线不一致：%s" % sorted(diff_name))
            check(not diff_mode, "权限与官方基线完全一致", "权限与基线不一致：%s" % diff_mode)
            print("  （注：官方基线里 caesiumclt 也是 %s，无执行位，安装期由系统处理）" % oct(base.get("caesiumclt", 0)))
        else:
            print("\n  （跳过基线比对：找不到 %s）" % baseline)

        os.makedirs(DIST, exist_ok=True)
        target = os.path.join(DIST, TARGET_NAME)
        shutil.copyfile(fpk, target)
        print("\n== 交付产物 ==\n  %s (%d bytes) sha256=%s" %
              (target, os.path.getsize(target), sha(target)[:32]))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    build_arm()

    print("\n== %s ==" % ("打包校验通过" if not FAILS else "打包校验失败 %d 项" % len(FAILS)))
    for f in FAILS:
        print("  - %s" % f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
