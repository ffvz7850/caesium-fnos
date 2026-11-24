#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞牛版多线程压缩测试（进程内 monkeypatch subprocess.run，不执行真引擎）。

核心：证明"真的并发"，而不是"代码看起来并行"——用一个会 sleep 的 FakeRun
记录同时在跑的调用数峰值（worker 数），workers=1 时峰值必须为 1。

覆盖：并发峰值、计数守恒、失败隔离、取消、workers 边界与环境变量回退。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 可用 CAESIUM_SERVER_DIR 指向另一份 server.py（如 1.8.0 旧版）做对比回归
APP = os.environ.get("CAESIUM_SERVER_DIR") or os.path.join(os.path.dirname(HERE), "caesium", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import harness as H  # noqa: E402
import server as srv  # noqa: E402

_spec = importlib.util.spec_from_file_location("fake_clt", os.path.join(HERE, "fake_clt.py"))
fake_clt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_clt)


class FakeProc(object):
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class ConcurrentRun(object):
    """替代 subprocess.run：可观测并发峰值；每个调用 sleep 一小段制造重叠窗口。"""

    def __init__(self, sleep=0.05, fail_contains="", hold_cancel=None):
        self.calls = []
        self.peak = 0
        self._cur = 0
        self._lock = threading.Lock()
        self.sleep = sleep
        self.fail_contains = fail_contains
        self.hold_cancel = hold_cancel      # 可选：每次进入时回调（用于中途触发 cancel）

    def __call__(self, cmd, **kwargs):
        parsed = fake_clt.parse_args([str(x) for x in cmd[1:]])
        src = parsed["src"]
        with self._lock:
            self._cur += 1
            self.peak = max(self.peak, self._cur)
            self.calls.append(list(cmd))
        try:
            if self.hold_cancel:
                self.hold_cancel()
            time.sleep(self.sleep)
            if self.fail_contains and self.fail_contains in src:
                return FakeProc(rc=3, err="forced failure")
            if not src or not os.path.isfile(src):
                return FakeProc(rc=2, err="source not found")
            out_dir = parsed["out_dir"] or os.path.dirname(src)
            os.makedirs(out_dir, exist_ok=True)
            ext = os.path.splitext(src)[1].lower()
            if parsed["fmt"]:
                ext = fake_clt.EXT_MAP.get(str(parsed["fmt"]).lower(), ext)
            base = os.path.splitext(os.path.basename(src))[0]
            target = os.path.join(out_dir, base + parsed["suffix"] + ext)
            if not os.path.isfile(target):
                with open(target, "wb") as f:
                    f.write(b"FAKE" + b"x" * 300)
            return FakeProc(rc=0, out="fake ok")
        finally:
            with self._lock:
                self._cur -= 1


def base_opts(**kw):
    opts = {"mode": "quality", "jpeg_quality": 90, "png_quality": 90, "png_opt_level": 3,
            "webp_quality": 90, "tiff_algorithm": "deflate", "chroma": "auto",
            "progressive": True, "zopfli": False, "keep_exif": True, "suffix": "", "format": "keep",
            "keep_dates": True, "long_edge": 0, "width": 0, "height": 0, "percent": 0,
            "no_upscale": False, "max_size_kb": 200, "exist_policy": "overwrite"}
    opts.update(kw)
    return opts


def make_files(n, d):
    files = []
    for i in range(n):
        p = os.path.join(d, "f%03d.jpg" % i)
        with open(p, "wb") as f:
            f.write(b"\xff\xd8\xff\xd9" + b"x" * 100)
        files.append({"path": p, "root": d})
    return files


def run_task(files, out, opts):
    t = srv.new_task(files, out, opts, False)
    srv.run_task(t)
    return t


