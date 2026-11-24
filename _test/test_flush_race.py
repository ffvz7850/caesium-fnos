#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 回归：并发 record_result / flush_results 时 results.json 的落盘完整性。

旧实现把 ``json.dumps`` 快照放在 RESULTS_LOCK 内，却把「写 .tmp + os.replace」放在锁外：
多个线程会同时写同一个 .tmp 并互相覆盖，轻则用一个旧快照盖掉期间新加入的记录
（16 线程 x 800 条时旧代码只落 791 条），重则把尚未写完的 .tmp replace 成 results.json，
重启后 json.load 直接炸。这里刻意不做最终 force flush，专门压测正常节流路径。
"""
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 可用 CAESIUM_SERVER_DIR 指向另一份 server.py（如 1.8.0 旧版）做对比回归
APP = os.environ.get("CAESIUM_SERVER_DIR") or os.path.join(ROOT, "caesium", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import harness as H  # noqa: E402
import server as srv  # noqa: E402

N_THREADS = 16
N_RECORDS = 800


def _setup():
    """独立临时 workdir + 重置 results 全局状态（不触碰磁盘上的产品文件）。"""
    root = tempfile.mkdtemp(prefix="caesium_flush_race_")
    work = os.path.join(root, "work")
    os.makedirs(work)
    srv.RESULTS_FILE = os.path.join(work, "results.json")
    srv.RESULTS = {"version": 1, "records": {}}
    srv.RESULTS_DIRTY = 0
    srv.RESULTS_LAST_FLUSH = 0.0
    return root


def _disk_records(path):
    """读 results.json，返回 (是否可解析, records 字典或 None, 错误信息)。"""
    if not os.path.isfile(path):
        return False, None, "文件尚未生成"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, None, str(e)
    if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
        return False, None, "结构不是 {version, records:{...}}"
    return True, data["records"], ""


def test_flush_race_json_is_complete_and_records_not_lost():
    """16 线程 x 800 条：磁盘 JSON 必须完整可解析，且记录数不低于内存快照的合理下界。"""
    root = _setup()
    try:
        def writer(k):
            for j in range(N_RECORDS // N_THREADS):
                path = "/vol/race_%02d_%03d.jpg" % (k, j)
                srv.record_result(path, {
                    "status": "ok", "out": path, "sig": "race", "ts": 1,
                    "note": "x" * 512,   # 放大序列化/写盘窗口，让锁外写盘的竞态稳定暴露
                })

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(N_THREADS)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        H.is_true(all(not th.is_alive() for th in threads), "写线程必须在 60s 内结束")
        # 注意：这里刻意不调用 flush_results(force=True)，走真实节流路径。

        mem = srv.get_results()
        H.eq(len(mem), N_RECORDS, "内存应完整收下 800 条（内存写入在 RESULTS_LOCK 内，不许丢）")

        ok, disk, err = _disk_records(srv.RESULTS_FILE)
        H.is_true(ok, "results.json 必须是完整可解析的 JSON，实际错误：%s" % err)
        # 节流规则（RESULT_FLUSH_EVERY=5）：没做最终 force flush 时，磁盘最多比内存
        # 少 4 条（正在攒的那批）。少得更多 = 并发写盘用旧快照把新记录冲掉了。
        lower = len(mem) - (srv.RESULT_FLUSH_EVERY - 1)
        H.is_true(len(disk) >= lower,
                  "磁盘记录 %d 低于合理下界 %d（内存 %d 条）—— 并发落盘把记录冲丢了"
                  % (len(disk), lower, len(mem)))
        H.is_true(not os.path.isfile(srv.RESULTS_FILE + ".tmp"), "不许残留 .tmp 中间文件")
        print("     [实测] 内存 %d 条 / 磁盘 %d 条 / 下界 %d" % (len(mem), len(disk), lower))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_clear_race_keeps_json_parseable():
    """一边并发 record、一边 clear 和读盘：磁盘上的 results.json 永远不能被读到半截。"""
    root = _setup()
    errors = []
    stop = threading.Event()

    def writer():
        for i in range(300):
            try:
                srv.record_result("/vol/c_%03d.jpg" % i, {
                    "status": "ok", "sig": "race", "ts": 1, "note": "y" * 1024})
            except Exception as e:
                errors.append(("record", repr(e)))

    def clearer():
        for _ in range(30):
            try:
                srv.clear_results()
            except Exception as e:
                errors.append(("clear", repr(e)))

    def reader():
        while not stop.is_set():
            if not os.path.isfile(srv.RESULTS_FILE):
                continue
            try:
                with open(srv.RESULTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except OSError as e:
                # Windows 上 os.replace 的瞬间目标文件会被独占，open 抛 13/WinError 32，
                # 这是正常并发窗口，不是半截 JSON（原子替换本身保证了完整性）。
                if (getattr(e, "errno", None) == 13
                        or getattr(e, "winerror", None) in (32, 33)):
                    continue
                errors.append(("read", repr(e)))
                return
            except Exception as e:
                errors.append(("read", repr(e)))   # JSONDecodeError 等 = 真损坏
                return
            if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
                errors.append(("read", "结构不是 {version, records:{...}}"))
                return

    try:
        rd = threading.Thread(target=reader)
        rd.start()
        workers = ([threading.Thread(target=writer) for _ in range(4)]
                   + [threading.Thread(target=clearer) for _ in range(2)])
        for th in workers:
            th.start()
        for th in workers:
            th.join(60)
        stop.set()
        rd.join(10)
        H.eq(errors, [], "并发 record/clear/读盘时不许出现半截 JSON 或异常：%s" % errors[:3])
    finally:
        stop.set()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    H.finish(globals())
