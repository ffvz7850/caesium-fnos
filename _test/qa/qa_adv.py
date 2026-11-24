#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA 独立对抗用例（不复用工程师的用例，只复用 harness 的断言工具）。

用法：
    python qa_adv.py            # 跑全部（约 60-90 秒）
    python qa_adv.py C06 C07    # 只跑指定编号

特点：
 * 服务端每次以全新 workdir 启动（干净状态），CAESIUM_CLT 指向 qa/qa_fake.cmd；
 * 假引擎可进入 partial / hang / chunk 模式，用来制造"半截文件""真 kill""并发写"；
 * 所有临时目录都在系统临时目录下，绝不碰真实 NAS 路径。
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(TEST_DIR)
APP = os.path.join(ROOT, "caesium", "app")
SERVER_PY = os.path.join(APP, "server.py")
FAKE_CLT = os.path.join(HERE, "qa_fake.cmd")

sys.path.insert(0, TEST_DIR)
import harness as H  # noqa: E402

BASE_OPTS = {"mode": "quality", "lossless": False, "jpeg_quality": 90, "png_quality": 90,
             "png_opt_level": 3, "webp_quality": 90, "tiff_algorithm": "deflate", "chroma": "auto",
             "progressive": True, "zopfli": False, "keep_exif": True, "suffix": "", "format": "keep",
             "keep_dates": True, "long_edge": 0, "width": 0, "height": 0, "percent": 0,
             "no_upscale": False, "max_size_kb": 200, "exist_policy": "skip"}

STATE = {"proc": None, "root": None, "work": None, "log": None, "port": 0, "env": {}}
ROWS = []          # (编号, 场景, 期望, 实际, PASS/FAIL)


def row(cid, scene, expect, actual, ok):
    ROWS.append((cid, scene, expect, actual, "PASS" if ok else "FAIL"))
    print("  [%s] %-9s %s\n         期望: %s\n         实际: %s" %
          ("PASS" if ok else "FAIL", cid, scene, expect, actual))


# ------------------------------------------------------------------ 基础设施
def url(path):
    return "http://127.0.0.1:%d%s" % (STATE["port"], path)


