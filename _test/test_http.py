#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真·HTTP 集成测试：把 server.py 以 TCP 模式真跑起来（子进程），CAESIUM_CLT 指向假引擎，
用 urllib 逐条打接口，把"三种策略落盘 / 重启续传 / 改参数重压 / 任务异常 / 大图 413"全验一遍。

不碰任何真实 NAS 路径；所有目录都在系统临时目录下。
"""
import io
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
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "caesium", "app")
SERVER_PY = os.path.join(APP, "server.py")
FAKE_CLT = os.path.join(HERE, "fake_clt.cmd")

sys.path.insert(0, HERE)
import harness as H  # noqa: E402

def manifest_version():
    """版本号以 manifest 为唯一真源，避免测试里硬编码一个会过期的 1.7.5。"""
    with open(os.path.join(ROOT, "caesium", "manifest"), "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"(?m)^\s*version\s*=\s*([0-9][0-9A-Za-z.\-]*)\s*$", text)
    if not m:
        raise AssertionError("manifest 里解析不到 version")
    return m.group(1)


BASE_OPTS = {"mode": "quality", "lossless": False, "jpeg_quality": 90, "png_quality": 90,
             "png_opt_level": 3, "webp_quality": 90, "tiff_algorithm": "deflate", "chroma": "auto",
             "progressive": True, "zopfli": False, "keep_exif": True, "suffix": "", "format": "keep",
             "keep_dates": True, "long_edge": 0, "width": 0, "height": 0, "percent": 0,
             "no_upscale": False, "max_size_kb": 200, "exist_policy": "skip"}

PORT = 0
EVIDENCE = []          # 记录每条 curl 式请求的"方法 路径 -> 状态码 + 关键返回"，写进报告
STATE = {"proc": None, "root": None, "work": None, "log": None}


def note(line):
    EVIDENCE.append(line)
    print("  · %s" % line)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def url(path):
    return "http://127.0.0.1:%d%s" % (PORT, path)


def http_get(path):
    """返回 (code, body_bytes)"""
    try:
        with urllib.request.urlopen(url(path), timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_post(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url(path), data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode("utf-8"))
        except Exception:
            return e.code, {"raw": body[:200]}


def http_json(path):
    code, body = http_get(path)
    return code, json.loads(body.decode("utf-8"))


def engine_calls():
    """假引擎被 spawn 的次数（每 spawn 一次写一行日志）。"""
    p = STATE["log"]
    if not os.path.isfile(p):
        return []
    with open(p, "r", encoding="utf-8") as f:
        return [json.loads(x) for x in f.read().splitlines() if x.strip()]


def start_server():
    env = dict(os.environ)
    env["CAESIUM_CLT"] = FAKE_CLT
    env["CAESIUM_FAKE_LOG"] = STATE["log"]
    env["TRIM_DATA_ACCESSIBLE_PATHS"] = STATE["root"] + os.sep + "src"
    env.pop("CAESIUM_ACCESSIBLE_PATHS", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["CAESIUM_FAKE_SLEEP"] = os.environ.get("TEST_FAKE_SLEEP", "0")
    proc = subprocess.Popen([sys.executable, SERVER_PY, "--port", str(PORT), "--workdir", STATE["work"]],
                            cwd=APP, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    STATE["proc"] = proc
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            raise RuntimeError("server exited early: rc=%s\n%s" % (proc.returncode, out))
        try:
            code, _ = http_json("/api/status")
            if code == 200:
                return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError("server did not become ready in time")


def stop_server(hard=False):
    proc = STATE["proc"]
    if not proc:
        return
    if proc.poll() is None:
        if hard:
            proc.kill()          # 模拟闪退：TerminateProcess，不走任何清理
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    STATE["proc"] = None


def wait_task(tid, timeout=90):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        code, j = http_json("/api/tasks/" + tid)
        if code != 200:
            raise AssertionError("查询任务失败: %s %s" % (code, j))
        last = j
        if j["state"] in ("done", "canceled", "error"):
            return j
        time.sleep(0.2)
    raise AssertionError("任务超时未结束: %s" % last)


def post_compress(files, out_dir, opts=None, keep=False, force=False, same_folder=False):
    payload = {"files": files, "output": "" if same_folder else out_dir,
               "same_folder": same_folder, "keep_structure": keep,
               "force_recompress": force, "options": dict(opts or BASE_OPTS)}
    code, j = http_post("/api/compress", payload)
    if code != 200 or "task_id" not in j:
        raise AssertionError("创建任务失败: %s %s" % (code, j))
    return wait_task(j["task_id"])


def read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def workdir(name):
    d = os.path.join(STATE["root"], name)
    os.makedirs(d, exist_ok=True)
    return d


def make_src(dirpath, name="a.jpg", data=b"ORIGINAL"):
    p = os.path.join(dirpath, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


# ------------------------------------------------------------------ 用例
def test_00_status_reports_manifest_version():
    code, j = http_json("/api/status")
    note("GET /api/status -> %s %s" % (code, j))
    H.eq(code, 200)
    expect = manifest_version()
    H.eq(j["version"], expect, "/api/status 版本号应与 manifest 一致（%s）" % expect)
    H.is_true(j["clt"], "clt 应探测到假引擎存在")


def test_01_skip_policy_does_not_touch_existing_file():
    src, out = workdir("t01_src"), workdir("t01_out")
    a = make_src(src)
    foreign = os.path.join(out, "a.jpg")
    with open(foreign, "wb") as f:
        f.write(b"FOREIGN-OTHER-TOOL")
    before_calls = len(engine_calls())
    opts = dict(BASE_OPTS, exist_policy="skip")
    t = post_compress([{"path": a, "root": src}], out, opts)
    note("POST /api/compress(skip) -> state=%s ok=%s skipped=%s fail=%s" %
         (t["state"], t["ok"], t["skipped"], t["fail"]))
    H.eq(t["state"], "done")
    H.eq((t["ok"], t["skipped"], t["fail"]), (0, 1, 0))
    H.eq(t["results"][0]["status"], "skipped")
    H.eq(len(engine_calls()), before_calls, "skip 命中时不允许 spawn 引擎")
    H.eq(read_bytes(foreign), b"FOREIGN-OTHER-TOOL", "已存在文件必须原样保留")
    note("假引擎累计被 spawn %d 次（本次 +0）" % len(engine_calls()))


def test_02_overwrite_policy_replaces_file():
    src, out = workdir("t02_src"), workdir("t02_out")
    a = make_src(src)
    existing = os.path.join(out, "a.jpg")
    with open(existing, "wb") as f:
        f.write(b"OLD")
    opts = dict(BASE_OPTS, exist_policy="overwrite")
    t = post_compress([{"path": a, "root": src}], out, opts)
    note("POST /api/compress(overwrite) -> ok=%s status=%s" % (t["ok"], t["results"][0]["status"]))
    H.eq((t["ok"], t["skipped"]), (1, 0))
    H.is_true(read_bytes(existing).startswith(b"FAKE"), "overwrite 应真覆盖")
    last = engine_calls()[-1]
    H.eq(last["overwrite"], "all", "overwrite 策略应传 -O all")
    note("引擎实际收到 -O %s" % last["overwrite"])


def test_03_rename_policy_keeps_two_copies():
    src, out = workdir("t03_src"), workdir("t03_out")
    a = make_src(src)
    first = os.path.join(out, "a.jpg")
    with open(first, "wb") as f:
        f.write(b"OLD")
    opts = dict(BASE_OPTS, exist_policy="rename")
    t = post_compress([{"path": a, "root": src}], out, opts)
    outs = sorted(os.listdir(out))
    note("POST /api/compress(rename) -> ok=%s 落盘=%s" % (t["ok"], outs))
    H.eq((t["ok"], t["skipped"]), (1, 0))
    H.eq(t["results"][0]["out"].replace("\\", "/").split("/")[-1], "a (2).jpg")
    H.eq(read_bytes(first), b"OLD", "rename 绝不能覆盖原文件")
    H.is_true(not [x for x in outs if x.startswith(".caesium-tmp")], "临时目录必须清理")
    H.eq(engine_calls()[-1]["overwrite"], "never", "rename 策略引擎侧也不许覆盖")


def test_04_crash_midway_then_resume_without_recompressing():
    """闪退续传核心场景：压到一半把服务打死 → 重启 → 记录还在 → 再压只压剩下的。

    v1.8.x 默认 16 路并行：要制造「压到一半」的中间态，文件数必须多于 workers
    且单文件耗时足够长，否则采样前就全压完了，续传根本测不到（旧测试用 6 文件 x
    0.4s，在 16 路并行下瞬间全完成，断言 n_rec<6 必然失败）。
    """
    src, out = workdir("t04_src"), workdir("t04_out")
    nfiles = 24
    files = []
    for i in range(nfiles):
        files.append({"path": make_src(src, "f%02d.jpg" % i), "root": src})
    # start_server() 把 TEST_FAKE_SLEEP 映射成子进程的 CAESIUM_FAKE_SLEEP，
    # 所以这里必须设 TEST_FAKE_SLEEP（直接设 CAESIUM_FAKE_SLEEP 会被覆盖成 0）。
    stop_server()
    os.environ["TEST_FAKE_SLEEP"] = "2.0"
    start_server()
    try:
        code, j = http_post("/api/compress", {"files": files, "output": out, "same_folder": False,
                                              "keep_structure": False,
                                              "options": dict(BASE_OPTS)})
        tid = j["task_id"]
        note("POST /api/compress(%d 文件, 每文件 2.0s) -> task_id=%s" % (nfiles, tid))
        # 等到"至少压完 1 个、但没全压完"再动手：机器快慢不同，不能靠固定 sleep
        # （本机一次 fake 引擎 spawn 约 1.0s，固定 1.2s 会恰好在第 1 个还没压完时采样）
        deadline = time.time() + 180
        mid = {"done": 0, "total": len(files), "state": "running"}
        while time.time() < deadline:
            code, mid = http_json("/api/tasks/" + tid)
            if mid.get("done", 0) >= 1:
                break
            time.sleep(0.1)
        time.sleep(1.3)     # 留出 results.json 落盘节流窗口（≥1s 才写盘）
        code, mid = http_json("/api/tasks/" + tid)
        note("中途 GET /api/tasks/%s -> state=%s done=%s/%s" % (tid[:6], mid["state"], mid["done"], mid["total"]))
        stop_server(hard=True)                # 模拟闪退
        note("已 kill -9 服务（模拟闪退）")
        start_server()
        code, res = http_json("/api/results")
        t04_paths = set(f["path"] for f in files)
        n_rec = len([p for p in res.get("records", {}) if p in t04_paths])
        note("重启后 GET /api/results -> code=%s 本任务已完成记录=%s（全库 %s 条，含前几个用例）" %
             (code, n_rec, len(res.get("records", {}))))
        H.is_true(n_rec >= 1, "重启后必须能读到至少一条已完成记录")
        H.is_true(n_rec < nfiles, "应该只压完了一部分（否则测不到续传）：%d/%d" % (n_rec, nfiles))
        before_calls = len(engine_calls())
        done_paths = set(p for p in res["records"].keys() if p in t04_paths)
        n_exist = len([n for n in os.listdir(out) if not n.startswith(".")])
        note("重跑前：记录 %d 个 / 输出目录已有成品 %d 个" % (len(done_paths), n_exist))
        t = post_compress(files, out, dict(BASE_OPTS))
        note("再次 POST /api/compress -> state=%s ok=%s skipped=%s fail=%s" %
             (t["state"], t["ok"], t["skipped"], t["fail"]))
        H.eq(t["state"], "done")
        # 已经压出来的文件（有记录的 + 记录丢了但成品还在的）都不许重压
        H.eq(t["skipped"], n_exist, "输出已存在的文件必须全部跳过")
        H.eq(t["ok"], nfiles - n_exist, "只应处理真正还没压过的文件")
        H.eq(t["done"], t["ok"] + t["skipped"] + t["fail"])
        new_calls = engine_calls()[before_calls:]
        recompressed = set((c.get("src") or "").replace("\\", "/") for c in new_calls)
        H.eq(len(new_calls), nfiles - n_exist, "第二次只允许为剩余文件 spawn 引擎")
        overlap = done_paths & recompressed
        H.eq(len(overlap), 0, "已有压缩记录的文件不得被重新压缩：%s" % overlap)
        note("第二次 spawn %d 次（剩余 = %d），有记录的 %d 个文件 0 次重压" %
             (len(new_calls), nfiles - n_exist, len(done_paths)))

        # 第三遍：全部压完之后再压一次，必须完全不 spawn 引擎（纯记录命中）
        before3 = len(engine_calls())
        t3 = post_compress(files, out, dict(BASE_OPTS))
        note("第三遍 POST /api/compress（全部已完成）-> ok=%s skipped=%s，引擎新增 spawn=%d" %
             (t3["ok"], t3["skipped"], len(engine_calls()) - before3))
        H.eq((t3["ok"], t3["skipped"], t3["fail"]), (0, nfiles, 0), "全部已完成时应全部 skipped")
        H.eq(len(engine_calls()) - before3, 0, "全部已完成时不允许再 spawn 引擎")
    finally:
        os.environ["TEST_FAKE_SLEEP"] = "0"
        stop_server()
        start_server()


def test_05_param_change_invalidates_done_state():
    src, out = workdir("t05_src"), workdir("t05_out")
    a = make_src(src)
    files = [{"path": a, "root": src}]
    t1 = post_compress(files, out, dict(BASE_OPTS, jpeg_quality=90))
    H.eq(t1["ok"], 1)
    code, st = http_post("/api/results/status", {"files": files, "options": dict(BASE_OPTS, jpeg_quality=90),
                                                 "output": out, "keep_structure": False})
    note("POST /api/results/status(q=90) -> code=%s sig=%s done=%s" % (code, st.get("sig"), len(st.get("done", {}))))
    H.eq(len(st["done"]), 1, "同参数应判定已完成")
    code, st2 = http_post("/api/results/status", {"files": files, "options": dict(BASE_OPTS, jpeg_quality=60),
                                                  "output": out, "keep_structure": False})
    note("POST /api/results/status(q=60) -> sig=%s done=%s" % (st2.get("sig"), len(st2.get("done", {}))))
    H.is_true(st2["sig"] != st["sig"], "改质量必须改变参数指纹")
    H.eq(len(st2["done"]), 0, "参数变了不能算已完成")
    before_calls = len(engine_calls())
    t2 = post_compress(files, out, dict(BASE_OPTS, jpeg_quality=60))
    note("改 q=60 后 POST /api/compress -> ok=%s skipped=%s，引擎新增 spawn %d 次" %
         (t2["ok"], t2["skipped"], len(engine_calls()) - before_calls))
    H.eq((t2["ok"], t2["skipped"]), (1, 0), "参数变化必须真重压")
    H.eq(len(engine_calls()) - before_calls, 1, "参数变化应再 spawn 一次")
    H.eq(engine_calls()[-1]["quality"], "60", "重压应使用新质量")


def test_06_task_exception_lands_in_error_not_running():
    src = workdir("t06_src")
    a = make_src(src)
    blocker = os.path.join(STATE["root"], "blocker.txt")
    with open(blocker, "wb") as f:
        f.write(b"x")
    bad_out = os.path.join(blocker, "sub")   # 父路径是文件 → makedirs 抛异常
    code, j = http_post("/api/compress", {"files": [{"path": a, "root": src}], "output": bad_out,
                                          "same_folder": False, "keep_structure": False,
                                          "options": dict(BASE_OPTS)})
    tid = j["task_id"]
    t = wait_task(tid, timeout=30)
    note("POST /api/compress(非法输出目录) -> state=%s error=%s" %
         (t["state"], (t.get("error") or "")[:60]))
    H.is_true(t["state"] in ("error", "done"), "任务不能卡在 running")
    if t["state"] == "error":
        H.is_true(t.get("error"), "error 状态必须带原因")
    else:
        H.eq(t["fail"], 1, "若未整体异常，也应逐文件失败")
    H.is_true(t["state"] != "running", "绝不允停在 running")


def test_07_img_streaming_and_too_large():
    src = workdir("t07_src")
    small = make_src(src, "small.png", b"\x89PNG\r\n\x1a\n" + b"z" * 5000)
    code, body = http_get("/api/img?path=" + urllib.parse.quote(small))
    note("GET /api/img(5KB png) -> code=%s len=%s" % (code, len(body)))
    H.eq(code, 200)
    H.eq(body, read_bytes(small), "小图应完整返回")
    big = os.path.join(src, "big.jpg")
    with open(big, "wb") as f:
        f.seek(65 * 1024 * 1024 - 1)
        f.write(b"\0")
    code2, body2 = http_get("/api/img?path=" + urllib.parse.quote(big))
    note("GET /api/img(65MB jpg) -> code=%s body=%r" % (code2, body2[:40]))
    H.eq(code2, 413, "超过 64MB 应返回 413 而不是把进程撑爆")


def test_08_active_task_endpoint():
    code, j = http_json("/api/tasks/active")
    note("GET /api/tasks/active -> %s %s" % (code, json.dumps(j)[:60]))
    H.eq(code, 200)
    H.is_true("task" in j, "必须返回 task 字段")


def test_09_results_clear():
    code, j = http_post("/api/results/clear", {})
    note("POST /api/results/clear -> %s %s" % (code, j))
    H.eq(code, 200)
    code, res = http_json("/api/results")
    H.eq(len(res["records"]), 0, "清空后不应还有记录")


# ------------------------------------------------------------------ v1.7.2 异步扫描
def _scan_wait(sid, timeout=180):
    """等扫描结束，返回末页快照（offset=0,limit=1 只看状态）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, j = http_json("/api/scan/status?id=%s&offset=0&limit=1" % sid)
        if code != 200:
            raise AssertionError("查扫描状态失败 %s %s" % (code, j))
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("扫描超时")


