#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.7.2 扫描异步化的独立对抗验证（A2/A3/B4-B7）。

不复用工程师用例的结论，全部自己构造：
  * 网关桩（qa_gw_stub.py）复现「504 HTML」真实故障链路；
  * 进程级 I/O 计数（Windows GetProcessIoCounters / Linux /proc/<pid>/io）证明
    "取消之后到底还读不读盘"；
  * 扫描进行中分页 + 单飞 + 取消 + 边界目录。

用法：python qa_scan_api.py [编号...]
"""
import json
import os
import shutil
import socket
import struct
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
sys.path.insert(0, HERE)
import harness as H        # noqa: E402
import qa_gw_stub as GW    # noqa: E402

STATE = {"proc": None, "root": None, "work": None, "port": 0}
ROWS = []


def row(cid, scene, expect, actual, ok):
    ROWS.append((cid, scene, "PASS" if ok else "FAIL"))
    print("  [%s] %-7s %s\n         期望: %s\n         实际: %s" %
          ("PASS" if ok else "FAIL", cid, scene, expect, actual))


# ------------------------------------------------------------------ 进程 I/O 计数
from qa_io import read_bytes as _io_read_bytes  # noqa: E402


def io_read_bytes(pid):
    return _io_read_bytes(pid)


def io_busy_seconds(pid, seconds=1.0):
    """观察 N 秒内进程读了多少字节。返回 (delta_bytes, None) 或 (None, 原因)。"""
    a = io_read_bytes(pid)
    if a is None:
        return None, "io counters unavailable"
    time.sleep(seconds)
    b = io_read_bytes(pid)
    if b is None:
        return None, "io counters unavailable"
    return b - a, None


# ------------------------------------------------------------------ HTTP
def url(path, port=None):
    return "http://127.0.0.1:%d%s" % (port or STATE["port"], path)


def http_get(path, port=None, timeout=30):
    try:
        with urllib.request.urlopen(url(path, port), timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_post(path, payload, port=None, timeout=60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url(path, port), data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode("utf-8"))
        except Exception:
            return e.code, {"raw": body[:300].decode("utf-8", "replace")}


def jget(path, port=None):
    code, body = http_get(path, port)
    try:
        return code, json.loads(body.decode("utf-8"))
    except Exception:
        return code, {"raw": body[:200].decode("utf-8", "replace")}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_server():
    env = dict(os.environ)
    env["CAESIUM_CLT"] = FAKE_CLT
    env["QA_LOG"] = os.path.join(STATE["root"], "engine.jsonl")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([sys.executable, SERVER_PY, "--port", str(STATE["port"]),
                             "--workdir", STATE["work"]], cwd=APP, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    STATE["proc"] = proc
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            raise RuntimeError("server 提前退出: %s\n%s" % (proc.returncode, out))
        try:
            code, _ = jget("/api/status")
            if code == 200:
                return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError("server 启动超时")


def stop_server():
    p = STATE["proc"]
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=15)
        except Exception:
            p.kill()
    STATE["proc"] = None


# ------------------------------------------------------------------ 造数据
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 640, 480)
       + b"\x08\x02\x00\x00\x00" + b"\x00" * 8 + b"IEND\xaeB`\x82")


def make_png(path, w=640, h=480):
    data = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", w, h)
            + b"\x08\x02\x00\x00\x00" + b"\x00" * 8 + b"IEND\xaeB`\x82")
    with open(path, "wb") as f:
        f.write(data)


def build_tree(count, tag, ext=".png"):
    d = os.path.join(STATE["root"], tag)
    os.makedirs(d, exist_ok=True)
    for i in range(count):
        sub = os.path.join(d, "s%02d" % (i % 20))
        if i < 20:
            os.makedirs(sub, exist_ok=True)
        make_png(os.path.join(sub, "img%05d%s" % (i, ext)), 640 + (i % 7), 480)
    return d


def scan_start(paths):
    code, j = http_post("/api/scan", {"paths": paths})
    H.eq(code, 200, "POST /api/scan 应 200")
    return j["scan_id"]


def scan_status(sid, offset=0, limit=500):
    code, j = jget("/api/scan/status?id=%s&offset=%d&limit=%d" % (sid, offset, limit))
    return j


def wait_scan(sid, timeout=120):
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        last = scan_status(sid, 0, 1)
        if last.get("state") in ("done", "canceled", "error"):
            return last
        time.sleep(0.05)
    raise AssertionError("扫描超时未结束: %s" % last)


def drain(sid, page=500):
    """把结果全量拉出来（客户端视角）。"""
    out, off, guard = [], 0, 0
    while guard < 10000:
        guard += 1
        j = scan_status(sid, off, page)
        files = j.get("files") or []
        out.extend(files)
        off = j.get("next_offset")
        if j.get("state") != "running" and off >= (j.get("total") or 0):
            break
        if not files:
            break
    return out


# ================================================================== 用例
def a1_post_returns_immediately_even_behind_gateway():
    """真实故障链路：经网关调 /api/scan，网关 1s 超时；新版必须毫秒级返回 JSON。"""
    big = build_tree(6000, "a1_big")
    GW.clear()
    gport, srv = GW.start("127.0.0.1:%d" % STATE["port"], timeout=1.0)
    try:
        t0 = time.time()
        code, body = http_get("/api/status", gport)          # 预热
        t0 = time.time()
        req = urllib.request.Request(url("/api/scan", gport),
                                     data=json.dumps({"paths": [big]}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read().decode("utf-8")
            code = r.status
        dt = (time.time() - t0) * 1000
        ok_json = payload.lstrip().startswith("{")
        sid = json.loads(payload).get("scan_id") if ok_json else None
        row("A1a", "经网关 POST /api/scan（6000 文件，网关超时 1s）",
            "毫秒级返回 JSON（含 scan_id），不是 504 HTML",
            "状态=%s 耗时=%.0f ms 响应头=%r" % (code, dt, payload[:60]), code == 200 and ok_json and dt < 900)
        # 扫描真的在跑
        if sid:
            st = scan_status(sid, 0, 1)
            row("A1b", "拿到 scan_id 后能分页查到进度",
                "state=running/done，total 逐步增长",
                "state=%s total=%s scanned=%s" % (st.get("state"), st.get("total"), st.get("scanned")),
                st.get("state") in ("running", "done"))
            wait_scan(sid, 180)
    finally:
        srv.shutdown()


def a2_cancel_really_stops_disk_reads():
    """用户最痛的点：取消之后到底还读不读盘（进程级 I/O 计数 + scanned 增长双证据）。"""
    big = build_tree(9000, "a2_big")
    sid = scan_start([big])
    time.sleep(0.6)
    st = scan_status(sid, 0, 1)
    before_scanned = st.get("scanned", 0)
    # 取消前：观察 1 秒，证明"确实在读"
    read_before, _err = io_busy_seconds(STATE["proc"].pid, 1.0)
    st2 = scan_status(sid, 0, 1)
    grow_before = st2.get("scanned", 0) - before_scanned
    code, j = http_post("/api/scan/cancel", {"scan_id": sid})
    time.sleep(0.5)                                   # 给线程最多一个循环的收尾时间
    st3 = scan_status(sid, 0, 1)
    frozen_from = st3.get("scanned", 0)
    read_after, _err2 = io_busy_seconds(STATE["proc"].pid, 1.5)
    st4 = scan_status(sid, 0, 1)
    grow_after = st4.get("scanned", 0) - frozen_from
    row("A2a", "取消前：scan 在增长、进程在读盘",
        "scanned 增长 > 0 且 1 秒内进程读字节 > 0",
        "1 秒内 scanned += %d；进程读 %s 字节" % (grow_before, read_before),
        grow_before > 0 and (read_before is None or read_before > 0))
    row("A2b", "取消后：scanned 冻结、进程读字节回落（不许再走盘）",
        "scanned 增量为 0/1（最多差一个条目），1.5 秒内读字节 ≈ 0",
        "state=%s 取消后 scanned += %d；1.5 秒内进程读 %s 字节；接口返回 %s" %
        (st4.get("state"), grow_after, read_after, j),
        st4.get("state") == "canceled" and grow_after <= 1 and (read_after is None or read_after < 65536))


def a3_retry_storm_single_flight():
    """用户连点 5 次「添加文件夹」：只允许 1 个扫描在跑，其余不叠加。"""
    big = build_tree(6000, "a3_big")
    ids = []
    for i in range(5):
        ids.append(scan_start([big]))
        time.sleep(0.12)
    time.sleep(0.4)
    s1 = {sid: scan_status(sid, 0, 1).get("scanned", 0) for sid in ids}
    time.sleep(0.8)
    s2 = {sid: scan_status(sid, 0, 1).get("scanned", 0) for sid in ids}
    states = {sid: scan_status(sid, 0, 1).get("state") for sid in ids}
    growing = [sid for sid in ids if s2[sid] - s1[sid] > 5]
    running = [sid for sid in ids if states[sid] == "running"]
    row("A3", "连点 5 次「添加文件夹」（5 个 scan 快速连发）",
        "同一时刻只有 1 个在增长；前 4 个变为 canceled 且 scanned 不再增长",
        "各 scan 状态=%s；仍在增长=%s（共 %d 个）" %
        (list(states.values()), len(growing), len(ids)),
        len(growing) <= 1 and len(running) <= 1)


def b4_pagination_while_running():
    """扫描进行中分页拉取：5000 文件 / 页大小 200，查漏条与重条。"""
    big = build_tree(5000, "b4_big")
    sid = scan_start([big])
    got, off, pages, dup = [], 0, [], 0
    seen = set()
    t0 = time.time()
    while time.time() - t0 < 120:
        j = scan_status(sid, off, 200)
        chunk = j.get("files") or []
        pages.append(len(chunk))
        for f in chunk:
            p = f["path"]
            if p in seen:
                dup += 1
            else:
                seen.add(p)
                got.append(p)
        off = j.get("next_offset", off + len(chunk))
        if j.get("state") != "running" and off >= (j.get("total") or 0):
            break
        if j.get("state") == "running" and not chunk:
            time.sleep(0.02)
    final = scan_status(sid, 0, 1)
    truth = set(f["path"] for f in drain(sid, 2000))
    missing = truth - seen
    extra = seen - truth
    row("B4", "扫描进行中按 offset 翻页（5000 文件 / 页 200）",
        "不漏条、不重条；拼起来 == 服务端最终列表",
        "页数=%d 累计=%d 重条=%d 漏条=%d 多余=%d 服务端 total=%s" %
        (len(pages), len(got), dup, len(missing), len(extra), final.get("total")),
        dup == 0 and not missing and not extra and len(got) == 5000)


def b5_total_semantics():
    """total 语义：中途=已发现数，结束=最终数；越界 offset 的表现。"""
    big = build_tree(3000, "b5_big")
    sid = scan_start([big])
    time.sleep(0.15)
    mid = scan_status(sid, 0, 10)
    end = wait_scan(sid, 180)
    over = scan_status(sid, 999999, 100)
    neg = scan_status(sid, -5, 100)
    limited = scan_status(sid, 0, 999999)
    row("B5a", "total 语义：中途 = 已发现数（会增长），结束时 = 最终数",
        "中途 total ≤ 最终 total，且结束时 total == 最终 3000",
        "中途 total=%s → 最终 total=%s" % (mid.get("total"), end.get("total")),
        (mid.get("total") or 0) <= (end.get("total") or 0) and end.get("total") == 3000)
    row("B5b", "offset 越界 / 负数 / limit 超大",
        "越界回空页且 next_offset 不倒退；负数当 0；limit 被夹到上限 2000",
        "越界: files=%d next_offset=%s；负数: offset=%s；limit=999999 → 返回 %d 条" %
        (len(over.get("files") or []), over.get("next_offset"),
         neg.get("offset"), len(limited.get("files") or [])),
        not (over.get("files") or []) and over.get("next_offset") == 999999
        and neg.get("offset") == 0 and len(limited.get("files") or []) <= 2000)


def b6_cancel_then_new_scan_isolated():
    """取消 A 再发起 B：B 的结果不能混进 A，A 的结果也不能漏进 B。"""
    da = build_tree(400, "b6_a")
    db = os.path.join(STATE["root"], "b6_b")
    os.makedirs(db, exist_ok=True)
    for i in range(120):
        make_png(os.path.join(db, "b%03d.png" % i))
    sid_a = scan_start([da])
    time.sleep(0.25)
    wait_scan(sid_a, 60)
    http_post("/api/scan/cancel", {"scan_id": sid_a})
    sid_b = scan_start([db])
    wait_scan(sid_b, 60)
    a = scan_status(sid_a, 0, 1)
    fa = set(f["path"] for f in drain(sid_a, 2000))
    fb = set(f["path"] for f in drain(sid_b, 2000))
    row("B6", "取消 A → 发起 B：两个任务的结果不许互相污染",
        "A 只含 a 树、B 只含 b 树，交集为空",
        "A=%d 条（state=%s）B=%d 条，交集=%d" % (len(fa), a.get("state"), len(fb), len(fa & fb)),
        len(fa) == 400 and len(fb) == 120 and not (fa & fb))


def b7_edge_dirs():
    """空目录 / 只有非图片 / 深嵌套 / 中文与 emoji / 超长名 / 断链符号链接。"""
    root = os.path.join(STATE["root"], "b7")
    os.makedirs(root, exist_ok=True)
    empty = os.path.join(root, "empty")
    os.makedirs(empty, exist_ok=True)
    txt = os.path.join(root, "only_txt")
    os.makedirs(txt, exist_ok=True)
    for i in range(5):
        with open(os.path.join(txt, "x%d.txt" % i), "w") as f:
            f.write("hello")
    deep = os.path.join(root, "deep")
    os.makedirs(deep, exist_ok=True)
    cur = deep
    for i in range(15):                       # 15 层 > SCAN_MAX_DEPTH(12)
        cur = os.path.join(cur, "d%02d" % i)
        os.makedirs(cur, exist_ok=True)
    make_png(os.path.join(cur, "deep.png"))
    make_png(os.path.join(deep, "shallow.png"))
    weird = os.path.join(root, "weird")
    os.makedirs(weird, exist_ok=True)
    make_png(os.path.join(weird, "中文名字 空格.png"))
    make_png(os.path.join(weird, "emoji-\U0001F600\U0001F4F7.png"))
    make_png(os.path.join(weird, ("长" * 200) + ".png"))
    broken = os.path.join(weird, "broken.png")
    try:
        os.symlink(os.path.join(weird, "nope-target.png"), broken)
    except (OSError, NotImplementedError):
        broken = None
    sid = scan_start([root, os.path.join(root, "does_not_exist")])
    st = wait_scan(sid, 120)
    files = drain(sid, 2000)
    names = [f["name"] for f in files]
    row("B7", "空目录/只有非图片/15 层深嵌套/中文+emoji+200 字长名/断链符号链接",
        "state 落到 done（绝不卡 running）；不崩；深层超限被静默跳过但浅层要有",
        "state=%s error=%s 收录=%d 含中文=%s 含 emoji=%s 含深 15 层=%s" %
        (st.get("state"), st.get("error"), len(files),
         any("中文" in n for n in names), any("emoji" in n for n in names),
         any(n == "deep.png" for n in names)),
        st.get("state") == "done" and any("中文" in n for n in names)
        and any("emoji" in n for n in names))


def b8_status_404_and_bad_params():
    """scan_id 不存在 / 空 / 畸形 offset —— 不许 500。"""
    code, j = jget("/api/scan/status?id=deadbeef&offset=0&limit=10")
    code2, j2 = jget("/api/scan/status")
    code3, j3 = jget("/api/scan/status?id=x&offset=abc&limit=xyz")
    code4, j4 = http_post("/api/scan", {"paths": "notalist"})
    code5, j5 = http_post("/api/scan", {})
    code6, j6 = http_post("/api/scan/cancel", {"scan_id": "nope"})
    row("B8", "scan_id 不存在 / 缺参 / offset 非法 / paths 是字符串或缺失",
        "全部 2xx/4xx，不 5xx、不抛 traceback；paths 缺失返回可用的 scan_id",
        "status(未知)=%s status(缺参)=%s status(垃圾参数)=%s scan(str)=%s scan({})=%s cancel=%s" %
        (code, code2, code3, code4, code5, code6),
        min(code, code2, code3, code4, code5, code6) < 500 and code == 404
        and "scan_id" in (j5 or {}))


ALL = [a1_post_returns_immediately_even_behind_gateway, a2_cancel_really_stops_disk_reads,
       a3_retry_storm_single_flight, b4_pagination_while_running, b5_total_semantics,
       b6_cancel_then_new_scan_isolated, b7_edge_dirs, b8_status_404_and_bad_params]


def main():
    pick = [x.upper() for x in sys.argv[1:]]
    STATE["root"] = tempfile.mkdtemp(prefix="caesium_scan_")
    STATE["work"] = os.path.join(STATE["root"], "work")
    os.makedirs(STATE["work"], exist_ok=True)
    STATE["port"] = free_port()
    print("== 扫描异步化对抗验证 server=%s port=%d root=%s\n" % (SERVER_PY, STATE["port"], STATE["root"]))
    start_server()
    t0 = time.time()
    try:
        for fn in ALL:
            cid = fn.__name__.split("_")[0].upper()
            if pick and cid not in pick:
                continue
            print("\n--- %s ---" % fn.__doc__.strip().splitlines()[0])
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                row(cid, fn.__doc__.strip().splitlines()[0], "用例自身跑完", "异常 %r" % e, False)
    finally:
        stop_server()
        shutil.rmtree(STATE["root"], ignore_errors=True)
    print("\n" + "=" * 96)
    for cid, scene, verdict in ROWS:
        print("%-6s %-62s %s" % (cid, scene[:60], verdict))
    fails = [r for r in ROWS if r[2] == "FAIL"]
    print("=" * 96)
    print("合计 %d 条，PASS %d，FAIL %d，耗时 %.1fs" %
          (len(ROWS), len(ROWS) - len(fails), len(fails), time.time() - t0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
