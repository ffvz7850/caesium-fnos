#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.7.5 包级验证（x86 + arm64）：验"打进包里的那份东西"，不重新打包。

检查项：
  * 包内 server.py 与工作树源码是否同源（sha256）；
  * manifest：version=1.7.5、platform、checksum == md5(app.tgz)（与官方约定一致）；
  * 顶层必须有 LICENSE（v1.7.1 交付时丢过）；
  * app.tgz 内逐个文件与基线对比：x86 只应有 server.py 变化；arm64 引擎必须与 1.7.1 逐字节一致；
  * 用**包内那份 server.py**起服务做冒烟：/api/status 报 1.7.5、/api/scan 立刻返回 scan_id。

用法：python qa_package_172.py
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
ART = os.path.join(HERE, "artifacts")
FPK_X86 = os.path.join(ART, "final_x86.fpk")          # 第 2 轮：验最终交付包
FPK_ARM = os.path.join(ART, "final_arm64.fpk")        # 第 2 轮：验最终交付包
OLD_X86 = os.path.join(ROOT, "_dl", "caesium_1.7.0_x86_64.fpk")          # 官方 1.7.0 基线
OLD_ARM = os.path.join(ROOT, "dist", "caesium_1.7.1_arm64.fpk")           # 1.7.1 arm 基线
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


def unpack(fpk, dest):
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fpk, "r:gz") as tf:
        tf.extractall(dest)
    return dest


def unpack_app(pkgdir, dest):
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(os.path.join(pkgdir, "app.tgz"), "r:gz") as tf:
        tf.extractall(dest)
    return dest


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