def test_10_scan_is_async_and_paginated():
    """2000 个文件：POST /api/scan 必须 50ms 内返回；分页拼接无重无漏。"""
    src = workdir("t10_src")
    n = 2000
    jpeg = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\x00" * 100 + b"\xff\xc0\x00\x11\x08\x00\x64\x00\xc8\x03" + b"\x00" * 200 + b"\xff\xd9")
    for i in range(n):
        make_src(src, "p%05d.jpg" % i, jpeg)

    t0 = time.time()
    code, j = http_post("/api/scan", {"paths": [src]})
    elapsed_ms = (time.time() - t0) * 1000
    note("POST /api/scan（%d 个文件）-> %s %s，耗时 %.1f ms" % (n, code, json.dumps(j), elapsed_ms))
    H.eq(code, 200)
    H.is_true("scan_id" in j, "必须立刻返回 scan_id")
    H.is_true(elapsed_ms < 50, "POST /api/scan 必须 50ms 内返回（本次 %.1f ms）" % elapsed_ms)

    sid = j["scan_id"]
    # 进度可观测：连续两次 status 能看到 scanned/total 变化
    prog = []
    for _ in range(40):
        code, s = http_json("/api/scan/status?id=%s&offset=0&limit=1" % sid)
        prog.append((s["state"], s["scanned"], s["total"]))
        if s["state"] != "running":
            break
        time.sleep(0.02)
    H.is_true(len(set(p[2] for p in prog)) >= 2,
              "轮询必须能看到 total 递增，实际采样 %s" % prog[:6])
    note("扫描进度采样 %s … %s" % (prog[:3], prog[-2:]))

    snap = _scan_wait(sid)
    H.eq(snap["state"], "done")
    H.eq(snap["total"], n, "total 应为 %d" % n)
    # 分页拉全量（limit=500 → 4 页）
    got, pages = [], []
    while True:
        code, j = http_json("/api/scan/status?id=%s&offset=%d&limit=500" % (sid, len(got)))
        H.eq(code, 200)
        got.extend(j["files"])
        pages.append(len(j["files"]))
        if not j["files"] or len(got) >= j["total"]:
            break
    note("分页拉取 %d 条，页大小 %s" % (len(got), pages))
    H.eq(len(got), n, "分页拼起来应有 %d 条" % n)
    H.eq(len(set(x["path"] for x in got)), n, "分页拼接不许有重复")
    expect = set(os.path.join(src, x) for x in os.listdir(src))
    H.eq(set(x["path"] for x in got), expect, "分页拼接不许有遗漏")
    H.eq(pages, [500, 500, 500, 500], "2000 条按 limit=500 应恰好 4 页")
    # 单条也别超过 limit
    code, one = http_json("/api/scan/status?id=%s&offset=0&limit=3" % sid)
    H.eq(len(one["files"]), 3, "limit=3 只能返回 3 条")
    H.eq(one["next_offset"], 3)