def http_get(path):
    try:
        with urllib.request.urlopen(url(path), timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_json(path):
    code, body = http_get(path)
    try:
        return code, json.loads(body.decode("utf-8"))
    except Exception:
        return code, {"raw": body[:200]}


def http_post_raw(path, raw_bytes):
    req = urllib.request.Request(url(path), data=raw_bytes,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]
    except Exception as e:
        return -1, ("CONNECTION-ERROR: %s" % e).encode("utf-8")


def http_post(path, payload):
    return http_post_raw(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def http_post_json(path, payload):
    code, body = http_post(path, payload)
    try:
        return code, json.loads(body.decode("utf-8")) if isinstance(body, bytes) else body
    except Exception:
        return code, {"raw": str(body)[:200]}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_server(extra_env=None):
    env = dict(os.environ)
    env.pop("CAESIUM_ACCESSIBLE_PATHS", None)
    env["CAESIUM_CLT"] = FAKE_CLT
    env["QA_LOG"] = STATE["log"]
    env["PYTHONIOENCODING"] = "utf-8"
    env["QA_NONCE"] = "n%d" % int(time.time() * 1000 % 1000000)
    env.update(extra_env or {})
    STATE["env"] = extra_env or {}
    proc = subprocess.Popen([sys.executable, SERVER_PY, "--port", str(STATE["port"]),
                             "--workdir", STATE["work"]],
                            cwd=APP, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    STATE["proc"] = proc
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            raise RuntimeError("server 提前退出 rc=%s\n%s" % (proc.returncode, out))
        try:
            code, _ = http_json("/api/status")
            if code == 200:
                return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError("server 启动超时")


def stop_server(hard=False):
    proc = STATE["proc"]
    if not proc:
        return
    if proc.poll() is None:
        if hard:
            # 真·闪退：连同子进程一起强杀（不走任何清理逻辑）
            subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
    STATE["proc"] = None


def restart(extra_env=None):
    stop_server()
    start_server(extra_env)


def engine_calls():
    p = STATE["log"]
    if not os.path.isfile(p):
        return []
    with open(p, "r", encoding="utf-8") as f:
        return [json.loads(x) for x in f.read().splitlines() if x.strip()]


def clear_engine_calls():
    try:
        os.unlink(STATE["log"])
    except OSError:
        pass


def wait_task(tid, timeout=120):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        code, j = http_json("/api/tasks/" + tid)
        if code != 200:
            raise AssertionError("查任务失败 %s %s" % (code, j))
        last = j
        if j["state"] in ("done", "canceled", "error"):
            return j
        time.sleep(0.2)
    raise AssertionError("任务超时 %s" % last)


def start_compress(files, out_dir, opts=None, keep=False, force=False, same_folder=False):
    payload = {"files": files, "output": "" if same_folder else out_dir,
               "same_folder": same_folder, "keep_structure": keep,
               "force_recompress": force, "options": dict(opts or BASE_OPTS)}
    code, j = http_post_json("/api/compress", payload)
    if code != 200 or "task_id" not in j:
        raise AssertionError("建任务失败 %s %s" % (code, j))
    return j["task_id"]


def compress(files, out_dir, opts=None, keep=False, force=False, same_folder=False):
    return wait_task(start_compress(files, out_dir, opts, keep, force, same_folder))


def results():
    code, j = http_json("/api/results")
    return j.get("records", {}) if code == 200 else {}


def clear_results():
    http_post("/api/results/clear", {})


def workdir(name):
    d = os.path.join(STATE["root"], name)
    os.makedirs(d, exist_ok=True)
    return d


def make_src(dirpath, name="a.jpg", data=b"ORIGINAL-IMAGE-BYTES"):
    p = os.path.join(dirpath, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def rb(p):
    with open(p, "rb") as f:
        return f.read()


def items(paths, root):
    return [{"path": p, "root": root} for p in paths]


# ================================================================== 用例
def c01_foreign_file_not_clobbered():
    """skip 语义边界：输出目录里是"用户自己的"同名文件。"""
    src, out = workdir("c01_src"), workdir("c01_out")
    a = make_src(src)
    foreign = os.path.join(out, "a.jpg")
    with open(foreign, "wb") as f:
        f.write(b"USER-FOREIGN-FILE")
    clear_engine_calls()
    t1 = compress(items([a], src), out, dict(BASE_OPTS, exist_policy="skip"))
    rec1 = results().get(a)
    ok1 = (t1["skipped"] == 1 and len(engine_calls()) == 0
           and rb(foreign) == b"USER-FOREIGN-FILE" and rec1 is None)
    row("C01a", "skip 命中用户文件：不 spawn / 不改文件 / 不写归属记录",
        "skipped=1, spawn=0, 内容不变, records 无此路径",
        "state=%s ok=%s skipped=%s spawn=%d 内容=%r record=%s" %
        (t1["state"], t1["ok"], t1["skipped"], len(engine_calls()), rb(foreign)[:24], rec1), ok1)

    # 紧接着改参数（jpeg_quality）再压 → 用户文件绝不能被当成"我们的旧产物"覆盖
    t2 = compress(items([a], src), out, dict(BASE_OPTS, jpeg_quality=55, exist_policy="skip"))
    ok2 = (t2["skipped"] == 1 and len(engine_calls()) == 0
           and rb(foreign) == b"USER-FOREIGN-FILE")
    row("C01b", "改 jpeg_quality 再压：用户同名文件仍不得被覆盖",
        "skipped=1, spawn=0, 内容不变",
        "ok=%s skipped=%s spawn=%d 内容=%r" %
        (t2["ok"], t2["skipped"], len(engine_calls()), rb(foreign)[:24]), ok2)


def c01c_user_jpeg_variants():
    """用户自己的 JPEG：EOI 在末尾 vs EOI 之后还有附加数据（拇指图/手机 Motion Photo）。"""
    src, out = workdir("c01c_src"), workdir("c01c_out")
    a = make_src(src, "u.jpg", data=b"U" * 3000)

    def jpeg(tail):
        return (b"\xff\xd8\xff\xe0" + b"\x00" * 600 + b"\xff\xda" + b"\x00" * 200
                + b"\xff\xd9" + tail)

    good = os.path.join(out, "u.jpg")
    with open(good, "wb") as f:
        f.write(jpeg(b""))                      # 完整图：EOI 就在末尾
    clear_engine_calls()
    t = compress(items([a], src), out, dict(BASE_OPTS, exist_policy="skip"))
    row("C01c", "用户自己的完整 JPEG（EOI 在文件末尾）",
        "skip（不 spawn、不覆盖）",
        "skipped=%s spawn=%d 内容未变=%s" %
        (t["skipped"], len(engine_calls()), rb(good) == jpeg(b"")),
        t["skipped"] == 1 and len(engine_calls()) == 0)

    out2 = workdir("c01d_out")
    b = make_src(src, "v.jpg", data=b"V" * 3000)
    trailer = os.path.join(out2, "v.jpg")
    data = jpeg(b"\x00" * 5000)                 # EOI 之后还有 5KB（手机 Motion Photo 就是这样）
    with open(trailer, "wb") as f:
        f.write(data)
    clear_engine_calls()
    t2 = compress(items([b], src), out2, dict(BASE_OPTS, exist_policy="skip"))
    untouched = rb(trailer) == data
    row("C01d", "用户自己的 JPEG，但 EOI 之后还跟着 5KB 附加数据（Motion Photo 结构）",
        "仍应 skip（这是合法完整的图，不是我们压坏的残骸）",
        "skipped=%s ok=%s spawn=%d 用户文件未被覆盖=%s" %
        (t2["skipped"], t2["ok"], len(engine_calls()), untouched),
        t2["skipped"] == 1 and untouched)


def c02_crash_residue_is_skipped():
    """输出目录里是"本应用上次被打断留下的残骸"：分"有图片魔数的真残骸"和"随便的文件"两种。"""
    src, out = workdir("c02_src"), workdir("c02_out")
    a = make_src(src, "half.jpg", data=b"X" * 20000)

    # (a) 真·截断 JPEG：有 SOI(FFD8)、没有 EOI(FFD9) —— 这就是闪退现场
    half = os.path.join(out, "half.jpg")
    with open(half, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 4000)
    clear_engine_calls()
    t = compress(items([a], src), out, dict(BASE_OPTS, exist_policy="skip"))
    kept = rb(half).startswith(b"\xff\xd8\xff\xe0")
    row("C02a", "输出目录是截断 JPEG 残骸（FFD8 开头、无 FFD9）→ 再压",
        "识别为残骸并重压（不能当'已存在'永久跳过）",
        "state=%s ok=%s skipped=%s spawn=%d 残骸仍在=%s 现在大小=%d" %
        (t["state"], t["ok"], t["skipped"], len(engine_calls()), kept, os.path.getsize(half)),
        t["ok"] == 1 and not kept)

    # (b) 没有任何图片魔数的莫名文件 → 属于"用户自己的文件"，必须继续 skip 保护
    out2 = workdir("c02b_out")
    b = make_src(src, "b.jpg", data=b"Y" * 20000)
    foreign = os.path.join(out2, "b.jpg")
    with open(foreign, "wb") as f:
        f.write(b"\x00" * 777)
    clear_engine_calls()
    t2 = compress(items([b], src), out2, dict(BASE_OPTS, exist_policy="skip"))
    same = rb(foreign) == b"\x00" * 777
    row("C02b", "输出目录同名文件没有图片魔数（用户自己的任意文件）",
        "仍按 skip 跳过、不 spawn、不覆盖（C01a 语义边界）",
        "skipped=%s spawn=%d 内容未变=%s" % (t2["skipped"], len(engine_calls()), same),
        t2["skipped"] == 1 and len(engine_calls()) == 0 and same)


def c03_skip_misjudgement():
    """已完成产物被删除 / 换成不同大小 / 换成相同大小。"""
    clear_results()
    src, out = workdir("c03_src"), workdir("c03_out")
    a = make_src(src, "d.jpg")
    t0 = compress(items([a], src), out, dict(BASE_OPTS))
    target = os.path.join(out, "d.jpg")
    size0 = os.path.getsize(target)
    H.eq(t0["ok"], 1, "首次应成功")

    os.unlink(target)                                     # (a) 被删掉
    clear_engine_calls()
    ta = compress(items([a], src), out, dict(BASE_OPTS))
    row("C03a", "已完成记录在，但输出文件被用户删了",
        "必须重压（ok=1, spawn=1）",
        "ok=%s skipped=%s spawn=%d 文件重建=%s" %
        (ta["ok"], ta["skipped"], len(engine_calls()), os.path.isfile(target)),
        ta["ok"] == 1 and len(engine_calls()) == 1)

    with open(target, "wb") as f:                          # (b) 换成不同大小
        f.write(b"TAMPERED-SMALLER")
    clear_engine_calls()
    tb = compress(items([a], src), out, dict(BASE_OPTS))
    row("C03b", "输出文件被换成不同大小的另一个文件",
        "必须重压（ok=1, spawn=1）",
        "ok=%s skipped=%s spawn=%d 大小=%d(原 %d)" %
        (tb["ok"], tb["skipped"], len(engine_calls()), os.path.getsize(target), size0),
        tb["ok"] == 1 and len(engine_calls()) == 1)

    with open(target, "wb") as f:                          # (c) 换成"相同大小"的别的文件
        f.write(b"Z" * size0)
    clear_engine_calls()
    tc = compress(items([a], src), out, dict(BASE_OPTS))
    same = rb(target).startswith(b"Z" * 16)
    row("C03c", "输出文件被换成大小相同的另一个文件（最难察觉）",
        "理想：能识别并重压；最差也要如实说明这是已知局限",
        "ok=%s skipped=%s spawn=%d 内容仍是伪造文件=%s" %
        (tc["ok"], tc["skipped"], len(engine_calls()), same),
        not (tc["skipped"] == 1 and same))


def c04_rename_five_times():
    """rename 模式连续压 5 次：产出 a.jpg / a (2) …，原文件内容不变，无临时目录残留。"""
    clear_results()
    src, out = workdir("c04_src"), workdir("c04_out")
    a = make_src(src, "a.jpg", data=b"ORIGINAL-MUST-NOT-CHANGE")
    orig = rb(a)
    seen = []
    for i in range(5):
        t = compress(items([a], src), out, dict(BASE_OPTS, exist_policy="rename"))
        seen.append((t["ok"], t["skipped"], t["results"][0].get("out")))
    names = sorted(n for n in os.listdir(out) if not n.startswith("."))
    tmp_left = [n for n in os.listdir(out) if n.startswith(".caesium-tmp")]
    row("C04", "rename 模式连压 5 次同一张图",
        "落盘 a.jpg/a (2)/a (3)/a (4)/a (5)，原图字节不变，零 .caesium-tmp 残留",
        "每轮(ok,skipped)=%s 落盘=%s 源文件未变=%s 残留=%s" %
        ([(x[0], x[1]) for x in seen], names, rb(a) == orig, tmp_left),
        rb(a) == orig and not tmp_left and len(names) >= 2)

    # 全部重压 ×5（force）→ 是否持续递增且绝不覆盖
    clear_results()
    for i in range(5):
        compress(items([a], src), out, dict(BASE_OPTS, exist_policy="rename"), force=True)
    names2 = sorted(n for n in os.listdir(out) if not n.startswith("."))
    row("C04b", "rename 模式 force 重压 5 次",
        "每次新增一个 (N) 副本，原图仍不变",
        "落盘=%s 源文件未变=%s" % (names2, rb(a) == orig),
        rb(a) == orig and len(names2) >= 5)


def c05_same_folder():
    """输出到原文件夹：exist_policy 被强制覆盖；确认不会退化成"什么都不做"。"""
    clear_results()
    src = workdir("c05_src")
    a = make_src(src, "sf.jpg", data=b"ORIGINAL-INPLACE")
    t1 = compress(items([a], src), "", dict(BASE_OPTS, exist_policy="skip"), same_folder=True)
    after1 = rb(a)
    ok1 = t1["ok"] == 1 and after1.startswith(b"QA-FAKE")
    row("C05a", "same_folder + exist_policy=skip → 实际按覆盖原图",
        "ok=1，原图被压缩结果替换（保持旧语义）",
        "ok=%s status=%s 内容头=%r" % (t1["ok"], t1["results"][0].get("status"), after1[:20]), ok1)

    t2 = compress(items([a], src), "", dict(BASE_OPTS, exist_policy="skip"), same_folder=True)
    row("C05b", "same_folder 同参数再点一次",
        "skipped（已压过，避免二次压缩掉画质）；不应报「已覆盖」",
        "ok=%s skipped=%s 内容不变=%s" % (t2["ok"], t2["skipped"], rb(a) == after1),
        t2["skipped"] == 1 and rb(a) == after1)

    t3 = compress(items([a], src), "", dict(BASE_OPTS, jpeg_quality=40), same_folder=True)
    row("C05c", "same_folder 改参数再压",
        "ok=1，按新参数重压（覆盖）",
        "ok=%s skipped=%s 参数 q=%r" %
        (t3["ok"], t3["skipped"], rb(a)[:40]), t3["ok"] == 1)


def c06_real_kill_midway():
    """真 kill：6 个文件，压到第 3 个时把服务+引擎整棵树强杀 → 重启 → 再压。"""
    clear_results()
    src, out = workdir("c06_src"), workdir("c06_out")
    paths = [make_src(src, "k%d.jpg" % i, data=b"K" * 8000) for i in range(6)]
    restart({"QA_MODE": "hang", "QA_PARTIAL_N": "777", "QA_SLEEP": "0"})
    clear_engine_calls()
    tid = start_compress(items(paths, src), out, dict(BASE_OPTS), keep=False)
    # 等到有文件写入落盘（前两个正常？不——hang 模式下每个都会挂住）
    deadline = time.time() + 20
    while time.time() < deadline:
        calls = engine_calls()
        st = http_json("/api/tasks/" + tid)[1]
        if st["done"] >= 1 or len(calls) >= 1:
            break
        time.sleep(0.1)
    time.sleep(0.6)
    mid = http_json("/api/tasks/" + tid)[1]
    stop_server(hard=True)          # ← 真·闪退（taskkill /F /T）
    out_before = sorted(n for n in os.listdir(out) if not n.startswith("."))
    sizes = {n: os.path.getsize(os.path.join(out, n)) for n in out_before}
    row("C06a", "压到中途强杀（含引擎子进程），观察输出目录残留",
        "理想：输出目录里只有完整文件（半成品应落在临时目录被丢弃）",
        "kill 前 done=%s/%s；残留=%s" % (mid["done"], mid["total"], sizes),
        all(v > 777 for v in sizes.values()))

    restart({})                     # 正常引擎模式重启
    recs = results()
    row("C06b", "重启后 results.json 是否把「半成品」记成已完成",
        "不得存在指向半截文件的 ok 记录",
        "记录数=%d，指向残缺文件的=%s" %
        (len(recs), [p for p, r in recs.items() if r.get("out") and os.path.isfile(r["out"])
                     and os.path.getsize(r["out"]) == 777]),
        not [1 for r in recs.values() if r.get("out") and os.path.isfile(r["out"])
             and os.path.getsize(r["out"]) == 777])

    clear_engine_calls()
    t = compress(items(paths, src), out, dict(BASE_OPTS))
    still_bad = [n for n in os.listdir(out) if not n.startswith(".")
                 and os.path.getsize(os.path.join(out, n)) == 777]
    skipped_bad = [r for r in t["results"] if r.get("status") == "skipped"
                   and os.path.basename(r.get("out") or "") in still_bad]
    row("C06c", "重启后再点压缩：半截文件会不会被当成「已完成」跳过",
        "半截文件必须被重压（skipped 里的文件都应是完整成品）",
        "ok=%s skipped=%s fail=%s；仍是 777 字节的残骸=%s；被误判 skipped=%d" %
        (t["ok"], t["skipped"], t["fail"], still_bad, len(skipped_bad)),
        not still_bad)


def c07_two_concurrent_tasks():
    """连点两次压缩：两个任务同时压同一批文件。"""
    clear_results()
    src, out = workdir("c07_src"), workdir("c07_out")
    a = make_src(src, "race.jpg", data=b"R" * 1000)
    restart({"QA_MODE": "chunk", "QA_SCALE_BY_Q": "1", "QA_UNIT": "61440", "QA_SLEEP": "0.15"})
    clear_engine_calls()
    # 两次任务参数不同（q=95 产物体积大，q=20 体积小）→ 体积不同才可能写出"半大文件"
    tid1 = start_compress(items([a], src), out, dict(BASE_OPTS, exist_policy="overwrite", jpeg_quality=95))
    time.sleep(1.0)          # 让第一个任务写到一半，再点第二次
    tid2 = start_compress(items([a], src), out, dict(BASE_OPTS, exist_policy="overwrite", jpeg_quality=20))
    t1, t2 = wait_task(tid1), wait_task(tid2)
    target = os.path.join(out, "race.jpg")
    data = rb(target)
    nonces = set(re.findall(rb"<n\d+-\d+>", data))
    size = len(data)
    # q=95 → 95*61440=5836800；q=20 → 1228800；合法产物只能是这两个尺寸之一且只含一个 nonce
    ok = len(nonces) == 1 and size in (5836800, 1228800)
    row("C07", "两个任务并发压同一个输出文件（连点两次压缩，参数不同）",
        "最终文件必须是其中一次的完整产物（尺寸/内容不自相矛盾）",
        "task1=(%s ok=%s) task2=(%s ok=%s) 文件大小=%d（合法尺寸 5836800 或 1228800）"
        " 文件里出现 %d 个写者标记=%s spawn=%d" %
        (t1["state"], t1["ok"], t2["state"], t2["ok"], size, len(nonces),
         sorted(x.decode() for x in nonces), len(engine_calls())), ok)


def c08_cancel_counts():
    """取消后计数口径 & 续传。"""
    clear_results()
    src, out = workdir("c08_src"), workdir("c08_out")
    paths = [make_src(src, "n%d.jpg" % i, data=b"N" * 500) for i in range(6)]
    restart({"QA_SLEEP": "0.35"})
    clear_engine_calls()
    tid = start_compress(items(paths, src), out, dict(BASE_OPTS))
    time.sleep(1.2)
    http_post("/api/cancel", {"task_id": tid})
    t = wait_task(tid)
    consistent = t["done"] == t["ok"] + t["skipped"] + t["fail"]
    row("C08a", "取消后的计数口径",
        "done == ok+skipped+fail（前端进度条用 done/total，不能自相矛盾）",
        "state=%s total=%s done=%s ok=%s skipped=%s fail=%s 一致=%s" %
        (t["state"], t["total"], t["done"], t["ok"], t["skipped"], t["fail"], consistent),
        consistent)

    done_paths = set(r["path"] for r in t["results"] if r.get("status") == "ok")
    clear_engine_calls()
    t2 = compress(items(paths, src), out, dict(BASE_OPTS))
    respawn = set((c.get("src") or "") for c in engine_calls())
    overlap = done_paths & respawn
    row("C08b", "取消后再点压缩：已完成的不许重压",
        "已完成文件 0 次 spawn，只压剩下的",
        "已完成=%d 个，本轮 spawn=%d 次，重压了已完成文件=%s" %
        (len(done_paths), len(engine_calls()), sorted(overlap)),
        not overlap and t2["done"] == 6)


def c09_api_robustness():
    """畸形/越界请求不许把服务打挂。"""
    src = workdir("c09_src")
    a = make_src(src)
    cases = [
        ("畸形 JSON", "/api/compress", b"{not json"),
        ("JSON 是数组", "/api/compress", b"[]"),
        ("files 是字符串", "/api/compress", json.dumps({"files": "abc", "output": "x"}).encode()),
        ("files 里 path 是数字", "/api/compress",
         json.dumps({"files": [{"path": 123}], "output": ""}).encode()),
        ("files 为空", "/api/compress", json.dumps({"files": [], "output": "x"}).encode()),
        ("exist_policy 非法值", "/api/compress",
         json.dumps({"files": [{"path": a, "root": src}], "output": os.path.join(STATE["root"], "c09_out"),
                     "options": {"exist_policy": "PWNED"}}).encode()),
        ("超长 path（8KB）", "/api/compress",
         json.dumps({"files": [{"path": "/" + "a" * 8000, "root": src}],
                     "output": os.path.join(STATE["root"], "c09_out2")}).encode()),
        ("options 不是对象", "/api/compress",
         json.dumps({"files": [{"path": a, "root": src}], "output": os.path.join(STATE["root"], "c09_out3"),
                     "options": "str"}).encode()),
        ("results/status files 是字符串", "/api/results/status", json.dumps({"files": "abc"}).encode()),
        ("scan paths 是字符串", "/api/scan", json.dumps({"paths": "abc"}).encode()),
        ("cancel 缺 task_id", "/api/cancel", b"{}"),
    ]
    bad = []
    detail = []
    for name, path, raw in cases:
        code, body = http_post_raw(path, raw)
        detail.append("%s->%s" % (name, code))
        if code in (-1,) or code >= 500:
            bad.append("%s(%s)" % (name, code))
    alive_code, alive = http_json("/api/status")
    # /api/img 传目录
    d = workdir("c09_dir.jpg")
    code_img, _ = http_get("/api/img?path=" + urllib.parse.quote(d))
    detail.append("img(dir)->%s" % code_img)
    if code_img >= 500:
        bad.append("img(dir)")
    row("C09", "畸形/越界请求（11 条）+ /api/img 传目录",
        "全部返回 4xx/2xx，无 5xx、无连接被重置，服务始终存活",
        "%s；服务存活=%s(%s)" % (" ".join(detail), alive_code == 200, alive.get("version")),
        not bad and alive_code == 200)


def c10_sig_coverage():
    """sig 覆盖率：代码层面比对 + 逐个字段动态验证。"""
    sys.path.insert(0, APP)
    import importlib
    srv = importlib.import_module("server")
    # 1) build_clt_cmd 真正读取的 option key ⊆ SIG_FIELDS
    pool = {"mode", "lossless", "jpeg_quality", "webp_quality", "png_quality", "png_opt_level",
            "max_size_kb", "format", "chroma", "progressive", "zopfli", "tiff_algorithm",
            "keep_exif", "keep_dates", "suffix"}
    missing = sorted(k for k in pool if k not in srv.SIG_FIELDS)
    row("C10a", "静态比对：影响输出的参数是否都进了 SIG_FIELDS",
        "build_clt_cmd 读取的所有 option key（除 exist_policy/overwrite）都在 SIG_FIELDS 里",
        "缺失=%s；SIG_FIELDS=%s" % (missing, list(srv.SIG_FIELDS)), not missing)

    # 2) 逐个字段动态验证：改一个值 sig 必须变
    base = dict(BASE_OPTS)
    b0 = srv.results_signature(base)
    variants = {
        "jpeg_quality": 61, "png_quality": 61, "webp_quality": 61, "png_opt_level": 6,
        "format": "webp", "suffix": "_c", "long_edge": 1280, "width": 800, "height": 600,
        "percent": 50, "keep_exif": False, "keep_dates": False, "max_size_kb": 99,
        "zopfli": True, "chroma": "4:4:4", "progressive": False, "tiff_algorithm": "lzw",
        "mode": "target", "lossless": True, "no_upscale": True,
    }
    stale = []
    for k, v in variants.items():
        if srv.results_signature(dict(base, **{k: v})) == b0:
            stale.append(k)
    # 3) 同时验证"不该影响 sig"的键
    noise = [k for k in ("exist_policy", "overwrite")
             if srv.results_signature(dict(base, **{k: "rename" if k == "exist_policy" else True})) != b0]
    row("C10b", "动态验证：20 个内容参数逐个改动",
        "每一个都必须改变 sig；exist_policy/overwrite 不得改变 sig",
        "未改变 sig 的字段=%s；应忽略却改变了 sig 的字段=%s" % (stale, noise),
        not stale and not noise)

    # 4) 前端 collectOptions 发出的键 vs SIG_FIELDS
    page = srv.PAGE
    m = re.search(r"function collectOptions\(\)\{(.*?)\n\}", page, re.S)
    body = m.group(1) if m else ""
    keys = set(re.findall(r"opts\.([a-z_]+)\s*=", body))
    unknown = sorted(k for k in keys if k not in srv.SIG_FIELDS)
    row("C10c", "前端 collectOptions 发出的键都在 SIG_FIELDS 里",
        "无遗漏（新增参数忘了进 sig → 用户改参数却被跳过）",
        "前端键=%s；不在 SIG_FIELDS 的=%s" % (sorted(keys), unknown), not unknown)


def c11_engine_failure_masked_as_ok():
    """引擎非零退出但已写出半截文件 → 是否被当成成功并写进归属记录。"""
    clear_results()
    src, out = workdir("c11_src"), workdir("c11_out")
    a = make_src(src, "boom.jpg", data=b"B" * 50000)
    restart({"QA_MODE": "partial", "QA_PARTIAL_N": "900", "QA_EXIT": "9"})
    clear_engine_calls()
    t = compress(items([a], src), out, dict(BASE_OPTS))
    target = os.path.join(out, "boom.jpg")
    size = os.path.getsize(target) if os.path.isfile(target) else -1
    rec = results().get(a)
    ok = not (t["ok"] == 1 and size == 900 and rec)
    row("C11a", "引擎退出码 9（编码中途崩溃）但留下了 900 字节半成品",
        "必须判为失败（fail=1），绝不能 ok=1 且写入 ok 记录",
        "ok=%s fail=%s 落盘大小=%s 记录=%s" %
        (t["ok"], t["fail"], size, {k: rec.get(k) for k in ("status", "out_size")} if rec else None), ok)

    clear_engine_calls()
    t2 = compress(items([a], src), out, dict(BASE_OPTS))
    row("C11b", "紧接着再压一次（同一批参数，引擎仍然坏着）",
        "必须重试（spawn 一次）且**不许**被当成'已完成'跳过",
        "ok=%s skipped=%s fail=%s spawn=%d 输出文件=%s" %
        (t2["ok"], t2["skipped"], t2["fail"], len(engine_calls()),
         os.path.getsize(target) if os.path.isfile(target) else "不存在"),
        t2["skipped"] == 0 and len(engine_calls()) == 1)

    # same_folder：引擎中途崩溃会不会直接毁掉原图
    restart({"QA_MODE": "partial", "QA_PARTIAL_N": "500", "QA_EXIT": "9"})
    src2 = workdir("c11c_src")
    b = make_src(src2, "orig.jpg", data=b"PRECIOUS-ORIGINAL" * 2000)
    orig_size = os.path.getsize(b)
    t3 = compress(items([b], src2), "", dict(BASE_OPTS, exist_policy="overwrite"), same_folder=True)
    now = os.path.getsize(b)
    row("C11c", "same_folder（覆盖原图）+ 引擎中途崩溃",
        "原图必须完好（要么不动，要么原子替换成完整成品），不能被截断",
        "status=%s ok=%s 原图 %d 字节 → %d 字节" % (t3["results"][0].get("status"), t3["ok"], orig_size, now),
        now == orig_size)

    # 引擎"直接失败、什么都不写" + 目标路径上已有上一次的产物 → 会不会把旧文件当成本次成果
    clear_results()
    src3, out3 = workdir("c11d_src"), workdir("c11d_out")
    d = make_src(src3, "stale.jpg", data=b"S" * 9000)
    restart({})
    t4 = compress(items([d], src3), out3, dict(BASE_OPTS, jpeg_quality=90, exist_policy="overwrite"))
    before_bytes = rb(os.path.join(out3, "stale.jpg"))
    restart({"QA_MODE": "fail", "QA_EXIT": "3"})
    clear_engine_calls()
    t5 = compress(items([d], src3), out3, dict(BASE_OPTS, jpeg_quality=30, exist_policy="overwrite"))
    after_bytes = rb(os.path.join(out3, "stale.jpg"))
    rec = results().get(d)
    row("C11d", "引擎直接失败（未写任何文件）+ 目标路径上是上一次的旧产物（改 q=90→30 重压）",
        "必须报失败（fail=1），不能把旧文件当本次成果记成 ok",
        "ok=%s fail=%s spawn=%d 文件内容未变=%s 记录 status=%s（参数已是 q=30）" %
        (t5["ok"], t5["fail"], len(engine_calls()), after_bytes == before_bytes,
         rec.get("status") if rec else None),
        t5["fail"] == 1)


def strip_comments(src):
    """去掉 HTML/JS 注释，避免把注释里的 confirm() 字样当成真调用。"""
    s = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    return s


def c12_frontend_contract():
    """前端契约：原有 id 与按钮文案不变、未新增原生 alert/confirm。"""
    base_py = os.path.join(ROOT, "app_tgz", "server.py")
    if not os.path.isfile(base_py):
        row("C12", "前端契约（需要基线 app_tgz/server.py）", "基线存在", "基线缺失，跳过", False)
        return
    with open(base_py, "r", encoding="utf-8", errors="replace") as f:
        old = f.read()
    with open(SERVER_PY, "r", encoding="utf-8", errors="replace") as f:
        new = f.read()
    ids_old = set(re.findall(r'id="([A-Za-z0-9_]+)"', old))
    ids_new = set(re.findall(r'id="([A-Za-z0-9_]+)"', new))
    lost = sorted(ids_old - ids_new)
    added = sorted(ids_new - ids_old)
    texts = ["压缩", "清空", "添加文件", "添加文件夹", "移除", "取消", "重试失败", "压缩选项"]
    lost_txt = [x for x in texts if x in old and x not in new]
    pop_old = len(re.findall(r"(?<![.\w])(?:alert|confirm)\s*\(", strip_comments(old)))
    pop_new = len(re.findall(r"(?<![.\w])(?:alert|confirm)\s*\(", strip_comments(new)))
    row("C12a", "原有 id 集合是否保持（只增不改）",
        "丢失 0 个 id；新增的只能是新功能 id",
        "丢失=%s 新增=%s" % (lost, added), not lost)
    row("C12b", "原有按钮/标签文案是否保持",
        "关键文案一个都不能丢",
        "丢失文案=%s" % lost_txt, not lost_txt)
    row("C12c", "是否新增原生 alert/confirm",
        "新增 0 处（飞牛 iframe 里会被 sandbox 静默拦截）",
        "基线 %d 处 → 现在 %d 处" % (pop_old, pop_new), pop_new <= pop_old)


ALL = [c01_foreign_file_not_clobbered, c01c_user_jpeg_variants, c02_crash_residue_is_skipped, c03_skip_misjudgement,
       c04_rename_five_times, c05_same_folder, c06_real_kill_midway, c07_two_concurrent_tasks,
       c08_cancel_counts, c09_api_robustness, c10_sig_coverage,
       c11_engine_failure_masked_as_ok, c12_frontend_contract]


def main():
    pick = [x.upper() for x in sys.argv[1:]]
    STATE["root"] = tempfile.mkdtemp(prefix="caesium_qa_")
    STATE["work"] = os.path.join(STATE["root"], "work")
    STATE["log"] = os.path.join(STATE["root"], "engine_calls.jsonl")
    os.makedirs(STATE["work"], exist_ok=True)
    STATE["port"] = free_port()
    print("== QA 对抗验证：server.py=%s\n   假引擎=%s\n   临时根=%s\n" %
          (SERVER_PY, FAKE_CLT, STATE["root"]))
    start_server()
    try:
        for fn in ALL:
            cid = fn.__name__.split("_")[0].upper()
            if pick and cid not in pick:
                continue
            print("\n--- %s ---" % fn.__doc__.strip().splitlines()[0])
            try:
                fn()
            except Exception as e:
                row(cid, fn.__doc__.strip().splitlines()[0], "用例自身正常跑完", "异常：%r" % e, False)
    finally:
        code, alive = http_json("/api/status")
        print("\n== 收尾：服务在全部畸形请求后仍存活 = %s ==" % (code == 200))
        stop_server()
        shutil.rmtree(STATE["root"], ignore_errors=True)

    print("\n" + "=" * 100)
    print("%-7s %-52s %-6s" % ("编号", "场景", "结论"))
    print("-" * 100)
    for cid, scene, _e, _a, verdict in ROWS:
        print("%-7s %-52s %-6s" % (cid, scene[:50], verdict))
    fails = [r for r in ROWS if r[4] == "FAIL"]
    print("-" * 100)
    print("合计 %d 条，PASS %d，FAIL %d" % (len(ROWS), len(ROWS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