def check_one(fpk, platform, baseline, cid_prefix):
    tag = platform
    tmp = tempfile.mkdtemp(prefix="caesium_pkg_%s_" % tag)
    try:
        pkg = unpack(fpk, os.path.join(tmp, "pkg"))
        app = unpack_app(pkg, os.path.join(tmp, "app"))
        print("\n  == %s ==\n     fpk=%s\n     sha256=%s size=%d" % (tag, os.path.basename(fpk), sha(fpk), os.path.getsize(fpk)))
        with open(os.path.join(pkg, "manifest"), "r", encoding="utf-8") as f:
            man = f.read()
        fields = dict(re.findall(r"^(\w+)\s*=\s*(.*)$", man, re.M))
        row(cid_prefix + "1", "[%s] manifest 关键字段" % tag,
            "version=1.7.5 / platform=%s / checksum=md5(app.tgz)" % platform,
            "version=%s platform=%s checksum=%s md5=%s" %
            (fields.get("version"), fields.get("platform"), fields.get("checksum"),
             md5(os.path.join(pkg, "app.tgz"))),
            fields.get("version") == "1.7.5" and fields.get("platform") == platform
            and fields.get("checksum") == md5(os.path.join(pkg, "app.tgz")))

        pk_server = os.path.join(app, "server.py")
        row(cid_prefix + "2", "[%s] 包内 server.py 与工作树源码同源" % tag,
            "sha256 完全一致",
            "包内=%s 源码=%s" % (sha(pk_server)[:16], sha(SRC_SERVER)[:16]),
            sha(pk_server) == sha(SRC_SERVER))

        tops = sorted(os.path.relpath(os.path.join(b, n), pkg).replace("\\", "/")
                      for b, _d, fs in os.walk(pkg) for n in fs)
        row(cid_prefix + "3", "[%s] fpk 顶层必须带 LICENSE（v1.7.1 丢过）" % tag,
            "LICENSE 在顶层",
            "顶层=%s" % [t for t in tops if "/" not in t],
            "LICENSE" in tops)

        with open(pk_server, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
        musts = ["scan_id", "api/scan/status", "SCAN_MAX_FILES", "jsonSafe", "取消扫描", "正在扫描"]
        miss = [m for m in musts if m not in body]
        row(cid_prefix + "4", "[%s] 扫描异步化代码/文案真的在包里" % tag,
            "全部命中 %s" % musts, "缺失=%s" % (miss or "无"), not miss)

        if os.path.isfile(baseline):
            bdir = unpack(baseline, os.path.join(tmp, "base"))
            bapp = unpack_app(bdir, os.path.join(tmp, "base_app"))
            a = tree_hashes(bapp)
            b = tree_hashes(app)
            only_a = sorted(set(a) - set(b))
            only_b = sorted(set(b) - set(a))
            changed = sorted(k for k in a if k in b and a[k] != b[k])
            eng_same = a.get("caesiumclt") == b.get("caesiumclt")
            row(cid_prefix + "5", "[%s] app.tgz 内文件对比基线（%s）" % (tag, os.path.basename(baseline)),
                "只有 server.py 变；引擎 caesiumclt 逐字节一致；文件清单不变",
                "少=%s 多=%s 变化=%s；引擎一致=%s（sha=%s）" %
                (only_a, only_b, changed, eng_same, (b.get("caesiumclt") or "")[:16]),
                changed == ["server.py"] and not only_a and not only_b and eng_same)
        else:
            row(cid_prefix + "5", "[%s] 基线包对比" % tag, "基线存在", "缺失 %s" % baseline, False)

        # 冒烟：用包内 server.py 起服务
        port = free_port()
        work = os.path.join(tmp, "work")
        os.makedirs(work, exist_ok=True)
        env = dict(os.environ)
        env.update({"CAESIUM_CLT": FAKE, "PYTHONIOENCODING": "utf-8",
                    "QA_LOG": os.path.join(tmp, "engine.jsonl")})
        proc = subprocess.Popen([sys.executable, os.path.join(app, "server.py"),
                                 "--port", str(port), "--workdir", work],
                                cwd=app, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            ready = False
            end = time.time() + 30
            while time.time() < end:
                if proc.poll() is not None:
                    break
                try:
                    if get(port, "/api/status")[0] == 200:
                        ready = True
                        break
                except Exception:
                    time.sleep(0.15)
            st = json.loads(get(port, "/api/status")[1]) if ready else {}
            row(cid_prefix + "6", "[%s] 包内 server.py 起服务" % tag,
                "/api/status = 200 且 version=1.7.5",
                "ready=%s status=%s" % (ready, st), ready and st.get("version") == "1.7.5")
            if ready:
                src = os.path.join(tmp, "src")
                os.makedirs(src, exist_ok=True)
                for i in range(30):
                    with open(os.path.join(src, "p%02d.png" % i), "wb") as f:
                        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" +
                                (100 + i).to_bytes(4, "big") + (50).to_bytes(4, "big") +
                                b"\x08\x02\x00\x00\x00" + b"\x00" * 8 + b"IEND\xaeB`\x82")
                t0 = time.time()
                code, j = post(port, "/api/scan", {"paths": [src]})
                dt = (time.time() - t0) * 1000
                sid = j.get("scan_id")
                ok_state = None
                if sid:
                    for _ in range(200):
                        s = json.loads(get(port, "/api/scan/status?id=%s&offset=0&limit=50" % sid)[1])
                        if s.get("state") in ("done", "error"):
                            ok_state = s
                            break
                        time.sleep(0.05)
                row(cid_prefix + "7", "[%s] 包内 server.py：/api/scan 立刻返回 + 分页取结果" % tag,
                    "POST 毫秒级返回 scan_id，最终能拉到 30 个文件",
                    "POST 耗时 %.1f ms scan_id=%s 最终 total=%s state=%s" %
                    (dt, sid, (ok_state or {}).get("total"), (ok_state or {}).get("state")),
                    bool(sid) and dt < 500 and (ok_state or {}).get("total") == 30)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_readme():
    """说明文档里的 sha256 / 大小 / md5 必须与最终交付包逐项对上（不许写旧哈希）。"""
    md = os.path.join(ART, "final_readme.md")
    if not os.path.isfile(md):
        row("R1", "说明文档校验值", "文档存在", "缺失", False)
        return
    with open(md, "r", encoding="utf-8") as f:
        txt = f.read()
    checks = []
    for name, fpk in (("x86", FPK_X86), ("arm64", FPK_ARM)):
        h = hashlib.new("sha256")
        m = hashlib.new("md5")
        with open(fpk, "rb") as f:
            data = f.read()
        h.update(data); m.update(data)
        checks.append((name, h.hexdigest(), m.hexdigest(), str(len(data))))
    miss = []
    for name, sha, md5v, size in checks:
        for label, val in (("sha256", sha), ("md5", md5v), ("size", size)):
            if val not in txt and (label != "size" or ("{:,}".format(int(val)) not in txt)):
                miss.append("%s/%s=%s" % (name, label, val[:16]))
    row("R1", "说明文档 `caesium-1.7.5-说明.md` 里的校验值 vs 最终包",
        "x86/arm64 两包的 sha256、md5、大小全部逐项一致（不许沿用旧哈希）",
        "x86 sha=%s md5=%s size=%s；arm sha=%s md5=%s size=%s；未对上=%s" %
        (checks[0][1][:12], checks[0][2][:12], checks[0][3],
         checks[1][1][:12], checks[1][2][:12], checks[1][3], miss or "无"), not miss)
    # 源码包存在且可解
    zp = os.path.join(ART, "final_src.zip")
    ok = False
    detail = "缺失"
    if os.path.isfile(zp):
        import zipfile
        try:
            with zipfile.ZipFile(zp) as zf:
                names = zf.namelist()
                ok = any(n.endswith("server.py") for n in names) and any("LICENSE" in n for n in names)
                detail = "%d 项，含 server.py=%s 含 LICENSE=%s" % (
                    len(names), any(n.endswith("server.py") for n in names),
                    any("LICENSE" in n for n in names))
        except Exception as e:
            detail = "解包失败 %r" % e
    row("R2", "源码包 caesium-1.7.5-src.zip 可解且含 server.py / LICENSE",
        "可解、含源码与许可证", detail, ok)


def main():
    print("== 包级验证（不重新打包，只验交付产物本身）")
    for f in (FPK_X86, FPK_ARM):
        if not os.path.isfile(f):
            print("!! 缺少 %s" % f)
    check_one(FPK_X86, "x86", OLD_X86, "P")
    check_one(FPK_ARM, "arm", OLD_ARM, "Q")
    check_readme()

    print("\n" + "=" * 96)
    for cid, scene, verdict in ROWS:
        print("%-6s %-64s %s" % (cid, scene[:62], verdict))
    fails = [r for r in ROWS if r[2] == "FAIL"]
    print("=" * 96)
    print("合计 %d 条，PASS %d，FAIL %d" % (len(ROWS), len(ROWS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