def test_11_scan_single_flight_cancel_and_404():
    src = workdir("t11_src")
    jpeg = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\x00" * 100 + b"\xff\xc0\x00\x11\x08\x00\x64\x00\xc8\x03" + b"\x00" * 200 + b"\xff\xd9")
    for i in range(800):
        make_src(src, "q%05d.jpg" % i, jpeg)

    # 单飞：连发两次，旧的必须变 canceled
    code1, j1 = http_post("/api/scan", {"paths": [src]})
    code2, j2 = http_post("/api/scan", {"paths": [src]})
    H.eq((code1, code2), (200, 200))
    time.sleep(0.1)
    code, old = http_json("/api/scan/status?id=%s" % j1["scan_id"])
    note("单飞：旧任务 state=%s，新任务=%s" % (old["state"], j2["scan_id"]))
    H.eq(old["state"], "canceled", "连发两次时旧扫描必须变 canceled")
    H.eq(_scan_wait(j2["scan_id"])["state"], "done", "新扫描应正常跑完")

    # 取消：立刻 canceled，之后不再增长
    code3, j3 = http_post("/api/scan", {"paths": [src]})
    time.sleep(0.05)
    codec, jc = http_post("/api/scan/cancel", {"scan_id": j3["scan_id"]})
    H.eq(codec, 200)
    H.is_true(jc["ok"], "取消应命中")
    code, s1 = http_json("/api/scan/status?id=%s" % j3["scan_id"])
    time.sleep(0.4)
    code, s2 = http_json("/api/scan/status?id=%s" % j3["scan_id"])
    note("取消后 state=%s，total %d → %d" % (s2["state"], s1["total"], s2["total"]))
    H.eq(s2["state"], "canceled")
    H.eq(s2["total"], s1["total"], "取消后 total 不许增长")

    # 未知 scan_id → 404 JSON（不是 HTML、不是 5xx）
    code4, j4 = http_json("/api/scan/status?id=deadbeef")
    note("GET /api/scan/status?id=deadbeef -> %s %s" % (code4, j4))
    H.eq(code4, 404)
    H.eq(j4.get("state"), "error", "未知 scan_id 应回 JSON 错误")


