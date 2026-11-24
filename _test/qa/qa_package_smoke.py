#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""包级验证：从 dist/*.fpk 解出 app.tgz，用**包内那份 server.py** 跑冒烟。

不做任何打包动作（不调用 fnpack），只验证"打进包里的东西"本身：
  1. 包内 server.py 与工作树源码 sha256 一致；
  2. 用包内 server.py 起服务，/api/status 报 1.7.5；
  3. 用包内 server.py 真压一个文件（skip 策略二次点击 → skipped，不 spawn 引擎）；
  4. 新增中文文案、新前端 id 确实在包里；
  5. 与官方 v1.7.0 包对比：仅 server.py 内容变化，caesiumclt 逐字节一致，顶层清单差异如实报告。

用法：python qa_package_smoke.py [fpk 路径]
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_SERVER = os.path.join(ROOT, "caesium", "app", "server.py")
DEFAULT_FPK = os.path.join(ROOT, "dist", "caesium_1.7.5_x86_64.fpk")
OFFICIAL = os.path.join(ROOT, "_dl", "caesium_1.7.0_x86_64.fpk")
FAKE = os.path.join(HERE, "qa_fake.cmd")

ROWS = []


def row(cid, scene, expect, actual, ok):
    ROWS.append((cid, scene, "PASS" if ok else "FAIL"))
    print("  [%s] %-7s %s\n         期望: %s\n         实际: %s" %
          ("PASS" if ok else "FAIL", cid, scene, expect, actual))


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def unpack_fpk(fpk, dest):
    with tarfile.open(fpk, "r:gz") as tf:
        names = tf.getnames()
        tf.extractall(dest)
    return names


