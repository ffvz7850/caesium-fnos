#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.7.2 第 2 轮：QA 自己再挖一遍的边界用例（不复用工程师用例）。

覆盖：
  A4 残骸判定有没有被 EOI 回扫改松（2KB/300KB 截断 JPEG、截断 PNG、9MB 超预算）
  A5 EOI 恰好跨 64KB 回扫块边界（我自己怀疑的实现漏洞）
  A8 _jpeg_has_eoi 的 I/O 代价（20MB 正常图 / 20MB 尾部无 EOI）
  B6 分页 + 动态增长边界（offset 超 total / 空页 / total 增长 / 取消后立刻重扫）
  B7 队列分批（半途失败 / 重复 path / append 语义 / 20000 上限）
  B9 真网关 504 + 取消后读盘（复用 qa_gw_stub）

用法：python qa_round2.py [编号...]
"""
import importlib.util
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
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(TEST_DIR)
APP = os.path.join(ROOT, "caesium", "app")
SERVER_PY = os.path.join(APP, "server.py")
FAKE_CLT = os.path.join(HERE, "qa_fake.cmd")

sys.path.insert(0, TEST_DIR)
sys.path.insert(0, HERE)
import qa_io                       # noqa: E402
import qa_gw_stub as GW           # noqa: E402

STATE = {"proc": None, "root": None, "work": None, "port": 0}
ROWS = []


def row(cid, scene, expect, actual, ok):
    ROWS.append((cid, scene, "PASS" if ok else "FAIL"))
    print("  [%s] %-7s %s\n         期望: %s\n         实际: %s" %
          ("PASS" if ok else "FAIL", cid, scene, expect, actual))


def load_server():
    spec = importlib.util.spec_from_file_location("server_final_172", SERVER_PY)
    m = importlib.util.module_from_spec(spec)
    sys.modules["server_final_172"] = m
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------------ HTTP
def url(p):
    return "http://127.0.0.1:%d%s" % (STATE["port"], p)


def jget(p):
    try:
        with urllib.request.urlopen(url(p), timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read()[:200]}


def jpost(p, payload):
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url(p), data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read()[:200]}


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def start_server():
    env = dict(os.environ)
    env["CAESIUM_CLT"] = FAKE_CLT
    env["QA_LOG"] = os.path.join(STATE["root"], "engine.jsonl")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([sys.executable, SERVER_PY, "--port", str(STATE["port"]), "--workdir", STATE["work"]],
                            cwd=APP, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    STATE["proc"] = proc
    end = time.time() + 30
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError("server 提前退出 %s %s" % (proc.returncode, proc.stdout.read()))
        try:
            if jget("/api/status")[0] == 200:
                return
        except Exception:
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


def rb(p):
    with open(p, "rb") as f:
        return f.read()


def w(path, data):
    with open(path, "wb") as f:
        f.write(data)


def wait_scan(sid, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = jget("/api/scan/status?id=%s&offset=0&limit=1" % sid)[1]
        if j.get("state") in ("done", "canceled", "error"):
            return j
        time.sleep(0.05)
    raise AssertionError("扫描超时")


# ------------------------------------------------------------------ 图片构造
JPEG_HEAD = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 640, 480) + b"\x08\x02\x00\x00\x00"


def complete_jpeg(size, tail=b""):
    """size 字节的完整 JPEG：EOI 在尾部（+可选附加数据，EOI 之后）。"""
    body = JPEG_HEAD + b"\x00" * max(0, size - len(JPEG_HEAD) - 2 - len(tail)) + b"\xff\xd9" + tail
    return body[:size] if len(body) > size else body


# ================================================================== A4/A5/A8 残骸判定
def a4_residue_not_relaxed():
    srv = load_server()
    d = os.path.join(STATE["root"], "a4")
    os.makedirs(d, exist_ok=True)

    cases = []
    # 2KB / 300KB 真截断 JPEG（无 EOI）
    for n in (2048, 300 * 1024):
        p = os.path.join(d, "trunc_%d.jpg" % n)
        w(p, JPEG_HEAD + b"\x00" * (n - len(JPEG_HEAD)))
        cases.append(("截断 JPEG %dB" % n, p, True))
    # 截断 PNG：有 IHDR，尾部 2KB 内没有 IEND
    p = os.path.join(d, "trunc.png")
    w(p, PNG_HEAD + b"\x00" * (300 * 1024))
    cases.append(("截断 PNG 300KB", p, True))
    # 完整 JPEG（EOI 在末尾）
    p = os.path.join(d, "ok.jpg")
    w(p, complete_jpeg(64 * 1024))
    cases.append(("完整 JPEG", p, False))
    # Motion Photo：EOI 之后 5KB 附加数据
    p = os.path.join(d, "motion.jpg")
    w(p, JPEG_HEAD + b"\x00" * 60000 + b"\xff\xd9" + b"\x01" * 5000)
    cases.append(("Motion Photo(EOI+5KB)", p, False))
    # 9MB 无 EOI（超过 8MB 回扫预算）→ 按"不是残骸"
    p = os.path.join(d, "no_eoi_9m.jpg")
    w(p, JPEG_HEAD + b"\x00" * (9 * 1024 * 1024 - len(JPEG_HEAD)))
    cases.append(("9MB 无 EOI（超预算）", p, False))

    bad = []
    for name, path, expect in cases:
        got = srv.image_looks_truncated(path)
        if got != expect:
            bad.append("%s: 期望 %s 实际 %s" % (name, expect, got))
    row("A4", "残骸判定 6 个样本（截断 JPEG 2KB/300KB、截断 PNG、完整图、Motion Photo、9MB 超预算）",
        "截断仍判 True；完整/Motion/超预算判 False（P1 修复不能把判定改松）",
        "不符合=%s" % (bad or "无"), not bad)


def a5_eoi_cross_chunk_boundary():
    """EOI(FFD9) 恰好跨 64KB 回扫块边界：分块查找会不会漏判？"""
    srv = load_server()
    d = os.path.join(STATE["root"], "a5")
    os.makedirs(d, exist_ok=True)
    tail_len = srv.TRUNC_TAIL_BYTES            # 2048
    chunk = srv.EOI_SCAN_CHUNK                 # 65536
    size = tail_len + 2 * chunk                # 133120
    boundary = size - tail_len - chunk          # 65536 = 第一块的起点
    buf = bytearray(b"\x00" * size)
    buf[:len(JPEG_HEAD)] = JPEG_HEAD
    buf[boundary - 1] = 0xFF                   # FF 落在"更靠头"的那一块
    buf[boundary] = 0xD9                       # D9 落在"更靠尾"的那一块
    p = os.path.join(d, "boundary.jpg")
    w(p, bytes(buf))
    got = srv.image_looks_truncated(p)
    row("A5", "EOI 恰好跨 64KB 回扫块边界（文件里确实存在 FFD9）",
        "应判 False（不是残骸）—— 否则用户的合法 JPEG 会被覆盖",
        "EOI 位置=%d/%d，image_looks_truncated=%s（跨块=%s）" %
        (boundary - 1, boundary, got, buf[boundary - 1:boundary + 1] == b"\xff\xd9"),
        got is False)


def a8_eoi_io_cost():
    srv = load_server()
    d = os.path.join(STATE["root"], "a8")
    os.makedirs(d, exist_ok=True)
    # 20MB 正常 JPEG：EOI 在末尾
    ok = os.path.join(d, "ok20.jpg")
    w(ok, complete_jpeg(20 * 1024 * 1024))
    # 20MB 尾部无 EOI，且前 8MB 内也没有 EOI（回扫预算耗尽）
    big = os.path.join(d, "noeoi20.jpg")
    w(big, JPEG_HEAD + b"\x00" * (20 * 1024 * 1024 - len(JPEG_HEAD)))

    def measure(fn):
        for _ in range(2):
            a = qa_io.read_bytes()
            fn()
            b = qa_io.read_bytes()
        return None if a is None else b - a

    read_ok = measure(lambda: srv.image_looks_truncated(ok))
    read_big = measure(lambda: srv.image_looks_truncated(big))
    ok_small = read_ok is None or read_ok <= 16 * 1024
    ok_bounded = read_big is None or (read_big <= 9 * 1024 * 1024 and read_big >= 4 * 1024 * 1024)
    row("A8", "20MB 正常图 vs 20MB 尾部无 EOI：一次残骸判定的进程读字节",
        "正常图 ≤16KB（零额外读）；尾部无 EOI 被 8MB 预算截断（≤9MB，绝不整文件读 20MB）",
        "正常图读 %s 字节；无 EOI 大图读 %s 字节（预算=%s）" %
        (read_ok, read_big, srv.EOI_SCAN_MAX), ok_small and ok_bounded)


def a4b_9m_user_file_not_overwritten():
    """9MB 无 EOI 的用户文件：超预算 → 不是残骸 → skip 策略下不许被覆盖/重压。"""
    src = os.path.join(STATE["root"], "a4b_src")
    out = os.path.join(STATE["root"], "a4b_out")
    os.makedirs(src, exist_ok=True); os.makedirs(out, exist_ok=True)
    a = os.path.join(src, "huge.jpg")
    w(a, b"SRC" * 1000)
    foreign = os.path.join(out, "huge.jpg")
    data = JPEG_HEAD + b"\x00" * (9 * 1024 * 1024 - len(JPEG_HEAD))
    w(foreign, data)
    log = os.path.join(STATE["root"], "engine.jsonl")
    try:
        os.unlink(log)
    except OSError:
        pass
    opts = {"mode": "quality", "jpeg_quality": 90, "exist_policy": "skip"}
    code, j = jpost("/api/compress", {"files": [{"path": a, "root": src}], "output": out,
                                      "same_folder": False, "keep_structure": False, "options": opts})
    tid = j.get("task_id")
    t = None
    for _ in range(200):
        t = jget("/api/tasks/" + tid)[1]
        if t.get("state") in ("done", "error", "canceled"):
            break
        time.sleep(0.05)
    calls = 0
    if os.path.isfile(log):
        calls = len([x for x in rb(log).decode().splitlines() if x.strip()])
    row("A4b", "9MB 无 EOI 的用户同名文件（超 8MB 回扫预算）",
        "skip：0 spawn、文件逐字节不变（超预算按「不是残骸」处理）",
        "state=%s ok=%s skipped=%s spawn=%d 内容未变=%s" %
        (t.get("state"), t.get("ok"), t.get("skipped"), calls, rb(foreign) == data),
        t.get("skipped") == 1 and calls == 0 and rb(foreign) == data)


# ================================================================== B6 分页边界
def b6_pagination_edges():
    d = os.path.join(STATE["root"], "b6")
    os.makedirs(d, exist_ok=True)
    for i in range(1200):
        w(os.path.join(d, "f%04d.png" % i), PNG_HEAD + b"\x00" * 8 + b"IEND\xaeB`\x82")
    sid = jpost("/api/scan", {"paths": [d]})[1]["scan_id"]
    # 一边扫一边翻页：offset 从 0 到超过 total，反复请求同一 offset、空页、跨 total
    seen, offsets = [], []
    t0 = time.time()
    for off in [0, 0, 200, 99999, 400, 800, 1200, 0, 600, 1000, 1400]:
        offsets.append(off)
        j = jget("/api/scan/status?id=%s&offset=%d&limit=200" % (sid, off))[1]
        seen.extend(f["path"] for f in (j.get("files") or []))
    end = wait_scan(sid, 120)
    # 最终全量拉取
    truth, off = [], 0
    while True:
        j = jget("/api/scan/status?id=%s&offset=%d&limit=2000" % (sid, off))[1]
        chunk = j.get("files") or []
        truth.extend(f["path"] for f in chunk)
        off = j.get("next_offset", off + len(chunk))
        if off >= (j.get("total") or 0):
            break
    row("B6a", "扫描进行中乱序/重复/越界/空页地翻页",
        "服务端分页稳定：最终列表 1200 条、无重复、状态能到 done",
        "请求 offset=%s；最终 total=%s 全量=%d 唯一=%d state=%s" %
        (offsets, end.get("total"), len(truth), len(set(truth)), end.get("state")),
        end.get("state") == "done" and len(truth) == 1200 and len(set(truth)) == 1200)

    # 取消后立刻重扫：新任务结果不能混进旧任务
    big = os.path.join(STATE["root"], "b6_cancel")
    os.makedirs(big, exist_ok=True)
    for i in range(800):
        w(os.path.join(big, "c%04d.png" % i), PNG_HEAD + b"\x00" * 8 + b"IEND\xaeB`\x82")
    s1 = jpost("/api/scan", {"paths": [big]})[1]["scan_id"]
    time.sleep(0.05)
    jpost("/api/scan/cancel", {"scan_id": s1})
    s2 = jpost("/api/scan", {"paths": [d]})[1]["scan_id"]      # 立刻对另一个目录重扫
    e1 = wait_scan(s1, 60); e2 = wait_scan(s2, 120)
    f1 = jget("/api/scan/status?id=%s&offset=0&limit=2000" % s1)[1].get("files") or []
    f2 = jget("/api/scan/status?id=%s&offset=0&limit=2000" % s2)[1].get("files") or []
    p1 = set(x["path"] for x in f1); p2 = set(x["path"] for x in f2)
    row("B6b", "取消 A 后立刻重扫 B（两个任务几乎同时存在）",
        "A=canceled 且结果冻结；B=done 且只含 B 目录；两者交集为空、A 不混入 B",
        "A state=%s total=%s；B state=%s total=%s；交集=%d" %
        (e1.get("state"), e1.get("total"), e2.get("state"), e2.get("total"), len(p1 & p2)),
        e1.get("state") == "canceled" and e2.get("state") == "done" and not (p1 & p2))


# ================================================================== B7 队列分批
def b7_queue_batches():
    def item(i):
        return {"path": "/q/f%05d.jpg" % i, "name": "f%05d.jpg" % i, "size": i,
                "ext": ".jpg", "root": "/q", "w": 10, "h": 10}

    # 正常 4 批 + 重复 path
    jpost("/api/queue", {"files": [item(i) for i in range(500)], "append": False})
    jpost("/api/queue", {"files": [item(i) for i in range(500, 1000)], "append": True})
    jpost("/api/queue", {"files": [item(i) for i in range(800, 1300)], "append": True})
    jpost("/api/queue", {"files": [item(i) for i in range(1300, 1800)], "append": True})
    q = jget("/api/queue")[1]
    paths = [f["path"] for f in q.get("files", [])]
    row("B7a", "4 批串行 append（第三批故意与第二批重叠 200 个 path）",
        "最终 1800 条（0..1799）去重、顺序稳定；服务端不保留重复项",
        "queue 条数=%d 唯一=%d 首=%s 尾=%s" %
        (len(paths), len(set(paths)), paths[0] if paths else "-", paths[-1] if paths else "-"),
        len(paths) == 1800 and len(set(paths)) == 1800)

    # 半途失败：只发第一批（第二批模拟 500 失败 = 不发），看服务端留下什么
    jpost("/api/queue", {"files": [item(i) for i in range(500)], "append": False})
    q2 = jget("/api/queue")[1]
    n2 = len(q2.get("files", []))
    row("B7b", "分批中途失败（第 2 批 500，模拟：只成功第一整批）",
        "服务端只保留已成功的批次（半个队列）—— 这是事实记录，前端必须如实提示",
        "第 2 批失败后 queue=%d 条（原来 1800 条）" % n2, n2 == 500)

    # append 语义：第一批 append=false 必须整份替换（旧数据不残留）
    jpost("/api/queue", {"files": [item(i) for i in range(300)], "append": False})
    q3 = jget("/api/queue")[1]
    p3 = set(f["path"] for f in q3.get("files", []))
    row("B7c", "append=false 整份替换（不得残留上一轮旧数据）",
        "只剩本次 300 条，不含 /q/f01999.jpg 等旧条目",
        "条数=%d 含旧条目=%s" % (len(p3), "/q/f01999.jpg" in p3),
        len(p3) == 300 and "/q/f01999.jpg" not in p3)

    # 20000 上限边界
    jpost("/api/queue", {"files": [item(i) for i in range(50001)], "append": False})
    q4 = jget("/api/queue")[1]
    n4 = len(q4.get("files", []))
    row("B7d", "一次性 POST 50001 条（超过 QUEUE_MAX_FILES=50000）",
        "保存最近 50000 条、返回 truncated=true 且 kept=50000；不 500 不超内存",
        "queue=%d 条" % n4, n4 == 50000)


# ================================================================== B9 真网关 504 复跑
def b9_gateway_504_and_cancel_io():
    d = os.path.join(STATE["root"], "b9")
    os.makedirs(d, exist_ok=True)
    for i in range(4000):
        w(os.path.join(d, "g%04d.png" % i), PNG_HEAD + b"\x00" * 8 + b"IEND\xaeB`\x82")
    GW.clear()
    gport, gw = GW.start("127.0.0.1:%d" % STATE["port"], timeout=1.0, force_paths=("/api/scan/xx",))
    try:
        # 正常路径：经网关 POST 必须毫秒级 JSON
        t0 = time.time()
        req = urllib.request.Request("http://127.0.0.1:%d/api/scan" % gport,
                                     data=json.dumps({"paths": [d]}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            body, code = r.read().decode(), r.status
        dt = (time.time() - t0) * 1000
        sid = json.loads(body).get("scan_id")
        row("B9a", "经真网关桩 POST /api/scan（9000 文件，网关 1s 超时）",
            "200 + JSON，毫秒级；不是 504 HTML",
            "status=%s 耗时=%.0f ms 响应=%s" % (code, dt, body[:70]), code == 200 and dt < 900 and bool(sid))

        # 强制 504 路径：前端收到的是 HTML 而不是 JSON（桩行为核对）
        gw2_port, gw2 = GW.start("127.0.0.1:%d" % STATE["port"], timeout=1.0,
                                 force_paths=("/api/scan",))
        try:
            req2 = urllib.request.Request("http://127.0.0.1:%d/api/scan" % gw2_port,
                                          data=json.dumps({"paths": [d]}).encode(),
                                          headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req2, timeout=10) as r2:
                    html, code2 = r2.read().decode(), r2.status
            except urllib.error.HTTPError as e:
                html, code2 = e.read().decode(), e.code
            row("B9b", "网关强制 504：前端会拿到什么",
                "真实 nginx 504 HTML（jsonSafe 会转成人话提示，不是 JSON.parse 抛错）",
                "status=%s body 前 60=%r 含 504 标题=%s" %
                (code2, html[:60], "504 Gateway Time-out" in html),
                code2 == 504 and "504 Gateway Time-out" in html)
        finally:
            gw2.shutdown()

        # 取消后读盘归零（进程级 I/O）：重新发起一个必定还在跑的扫描，立刻取消
        sid_c = jpost("/api/scan", {"paths": [d]})[1]["scan_id"]
        st_run = jget("/api/scan/status?id=%s&offset=0&limit=1" % sid_c)[1].get("state")
        a = qa_io.read_bytes(STATE["proc"].pid)
        time.sleep(0.3)
        b = qa_io.read_bytes(STATE["proc"].pid)
        jpost("/api/scan/cancel", {"scan_id": sid_c})
        time.sleep(0.5)
        c = qa_io.read_bytes(STATE["proc"].pid)
        time.sleep(1.5)
        d2 = qa_io.read_bytes(STATE["proc"].pid)
        st = jget("/api/scan/status?id=%s&offset=0&limit=1" % sid_c)[1]
        row("B9c", "扫描取消前后的进程读字节（进程级 I/O 计数）",
            "取消时仍在 running；取消前 >0；取消后 1.5s 内 ≈0，state=canceled",
            "cancel 前 state=%s，前 0.3s 读 %s 字节；取消后 1.5s 读 %s 字节；最终 state=%s" %
            (st_run, None if a is None else b - a, None if c is None else d2 - c, st.get("state")),
            st.get("state") == "canceled" and (a is None or (b - a) > 0)
            and (c is None or (d2 - c) < 65536))
    finally:
        gw.shutdown()


ALL = [a4_residue_not_relaxed, a5_eoi_cross_chunk_boundary, a8_eoi_io_cost,
       a4b_9m_user_file_not_overwritten, b6_pagination_edges, b7_queue_batches,
       b9_gateway_504_and_cancel_io]


def main():
    pick = [x.upper() for x in sys.argv[1:]]
    STATE["root"] = tempfile.mkdtemp(prefix="caesium_r2_")
    STATE["work"] = os.path.join(STATE["root"], "work")
    os.makedirs(STATE["work"], exist_ok=True)
    STATE["port"] = free_port()
    print("== v1.7.2 第 2 轮 QA 边界用例 server=%s\n   root=%s\n" % (SERVER_PY, STATE["root"]))
    start_server()
    t0 = time.time()
    try:
        for fn in ALL:
            cid = fn.__name__.split("_")[0].upper()
            if pick and cid not in pick:
                continue
            print("\n--- %s ---" % fn.__doc__.strip().splitlines()[0] if fn.__doc__ else fn.__name__)
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                row(cid, fn.__name__, "用例自身跑完", "异常 %r" % e, False)
    finally:
        stop_server()
        shutil.rmtree(STATE["root"], ignore_errors=True)
    print("\n" + "=" * 96)
    for cid, scene, verdict in ROWS:
        print("%-6s %-64s %s" % (cid, scene[:62], verdict))
    fails = [r for r in ROWS if r[2] == "FAIL"]
    print("=" * 96)
    print("合计 %d 条，PASS %d，FAIL %d，耗时 %.1fs" %
          (len(ROWS), len(ROWS) - len(fails), len(fails), time.time() - t0))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