def _queue_items(start, count):
    out = []
    for i in range(start, start + count):
        out.append({"path": "/vol/%04d.jpg" % i, "name": "%04d.jpg" % i, "size": 1024,
                    "ext": ".jpg", "root": "/vol", "w": 100, "h": 80})
    return out


def test_12_queue_batch_upload_2000():
    """队列分批 POST：2000 条分 4 批（append 接力），最终 count == 2000，单批 body 远小于 1MB。"""
    TOTAL, BATCH = 2000, 500
    bodies = []
    for start in range(0, TOTAL, BATCH):
        batch = _queue_items(start, BATCH)
        raw = json.dumps({"files": batch, "append": start > 0}, ensure_ascii=False).encode("utf-8")
        bodies.append(len(raw))
        req = urllib.request.Request(url("/api/queue"), data=raw,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read().decode("utf-8"))
        H.is_true(j.get("ok"), "每批都应保存成功")
    code, j = http_json("/api/queue")
    note("队列分批上传：4 批 body=%d/%d/%d/%d B，最终 count=%d" %
         (bodies[0], bodies[1], bodies[2], bodies[3], j["count"]))
    H.eq(code, 200)
    H.eq(j["count"], TOTAL, "服务端最终应有 %d 条" % TOTAL)
    H.eq(len(j["files"]), TOTAL, "返回的文件数应与 count 一致")
    H.eq(j["files"][0]["path"], "/vol/0000.jpg", "顺序应保持（第一批在最前）")
    H.eq(j["files"][-1]["path"], "/vol/1999.jpg", "最后一批应在最后")
    H.is_true(max(bodies) < 500 * 1024,
              "单批 body 必须远小于网关 1MB 限制（最大 %d B）" % max(bodies))