def tree_hashes(d):
    out = {}
    for base, _dirs, files in os.walk(d):
        for n in files:
            p = os.path.join(base, n)
            out[os.path.relpath(p, d).replace("\\", "/")] = sha(p)
    return out


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(port, path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(port, path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, json.loads(r.read().decode())


def main():
    fpk = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FPK
    tmp = tempfile.mkdtemp(prefix="caesium_pkg_")
    print("== fpk=%s\n   sha256=%s\n   size=%d\n   临时=%s\n" % (fpk, sha(fpk), os.path.getsize(fpk), tmp))
    try:
        pkg = os.path.join(tmp, "pkg")
        os.makedirs(pkg)
        names = unpack_fpk(fpk, pkg)
        row("P01", "fpk 顶层清单",
            "含 manifest/app.tgz/cmd/config/wizard/ui?（如实列出）",
            "%s" % sorted(n.split("/")[0] for n in names), True)

        app = os.path.join(tmp, "app")
        os.makedirs(app, exist_ok=True)
        with tarfile.open(os.path.join(pkg, "app.tgz"), "r:gz") as tf:
            tf.extractall(app)
        pk_server = os.path.join(app, "server.py")
        row("P02", "包内 server.py 与工作树源码是否同源",
            "sha256 完全一致（包里就是审过的那份代码）",
            "包内=%s\n         源码=%s" % (sha(pk_server)[:16], sha(SRC_SERVER)[:16]),
            sha(pk_server) == sha(SRC_SERVER))

        with open(os.path.join(pkg, "manifest"), "r", encoding="utf-8") as f:
            man = f.read()
        fields = dict(re.findall(r"^(\w+)\s*=\s*(.*)$", man, re.M))
        row("P03", "manifest 关键字段",
            "version=1.7.5 / platform=x86 / checksum=md5(app.tgz)",
            "version=%s platform=%s checksum=%s md5(app.tgz)=%s" %
            (fields.get("version"), fields.get("platform"), fields.get("checksum"),
             md5(os.path.join(pkg, "app.tgz"))),
            fields.get("version") == "1.7.5" and fields.get("platform") == "x86"
            and fields.get("checksum") == md5(os.path.join(pkg, "app.tgz")))

        # 与官方 v1.7.0 比
        if os.path.isfile(OFFICIAL):
            off = os.path.join(tmp, "official")
            os.makedirs(off, exist_ok=True)
            unpack_fpk(OFFICIAL, off)
            off_files = sorted(os.path.relpath(os.path.join(b, n), off).replace("\\", "/")
                               for b, _d, fs in os.walk(off) for n in fs)
            new_files = sorted(os.path.relpath(os.path.join(b, n), pkg).replace("\\", "/")
                               for b, _d, fs in os.walk(pkg) for n in fs)
            lost = [x for x in off_files if x not in new_files]
            diff_names = [x for x in off_files if x in new_files
                          and sha(os.path.join(off, x)) != sha(os.path.join(pkg, x))]
            row("P04", "与官方 v1.7.0 包的顶层文件清单对比",
                "只应有 app.tgz / manifest 变化；不得丢失任何随包文件",
                "丢失=%s；内容变化=%s" % (lost, diff_names), not lost)
            off_app = os.path.join(tmp, "official_app")
            os.makedirs(off_app, exist_ok=True)
            with tarfile.open(os.path.join(off, "app.tgz"), "r:gz") as tf:
                tf.extractall(off_app)
            a_files = tree_hashes(off_app)
            b_files = tree_hashes(app)
            only_a = sorted(set(a_files) - set(b_files))
            only_b = sorted(set(b_files) - set(a_files))
            changed = sorted(k for k in a_files if k in b_files and a_files[k] != b_files[k])
            row("P05", "app.tgz 内文件逐个 sha256 对比（对比官方 v1.7.0）",
                "只有 server.py 变；caesiumclt 必须逐字节一致",
                "少=%s 多=%s 变化=%s；caesiumclt 一致=%s" %
                (only_a, only_b, changed, a_files.get("caesiumclt") == b_files.get("caesiumclt")),
                changed == ["server.py"] and not only_a and not only_b
                and a_files.get("caesiumclt") == b_files.get("caesiumclt"))

        # 新文案是否真的进包
        with open(pk_server, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
        musts = ["已跳过", "跳过", "继续压缩（已完成", "全部重压", "已存在文件", "断点续传",
                 "results.json", "exist_policy", "skipped"]
        missing = [m for m in musts if m not in body]
        row("P06", "新功能文案/符号是否真的打进包里",
            "全部命中：%s" % musts[:5],
            "缺失=%s" % missing, not missing)

        # 冒烟：用包内 server.py 起服务
        port = free_port()
        work = os.path.join(tmp, "work")
        os.makedirs(work, exist_ok=True)
        log = os.path.join(tmp, "engine.jsonl")
        env = dict(os.environ)
        env.update({"CAESIUM_CLT": FAKE, "QA_LOG": log, "PYTHONIOENCODING": "utf-8",
                    "QA_NONCE": "pkg"})
        proc = subprocess.Popen([sys.executable, os.path.join(app, "server.py"),
                                 "--port", str(port), "--workdir", work],
                                cwd=app, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            ready = False
            deadline = time.time() + 30
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    code, body_ = get(port, "/api/status")
                    if code == 200:
                        ready = True
                        break
                except Exception:
                    time.sleep(0.15)
            st = json.loads(get(port, "/api/status")[1]) if ready else {}
            row("P07", "包内 server.py 以 TCP 模式起服务",
                "/api/status 200 且 version=1.7.5",
                "ready=%s status=%s" % (ready, st), ready and st.get("version") == "1.7.5")
            if not ready:
                print(proc.stdout.read().decode("utf-8", "replace") if proc.stdout else "")
                return 1

            src_dir = os.path.join(tmp, "src")
            out_dir = os.path.join(tmp, "out")
            os.makedirs(src_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            a = os.path.join(src_dir, "pkg.jpg")
            with open(a, "wb") as f:
                f.write(b"PKGSRC" * 100)
            opts = {"mode": "quality", "jpeg_quality": 90, "png_quality": 90, "png_opt_level": 3,
                    "webp_quality": 90, "tiff_algorithm": "deflate", "chroma": "auto",
                    "progressive": True, "zopfli": False, "keep_exif": True, "suffix": "",
                    "format": "keep", "keep_dates": True, "long_edge": 0, "width": 0,
                    "height": 0, "percent": 0, "no_upscale": False, "max_size_kb": 200,
                    "exist_policy": "skip"}
            body_ = {"files": [{"path": a, "root": src_dir}], "output": out_dir,
                     "same_folder": False, "keep_structure": False, "options": opts}
            code, j = post(port, "/api/compress", body_)
            tid = j.get("task_id")
            t = {}
            deadline = time.time() + 60
            while time.time() < deadline:
                t = json.loads(get(port, "/api/tasks/" + tid)[1])
                if t["state"] in ("done", "error", "canceled"):
                    break
                time.sleep(0.2)
            outs = sorted(os.listdir(out_dir))
            code2, j2 = post(port, "/api/compress", body_)
            t2 = {}
            deadline = time.time() + 60
            while time.time() < deadline:
                t2 = json.loads(get(port, "/api/tasks/" + j2["task_id"])[1])
                if t2["state"] in ("done", "error", "canceled"):
                    break
                time.sleep(0.2)
            n_spawn = 0
            if os.path.isfile(log):
                with open(log, "r", encoding="utf-8") as f:
                    n_spawn = len([x for x in f.read().splitlines() if x.strip()])
            row("P08", "包内 server.py 真压一次 + 第二次（skip 策略）",
                "第一次 ok=1 且落盘；第二次 skipped=1、仅 spawn 1 次（不重压）",
                "第一次 state=%s ok=%s 落盘=%s；第二次 skipped=%s；累计 spawn=%d" %
                (t.get("state"), t.get("ok"), outs, t2.get("skipped"), n_spawn),
                t.get("state") == "done" and t.get("ok") == 1 and outs
                and t2.get("skipped") == 1 and n_spawn == 1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 92)
    for cid, scene, verdict in ROWS:
        print("%-6s %-58s %s" % (cid, scene[:56], verdict))
    fails = [r for r in ROWS if r[2] == "FAIL"]
    print("=" * 92)
    print("合计 %d 条，PASS %d，FAIL %d" % (len(ROWS), len(ROWS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
