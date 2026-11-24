#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 回归单测：全局进程预算有上限、取消只终止本任务的在途引擎、进程句柄必被回收。

不依赖真实 caesiumclt：用可控 FakeProc + monkeypatch subprocess.Popen 驱动生产路径
（保持 subprocess.run 为原始函数，才能走 _run_clt 的 Popen 分支）。
"""
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.environ.get("CAESIUM_SERVER_DIR") or os.path.join(ROOT, "caesium", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import harness as H  # noqa: E402
import server as srv  # noqa: E402


class FakeProc(object):
    """可控的进程替身：记录 terminate/kill/wait，可模拟长时间 communicate。"""

    def __init__(self, block=False):
        self.terminated = 0
        self.killed = 0
        self.waited = 0
        self.returncode = None
        self._block = block
        self._done = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if self.returncode is None:
            self.returncode = -15
        self._done.set()

    def kill(self):
        self.killed += 1
        if self.returncode is None:
            self.returncode = -9
        self._done.set()

    def communicate(self, timeout=None):
        if self._block:
            self._done.wait(timeout or 5)
        if self.returncode is None:
            self.returncode = 0
        return "out", ""

    def wait(self):
        self.waited += 1
        return self.returncode


def test_process_budget_is_bounded():
    """全局在途引擎预算必须是有限值（默认 min(CPU,16)），且不能超额 acquire。"""
    size = srv.PROCESS_BUDGET_SIZE
    H.is_true(1 <= size <= 16, "全局进程预算应在 1..16，实际 %d" % size)
    held = []
    try:
        for _ in range(size):
            if srv.PROCESS_BUDGET.acquire(blocking=False):
                held.append(1)
        H.eq(len(held), size, "应能占满预算 %d 个槽位" % size)
        H.is_true(not srv.PROCESS_BUDGET.acquire(blocking=False),
                  "超过预算的 acquire 必须失败（BoundedSemaphore 上限）")
    finally:
        for _ in held:
            srv.PROCESS_BUDGET.release()


def test_register_unregister_task_proc():
    """在途进程按 task_id 登记；最后一个移除后该任务条目应销毁。"""
    p = FakeProc()
    srv._register_task_proc("tx", p)
    with srv._TASK_PROCS_LOCK:
        H.eq(len(srv._TASK_PROCS.get("tx", ())), 1, "应登记 1 个在途进程")
    srv._unregister_task_proc("tx", p)
    with srv._TASK_PROCS_LOCK:
        H.is_true("tx" not in srv._TASK_PROCS, "最后一个进程移除后任务条目应删除")


def test_cancel_only_terminates_target_task():
    """取消任务 A 只 terminate A 的在途引擎，绝不波及并发的任务 B。"""
    a, b, c = FakeProc(), FakeProc(), FakeProc()
    srv._register_task_proc("t1", a)
    srv._register_task_proc("t1", b)
    srv._register_task_proc("t2", c)
    try:
        H.eq(srv.cancel_task_processes("t1"), 2, "应终止 t1 的 2 个在途进程")
        H.eq((a.terminated, b.terminated, c.terminated), (1, 1, 0),
             "只许终止 t1 的进程，t2 的必须原封不动")
        H.eq(c.poll(), None, "被波及的 t2 进程不应被改状态")
        H.eq(srv.cancel_task_processes("no-such-task"), 0, "未知任务应返回 0")
    finally:
        srv._drop_task_procs("t1")
        srv._drop_task_procs("t2")


def test_run_clt_registers_and_reaps_process():
    """生产路径：Popen 出来的句柄先登记，communicate 结束必须 wait 回收并从登记表移除。"""
    old_popen = srv.subprocess.Popen
    old_run = srv.subprocess.run
    proc = FakeProc()
    try:
        srv.subprocess.run = srv._ORIGINAL_SUBPROCESS_RUN   # 强制走 Popen 生产路径
        srv.subprocess.Popen = lambda cmd, **kw: proc
        srv._bind_task_context({"id": "t3"})
        result, err = srv._run_clt(["caesiumclt", "a.jpg"])
        H.eq(err, "")
        H.eq(result.returncode, 0, "CompletedProcess 应带回引擎退出码")
        H.eq(proc.waited, 1, "必须 wait 回收子进程，杜绝僵尸")
        with srv._TASK_PROCS_LOCK:
            H.is_true("t3" not in srv._TASK_PROCS, "结束后必须从登记表移除")
    finally:
        srv.subprocess.Popen = old_popen
        srv.subprocess.run = old_run
        srv._clear_task_context()
        srv._drop_task_procs("t3")


def test_cancel_terminates_inflight_engine():
    """取消时引擎还卡在 communicate：必须能 terminate 它，而不是等 3600s 超时。"""
    old_popen = srv.subprocess.Popen
    old_run = srv.subprocess.run
    started = threading.Event()
    proc = FakeProc(block=True)
    errors = []

    def fake_popen(cmd, **kw):
        started.set()
        return proc

    def runner():
        try:
            srv._bind_task_context({"id": "t4"})   # threading.local，必须在 worker 线程里绑
            srv._run_clt(["caesiumclt", "slow.jpg"])
        except Exception as e:
            errors.append(repr(e))

    try:
        srv.subprocess.run = srv._ORIGINAL_SUBPROCESS_RUN
        srv.subprocess.Popen = fake_popen
        th = threading.Thread(target=runner)
        th.start()
        H.is_true(started.wait(10), "引擎应在 10s 内启动")
        # 给 _register_task_proc 一点时间进入登记表
        deadline = time.time() + 5
        while time.time() < deadline:
            with srv._TASK_PROCS_LOCK:
                if srv._TASK_PROCS.get("t4"):
                    break
            time.sleep(0.01)
        H.eq(srv.cancel_task_processes("t4"), 1, "应终止 1 个在途引擎")
        H.is_true(proc.terminated >= 1, "在途引擎必须收到 terminate，不能干等超时")
        th.join(10)
        H.is_true(not th.is_alive(), "被取消的引擎线程应尽快退出")
        H.eq(errors, [], "_run_clt 不应抛异常：%s" % errors)
    finally:
        srv.subprocess.Popen = old_popen
        srv.subprocess.run = old_run
        srv._clear_task_context()
        srv._drop_task_procs("t4")


if __name__ == "__main__":
    H.finish(globals())
