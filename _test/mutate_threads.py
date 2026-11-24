#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多线程反向验证：把并行逻辑的关键处逐个改坏，确认 test_threads.py 会变红。

改坏 → 跑测试 → finally 恢复 → 校验 sha256。全部 KILLED 才说明测试真的有效。

用法： python mutate_threads.py
"""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "caesium", "app", "server.py")
SUITE = [sys.executable, os.path.join(HERE, "test_threads.py")]

NL = "\n"

# (说明, 原文, 改坏成, 期望变红的用例关键词)
MUTATIONS = [
    ("多线程改成单线程（只起一个 worker）",
     '            threads = [threading.Thread(target=worker, daemon=True, name="caesium-worker-%d" % i)' + NL +
     '                       for i in range(workers)]',
     '            threads = [threading.Thread(target=worker, daemon=True, name="caesium-worker-%d" % i)' + NL +
     '                       for i in range(1)]',
     "test_parallel_really_concurrent"),

    ("计数不进锁（累加拆出竞态窗口 → 并发丢计数）",
     '                with lock:' + NL +
     '                    t["done"] += 1' + NL +
     '                    if kind == "skipped":',
     '                _v = t["done"]; time.sleep(0.001); t["done"] = _v + 1' + NL +
     '                if True:' + NL +
     '                    if kind == "skipped":',
     "test_counts_conserved_under_parallel"),

    ("取消被忽略（worker 不检查 cancel 标志）",
     '                if t["cancel"] or fatal["exc"] is not None:',
     '                if fatal["exc"] is not None:',
     "test_cancel_stops_workers"),

    ("自动并行度写死为 1（等于没多线程）",
     '        n = min(cpu, 16)' + NL +
     '    n = max(1, min(int(n), 32))',
     '        n = 1' + NL +
     '    n = max(1, min(int(n), 32))',
     "test_workers_env_fallback"),

    ("workers 参与参数指纹（改并行度让已完成失效）",
     'SIG_FIELDS = ("mode", "lossless", "jpeg_quality", "png_quality", "png_opt_level",',
     'SIG_FIELDS = ("workers", "mode", "lossless", "jpeg_quality", "png_quality", "png_opt_level",',
     "test_workers_not_in_signature"),
]


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def run_suite():
    p = subprocess.run(SUITE, capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout + p.stderr).splitlines()


def main():
    orig = open(SERVER, encoding="utf-8").read()
    orig_hash = sha(SERVER)
    print("original sha256 =", orig_hash)
    killed = 0
    for i, (name, old, new, keyword) in enumerate(MUTATIONS, 1):
        cnt = orig.count(old)
        if cnt != 1:
            print("SKIP %d. %s —— 锚点出现 %d 次" % (i, name, cnt))
            continue
        try:
            open(SERVER, "w", encoding="utf-8", newline="").write(orig.replace(old, new))
            rc, tail = run_suite()
        finally:
            open(SERVER, "w", encoding="utf-8", newline="").write(orig)
        ok = rc != 0
        hit = any(keyword in ln for ln in tail if ln.startswith("FAIL "))
        verdict = "KILLED" if ok and hit else ("KILLED(其他用例)" if ok else "SURVIVED")
        if ok:
            killed += 1
        print("%s %d. %s → rc=%d hit=%s" % (verdict, i, name, rc, hit))
        for ln in tail:
            if ln.startswith("FAIL "):
                print("        " + ln)
    restored = sha(SERVER)
    print("\nrestored sha256 = %s %s" % (restored, "OK" if restored == orig_hash else "!!! 恢复失败"))
    print("== 变异 %d 个：KILLED %d ==" % (len(MUTATIONS), killed))
    return 0 if restored == orig_hash and killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
