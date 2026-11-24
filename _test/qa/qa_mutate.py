#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA 自己的变异抽查（独立于工程师的 mutate.py）。

目的：证明工程师的测试套件"不是空转"—— 手改坏一处关键分支，测试必须变红；
同时找出套件**没覆盖**的分支（改坏后仍然全绿），以此说明覆盖盲区在哪里。

安全：每次变异前把 server.py 备份到 qa/artifacts/，结束后比对 sha256 并还原。

用法：python qa_mutate.py
"""
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(TEST_DIR)
SERVER = os.path.join(ROOT, "caesium", "app", "server.py")
BACKUP = os.path.join(HERE, "artifacts", "server.py.qa-backup")

UNIT = [sys.executable, os.path.join(TEST_DIR, "test_unit.py")]
HTTP = [sys.executable, os.path.join(TEST_DIR, "test_http.py")]

CASES = [
    ("M1 skip 判定不再检查文件大小（0 字节残骸也当已完成）",
     "if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:",
     "if os.path.isfile(out_path):",
     UNIT,
     "期望 test_skip_policy_ignores_empty_file 变红"),

    ("M2 run_task 去掉「这是我们上次压的」判断（改参数不再重压）",
     'if (force_all or has_record(src)) and normalize_exist_policy(options) != "rename":',
     'if force_all and normalize_exist_policy(options) != "rename":',
     UNIT,
     "期望 test_changed_quality_forces_recompress 变红"),

    ("M3 结果记录里的 error 也算已完成",
     'if rec.get("status") not in ("ok", "skipped"):',
     'if False:',
     UNIT,
     "期望 test_result_is_done_rules 变红"),

    ("M4 rename 的原子占位失去独占性（并发会互相覆盖）",
     "fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)",
     "fd = os.open(path, os.O_CREAT | os.O_WRONLY)",
     UNIT,
     "若仍全绿 → 套件完全没有覆盖「同一输出名并发」的场景（QA 的 C07 用例能抓到）"),

    ("M5 反向变异：给 compress_file 补上「引擎非零退出即失败」的判断",
     "    proc, launch_err = _run_clt(cmd)\n    if proc is None:\n        return False, None, launch_err, \"error\"",
     "    proc, launch_err = _run_clt(cmd)\n    if proc is None:\n        return False, None, launch_err, \"error\"\n"
     "    if proc.returncode != 0:\n        return False, None, _clt_error_text(proc), \"error\"",
     UNIT,
     "若仍全绿 → 套件里没有任何用例约束退出码语义（QA 的 C11 用例证明这是真漏洞）"),
]


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, cwd=ROOT)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    head = [ln for ln in out.splitlines() if ln.startswith("FAIL ") or ln.startswith("== ")]
    return proc.returncode, head[-4:]


def main():
    base = sha(SERVER)
    BAK = os.path.dirname(BACKUP)
    if not os.path.isdir(BAK):
        os.makedirs(BAK, exist_ok=True)
    shutil.copy2(SERVER, BACKUP)
    print("baseline sha256 = %s（已备份到 %s）\n" % (base, BACKUP))
    rows = []
    try:
        for name, old, new, suite, why in CASES:
            with open(SERVER, "r", encoding="utf-8", newline="") as f:
                src = f.read()
            if src.count(old) != 1:
                print("SKIP  %s —— 锚点出现 %d 次" % (name, src.count(old)))
                rows.append((name, "ANCHOR-MISS", why))
                continue
            try:
                with open(SERVER, "w", encoding="utf-8", newline="") as f:
                    f.write(src.replace(old, new))
                rc, tail = run(suite)
            finally:
                with open(SERVER, "w", encoding="utf-8", newline="") as f:
                    f.write(src)
            verdict = "KILLED  " if rc != 0 else "SURVIVED"
            print("%s %s\n        %s" % (verdict, name, " / ".join(tail)))
            rows.append((name, verdict.strip(), why))
    finally:
        shutil.copy2(BACKUP, SERVER)
    after = sha(SERVER)
    print("\n还原后 sha256 = %s  %s" % (after, "OK" if after == base else "!!! 还原失败"))
    print("\n" + "=" * 96)
    for name, verdict, why in rows:
        print("[%-9s] %s\n            %s" % (verdict, name, why))
    print("=" * 96)
    print("KILLED %d / SURVIVED %d" %
          (len([r for r in rows if r[1] == "KILLED"]), len([r for r in rows if r[1] == "SURVIVED"])))
    return 0 if after == base else 3


if __name__ == "__main__":
    sys.exit(main())