def test_13_queue_full_replace_and_dedup():
    """append 缺省 = 整份替换；同一 path 重复出现只留一条（幂等）。"""
    batch = _queue_items(0, 10) + _queue_items(0, 3)      # 故意带 3 条重复
    code, j = http_post("/api/queue", {"files": batch})
    note("POST /api/queue（10 条 + 3 条重复，不带 append）-> %s" % j)
    H.eq(code, 200)
    H.eq(j["count"], 10, "整份替换 + 去重后应是 10 条")
    code, got = http_json("/api/queue")
    H.eq(got["count"], 10, "服务端只应存 10 条")
    H.eq(len(set(x["path"] for x in got["files"])), 10, "不许有重复 path")
    # 空列表能清空队列
    code, j = http_post("/api/queue", {"files": []})
    code, got = http_json("/api/queue")
    H.eq(got["count"], 0, "空列表应把队列清空")


def test_14_compress_by_queue_reference():
    """大列表改用「队列引用」提交：请求体只有几百字节，服务端从队列取文件。

    背景：1 万条以上把 files 塞进 POST 体 ≈ 1MB+，会撞飞牛网关 client_max_body_size
    （超了直接 413，前端只看到 HTML）。v1.7.5 起前端对整表提交改发 {use_queue:true}。
    """
    src, out = workdir("t14_src"), workdir("t14_out")
    a = make_src(src, "a.jpg", b"AAAA" * 50)
    b = make_src(src, "b.jpg", b"BBBB" * 50)
    items = [{"path": a, "root": src, "name": "a.jpg", "size": 200, "ext": ".jpg"},
             {"path": b, "root": src, "name": "b.jpg", "size": 200, "ext": ".jpg"}]
    code, j = http_post("/api/queue", {"files": items})
    H.eq(code, 200)
    H.eq(j["count"], 2, "队列应有 2 条")

    payload = {"use_queue": True, "output": out, "same_folder": False,
               "keep_structure": False, "options": dict(BASE_OPTS)}
    body_len = len(json.dumps(payload))
    code, j = http_post("/api/compress", payload)
    note("POST /api/compress {use_queue:true}（body=%d B）-> %s" % (body_len, j))
    H.eq(code, 200, "队列引用提交应成功")
    H.is_true(body_len < 2000, "请求体应远小于 1MB，实际 %d B" % body_len)
    t = wait_task(j["task_id"])
    H.eq(t["total"], 2, "任务文件数应等于队列条数")
    H.eq(t["ok"], 2, "两个文件都应压成功")
    H.is_true(os.path.isfile(os.path.join(out, "a.jpg")), "输出目录应有 a.jpg")

    # 队列为空时不许当成"0 个文件成功"：必须明确报错，让前端退回逐条提交
    http_post("/api/queue", {"files": []})
    code, j = http_post("/api/compress", payload)
    note("POST /api/compress {use_queue:true} 队列为空 -> %s %s" % (code, j))
    H.eq(code, 409, "队列为空应返回 409")
    H.eq(j.get("code"), "queue_empty", "错误码应可识别，供前端自动退回")


def main():
    global PORT
    STATE["root"] = tempfile.mkdtemp(prefix="caesium_http_")
    STATE["work"] = os.path.join(STATE["root"], "work")
    STATE["log"] = os.path.join(STATE["root"], "engine_calls.jsonl")
    os.makedirs(STATE["work"], exist_ok=True)
    os.makedirs(os.path.join(STATE["root"], "src"), exist_ok=True)
    PORT = free_port()
    print("== 启动 server.py --port %d --workdir %s ==" % (PORT, STATE["work"]))
    start_server()
    print("== server ready (engine=%s) ==" % FAKE_CLT)
    try:
        failed = H.run(dict(globals()))
    finally:
        stop_server()
        print("\n== 请求留证 ==")
        for line in EVIDENCE:
            print(line)
        shutil.rmtree(STATE["root"], ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  （用于 quote，放在 main 之外供用例使用）
    main()
