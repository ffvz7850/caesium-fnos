#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.8.1 修复自验（不依赖 _test/，独立运行）：P0 落盘竞态 + P1-1/P1-2/P1-3。"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "caesium", "app"))
import server as srv  # noqa: E402

FAILS = []


def check(cond, what):
    print(("  PASS " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def scratch():
    d = tempfile.mkdtemp(prefix="caesium181_")
    srv.RESULTS_FILE = os.path.join(d, "results.json")
    srv.RESULTS = {"version": 1, "records": {}}
    srv.RESULTS_DIRTY = 0
    srv.RESULTS_LAST_FLUSH = 0.0
    return d


# ---------------------------------------------------------------- P0
def test_p0_all_records_land():
    print("\n[P0-a] 16 线程 × 50 条 = 800 条，最终全部落盘且全程 JSON 可解析")
    d = scratch()
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                with open(srv.RESULTS_FILE, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if not isinstance(obj.get("records"), dict):
                    errors.append("bad shape")
            except FileNotFoundError:
                pass
            except PermissionError:
                # Windows 不允许 rename 覆盖正被别的线程打开的文件；fnOS(Linux) 允许。
                # 这是本机验证平台的差异，不算 JSON 损坏。
                pass
            except Exception as e:
                errors.append("%s: %s" % (type(e).__name__, e))

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    try:
        def writer(k):
            for i in range(50):
                srv.record_result("/p/%d-%03d.jpg" % (k, i),
                                  {"status": "ok", "sig": "abcd", "ts": i})

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        stop.set()
        rt.join(5)
    srv.flush_results(force=True)   # 先停并发读线程，避免 Windows rename 权限干扰
    with open(srv.RESULTS_FILE, "r", encoding="utf-8") as f:
        disk = json.load(f)
    n_disk = len(disk["records"])
    n_mem = len(srv.get_results())
    check(n_disk == 800, "磁盘记录数=800（实际 %d）" % n_disk)
    check(n_mem == 800, "内存记录数=800（实际 %d）" % n_mem)
    check(not errors, "并发读取期间无 JSON 损坏（错误 %d 条: %s）" % (len(errors), errors[:3]))
    check(not [x for x in os.listdir(d) if ".tmp." in x], "无残留 uuid 临时文件")
    shutil.rmtree(d, ignore_errors=True)


def test_p0_clear_race():
    print("\n[P0-b] record + clear + force flush 并发：JSON 始终可解析，内存与磁盘最终一致")
    d = scratch()
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                with open(srv.RESULTS_FILE, "r", encoding="utf-8") as f:
                    json.load(f)
            except FileNotFoundError:
                pass
            except PermissionError:
                pass    # Windows 平台差异，见 P0-a 注释
            except Exception as e:
                errors.append("%s: %s" % (type(e).__name__, e))

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()

    def writer(k):
        for i in range(60):
            srv.record_result("/q/%d-%03d.jpg" % (k, i), {"status": "ok", "sig": "zz"})
            if i % 17 == 0:
                srv.flush_results(force=True)

    def clearer():
        for _ in range(30):
            time.sleep(0.005)
            srv.clear_results()

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(8)]
    threads.append(threading.Thread(target=clearer))
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        stop.set()
        rt.join(5)
    srv.flush_results(force=True)
    with open(srv.RESULTS_FILE, "r", encoding="utf-8") as f:
        disk = json.load(f)["records"]
    check(not errors, "全程 JSON 可解析（错误 %d 条: %s）" % (len(errors), errors[:3]))
    check(disk == srv.get_results(), "内存与磁盘记录完全一致（disk=%d mem=%d）" %
          (len(disk), len(srv.get_results())))
    shutil.rmtree(d, ignore_errors=True)


def test_p0_flush_failure_keeps_dirty():
    print("\n[P0-c] 写盘失败保留脏标记并记日志，恢复后可重试成功")
    d = scratch()
    # 把 results.json 自己变成一个目录 → os.replace 失败，但同目录下的 .flush.log 可写
    os.makedirs(srv.RESULTS_FILE)
    srv.record_result("/r/fail.jpg", {"status": "ok", "sig": "ff"})
    check(srv.RESULTS_DIRTY > 0, "失败后 DIRTY 仍 >0（实际 %d）" % srv.RESULTS_DIRTY)
    check(os.path.isfile(srv.RESULTS_FILE + ".flush.log"), "失败日志已写入")
    os.rmdir(srv.RESULTS_FILE)
    srv.flush_results(force=True)
    with open(srv.RESULTS_FILE, "r", encoding="utf-8") as f:
        disk = json.load(f)
    check(len(disk["records"]) == 1, "恢复路径后重试落盘成功")
    check(srv.RESULTS_DIRTY == 0, "成功后 DIRTY 清零")
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- P1-1
def test_p11_workers():
    print("\n[P1-1] workers=0（自动）回退环境变量；显式正数优先")
    old = os.environ.get("CAESIUM_WORKERS")
    try:
        os.environ["CAESIUM_WORKERS"] = "3"
        check(srv.resolve_workers({"workers": 0}, 10) == 3, "workers=0 → env 3")
        check(srv.resolve_workers({"workers": "0"}, 10) == 3, "workers='0' → env 3")
        check(srv.resolve_workers({}, 10) == 3, "workers 缺省 → env 3")
        check(srv.resolve_workers({"workers": 8}, 10) == 8, "workers=8 优先于 env 3")
        check(srv.resolve_workers({"workers": -1}, 10) == 3, "非法负数 → env 3")
    finally:
        if old is None:
            os.environ.pop("CAESIUM_WORKERS", None)
        else:
            os.environ["CAESIUM_WORKERS"] = old


# ---------------------------------------------------------------- P1-2 / P1-3
def test_p12_cancel_inflight():
    print("\n[P1-2] 取消可终止在途 Popen 引擎，任务收敛到 canceled，无僵尸")
    started = time.time()
    done = []

    def run():
        srv._bind_task_context({"id": "cancel-test"})
        try:
            proc, err = srv._run_clt([sys.executable, "-c", "import time; time.sleep(60)"])
            done.append(proc)
        finally:
            srv._clear_task_context()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while time.time() < deadline and "cancel-test" not in srv._TASK_PROCS:
        time.sleep(0.02)
    registered = "cancel-test" in srv._TASK_PROCS
    hit = srv.cancel_task_processes("cancel-test")
    th.join(15)
    elapsed = time.time() - started
    check(registered, "在途进程已登记到任务")
    check(hit >= 1, "cancel 命中进程")
    check(not th.is_alive(), "worker 已从 communicate 返回（耗时 %.2fs < 60s）" % elapsed)
    check(elapsed < 15, "取消后快速收敛（%.2fs）" % elapsed)
    check(len(done) == 1 and done[0].returncode not in (0, None), "被终止进程返回非零退出码")
    check(not srv._TASK_PROCS.get("cancel-test"), "进程登记表已清空")


def test_p13_process_budget():
    print("\n[P1-3] 全局进程预算限制跨任务在途引擎总数")
    budget = srv.PROCESS_BUDGET_SIZE
    peak = {"n": 0}
    stop = threading.Event()
    sentinel = object()

    def monitor():
        while not stop.is_set():
            with srv._TASK_PROCS_LOCK:
                total = sum(len(v) for v in srv._TASK_PROCS.values())
            peak["n"] = max(peak["n"], total)
            time.sleep(0.01)

    mt = threading.Thread(target=monitor, daemon=True)
    mt.start()

    def one(k):
        srv._bind_task_context({"id": "budget-%d" % k})
        try:
            srv._run_clt([sys.executable, "-c", "import time; time.sleep(0.4)"])
        finally:
            srv._clear_task_context()

    threads = [threading.Thread(target=one, args=(k,), daemon=True) for k in range(28)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        stop.set()
        mt.join(3)
    check(peak["n"] <= budget, "在途进程峰值 %d ≤ 预算 %d" % (peak["n"], budget))
    check(peak["n"] >= 2, "确有并发（峰值 %d）" % peak["n"])
    check(all(not t.is_alive() for t in threads), "全部 worker 已退出")
    check(srv.PROCESS_BUDGET._value == budget, "信号量已全部归还")


if __name__ == "__main__":
    test_p0_all_records_land()
    test_p0_clear_race()
    test_p0_flush_failure_keeps_dirty()
    test_p11_workers()
    test_p12_cancel_inflight()
    test_p13_process_budget()
    print("\n== 1.8.1 自验：%d failed ==" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1 if FAILS else 0)