# ---------------------------------------------------------------- 测试
def test_parallel_really_concurrent():
    """workers=4 时并发峰值应 ≥2（证明不是串行）。"""
    d = tempfile.mkdtemp()
    try:
        files = make_files(8, d)
        out = os.path.join(d, "out")
        run = ConcurrentRun(sleep=0.06)
        srv.subprocess.run = run
        t = run_task(files, out, base_opts(workers=4))
        assert t["state"] == "done", t.get("error")
        assert run.peak >= 2, "并发峰值=%d，说明还是串行" % run.peak
        assert t["workers"] == 4
        assert t["ok"] == 8, (t["ok"], t["fail"])
        H.eq(t["done"], t["total"], "done 计数")
        print("   peak=%d workers=%d" % (run.peak, t["workers"]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_single_worker_is_serial():
    """workers=1 时并发峰值必须=1。"""
    d = tempfile.mkdtemp()
    try:
        files = make_files(4, d)
        out = os.path.join(d, "out")
        run = ConcurrentRun(sleep=0.02)
        srv.subprocess.run = run
        t = run_task(files, out, base_opts(workers=1))
        H.eq(t["state"], "done")
        H.eq(run.peak, 1, "workers=1 并发峰值")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_counts_conserved_under_parallel():
    """高并发下计数守恒：total == ok+copied+skipped+fail，且 done==total。

    刻意把 _process_item 包一层 sleep(0)（读-改-写之间让出 GIL），放大"无锁累加"的
    竞态窗口：这样即使把计数移出锁（变异），本用例也必然丢计数而变红。
    """
    d = tempfile.mkdtemp()
    orig_process = srv._process_item
    def slow_process(*a, **kw):
        time.sleep(0)  # 让出 GIL，放大无锁 read-modify-write 的竞态
        return orig_process(*a, **kw)
    try:
        files = make_files(60, d)
        out = os.path.join(d, "out")
        run = ConcurrentRun(sleep=0.001)
        srv.subprocess.run = run
        srv._process_item = slow_process
        t = run_task(files, out, base_opts(workers=16))
        H.eq(t["state"], "done")
        H.eq(t["done"], t["total"], "done")
        H.eq(t["total"], t["ok"] + t.get("copied", 0) + t["skipped"] + t["fail"], "守恒")
        H.eq(len(t["results"]), t["total"], "results 条数")
    finally:
        srv._process_item = orig_process
        shutil.rmtree(d, ignore_errors=True)


def test_one_failure_isolated():
    """并行时单个文件失败不影响其他文件。"""
    d = tempfile.mkdtemp()
    try:
        files = make_files(6, d)
        out = os.path.join(d, "out")
        fail_name = os.path.basename(files[2]["path"])
        run = ConcurrentRun(sleep=0.01, fail_contains=fail_name)
        srv.subprocess.run = run
        t = run_task(files, out, base_opts(workers=4))
        H.eq(t["state"], "done", "整体不应因单文件失败而 error")
        H.eq(t["fail"], 1, "失败数")
        H.eq(t["ok"], 5, "成功数")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cancel_stops_workers():
    """中途 cancel 后任务状态为 canceled，且不会处理完全部文件。"""
    d = tempfile.mkdtemp()
    try:
        files = make_files(30, d)
        out = os.path.join(d, "out")
        t_holder = {}
        state = {"n": 0}

        def maybe_cancel():
            state["n"] += 1
            if state["n"] == 2:
                t_holder["t"]["cancel"] = True

        run = ConcurrentRun(sleep=0.01, hold_cancel=maybe_cancel)
        srv.subprocess.run = run
        t = srv.new_task(files, out, base_opts(workers=2), False)
        t_holder["t"] = t
        srv.run_task(t)
        H.eq(t["state"], "canceled", "取消状态")
        assert t["done"] < t["total"], "取消后不应处理完全部文件"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_workers_bounds():
    """workers 边界：≤0 / 非数字 = 自动（按文件数夹住）；超过文件数被夹住。"""
    cases = [(-5, 3, 3), (99, 3, 3), (0, 0, 1), ("abc", 1, 1)]
    for raw, nfiles, expect in cases:
        got = srv.resolve_workers({"workers": raw}, nfiles)
        H.eq(got, expect, "workers=%r nfiles=%s" % (raw, nfiles))


def test_workers_env_fallback():
    """没传 workers 时读环境变量；自动值必须真的等于逻辑核数（封顶 16）。

    变异 #4 把自动分支写死成 1；原来只断言 1<=auto<=16 会把它放行（错配 KILLED）。
    这里用受控的 os.cpu_count()=8 让期望值确定，CI 上核数不同也不会假红/假绿。
    """
    old_env = os.environ.get("CAESIUM_WORKERS")
    old_cpu = os.cpu_count
    try:
        os.cpu_count = lambda: 8
        os.environ["CAESIUM_WORKERS"] = "3"
        H.eq(srv.resolve_workers({}, 10), 3, "环境变量并行度")
        os.environ["CAESIUM_WORKERS"] = "0"
        auto = srv.resolve_workers({}, 10)
        H.eq(auto, 8, "自动值应等于逻辑核数（受控 8，封顶 16），实际 %d" % auto)
    finally:
        os.cpu_count = old_cpu
        if old_env is None:
            os.environ.pop("CAESIUM_WORKERS", None)
        else:
            os.environ["CAESIUM_WORKERS"] = old_env


def test_workers_zero_falls_back_to_env():
    """P1：前端下拉「自动」传 workers=0，此时环境变量 CAESIUM_WORKERS 仍应生效。

    旧实现把显式的 0 当成「自动」直接忽略 env（受控 8 核、nfiles=10 时返回 8），
    导致 NAS 上设置 CAESIUM_WORKERS 根本不起作用；期望是回退到 env=3。
    """
    old_env = os.environ.get("CAESIUM_WORKERS")
    old_cpu = os.cpu_count
    try:
        os.cpu_count = lambda: 8
        os.environ["CAESIUM_WORKERS"] = "3"
        H.eq(srv.resolve_workers({"workers": 0}, 10), 3,
             "workers=0 应视为未指定并回退到 CAESIUM_WORKERS=3")
        H.eq(srv.resolve_workers({"workers": "0"}, 10), 3,
             "workers='0' 字符串同样应回退到环境变量")
    finally:
        os.cpu_count = old_cpu
        if old_env is None:
            os.environ.pop("CAESIUM_WORKERS", None)
        else:
            os.environ["CAESIUM_WORKERS"] = old_env


def test_workers_not_in_signature():
    """workers 不参与参数指纹：改并行度不应让已完成文件失效。"""
    a = srv.results_signature(base_opts(workers=1))
    b = srv.results_signature(base_opts(workers=8))
    H.eq(a, b, "workers 不应改变参数指纹")


if __name__ == "__main__":
    H.finish(globals())
