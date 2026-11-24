#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键跑全部验证：一致性 → 单元 → 多线程/落盘/取消 → 真 HTTP → 前端 → 变异 → 打包。

用法：
    python run_all.py            # 全跑（含变异与打包，约 3-5 分钟）
    python run_all.py --fast     # 跳过变异与打包

说明：
- 多线程三件套（test_threads / test_flush_race / test_cancel_budget）已并入 STEPS，
  失败即整套红——避免"文档列了验证记录、一键入口却不跑"的盲区。
- mutate_threads.py 会在原地改 server.py 再恢复；这里统一用**临时副本**跑，产品代码零风险。
- smoke_mt_pkg.py 依赖预解包目录 _review/_smoke/target，需先手动解包 fpk 就位，
  不适合放进一键入口，请单独准备环境后运行： python smoke_mt_pkg.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, "dist")
FNPACK = "/d/PCAPP/DBAO/GONGJU/fnpack.exe"
PROJECT = os.path.join(ROOT, "caesium")
PY = sys.executable

STEPS = [
    ("全局一致性审查（id/接口/签名/计数/版本）", [PY, os.path.join(HERE, "test_consistency.py")]),
    ("后端单元测试（sig / 三态策略 / 断点续传 / 异常兜底）", [PY, os.path.join(HERE, "test_unit.py")]),
    ("多线程并发单测（并行峰值 / 计数守恒 / 失败隔离 / 取消 / 环境变量回退）",
     [PY, os.path.join(HERE, "test_threads.py")]),
    ("P0 并发落盘完整性（16 线程 x 800 条，不 force flush）",
     [PY, os.path.join(HERE, "test_flush_race.py")]),
    ("P1 取消中断 / 全局进程预算（mock Popen，不依赖真引擎）",
     [PY, os.path.join(HERE, "test_cancel_budget.py")]),
    ("真 HTTP 集成测试（TCP + 假引擎 + kill -9 重启续传）", [PY, os.path.join(HERE, "test_http.py")]),
    ("前端 jsdom 测试（状态文案 / 按钮文案 / 参数清空 / 自绘确认）",
     ["node", os.path.join(HERE, "test_frontend.js")]),
]
EXTRA = [
    ("变异测试（把关键分支改坏，确认测试会红）", [PY, os.path.join(HERE, "mutate.py")]),
    ("打包并校验 fpk", [PY, os.path.join(HERE, "test_package.py")]),
]


def _ensure_node_path():
    """前端测试依赖 jsdom；优先用 _test/_jsdom_env 里的隔离安装，避免污染全局。"""
    local = os.path.join(HERE, "_jsdom_env", "node_modules")
    if os.path.isdir(local):
        cur = os.environ.get("NODE_PATH", "")
        os.environ["NODE_PATH"] = local + (os.pathsep + cur if cur else "")


def run(label, cmd):
    print("\n" + "=" * 78)
    print(">>> %s" % label)
    print("    " + " ".join(cmd))
    print("=" * 78)
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def run_mutate_threads_isolated():
    """在临时副本上跑 mutate_threads.py，绝不原地改产品 caesium/app/server.py。

    mutate 脚本本身在 try/finally 里恢复并校验 sha256；这里再套一层隔离目录，
    即使中途异常/被强杀也不会污染产品代码。
    """
    sandbox = tempfile.mkdtemp(prefix="caesium_mutate_")
    try:
        app_dir = os.path.join(sandbox, "caesium", "app")
        test_dir = os.path.join(sandbox, "_test")
        os.makedirs(app_dir)
        os.makedirs(test_dir)
        shutil.copy(os.path.join(PROJECT, "app", "server.py"), os.path.join(app_dir, "server.py"))
        for name in ("mutate_threads.py", "test_threads.py", "harness.py", "fake_clt.py", "fake_clt.cmd"):
            src = os.path.join(HERE, name)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(test_dir, name))
        print("\n" + "=" * 78)
        print(">>> 多线程变异测试（隔离副本，不触碰产品 server.py）")
        print("    %s" % sandbox)
        print("=" * 78)
        proc = subprocess.run([PY, os.path.join(test_dir, "mutate_threads.py")], cwd=test_dir)
        return proc.returncode
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main():
    _ensure_node_path()
    fast = "--fast" in sys.argv
    results = []
    for label, cmd in STEPS:
        results.append((label, run(label, cmd)))
    if not fast:
        for label, cmd in EXTRA:
            results.append((label, run(label, cmd)))
        results.append(("多线程变异测试（隔离副本，5 个变异须全 KILLED）",
                        run_mutate_threads_isolated()))

    print("\n" + "=" * 78)
    print("总览")
    print("=" * 78)
    failed = 0
    for label, rc in results:
        print("  %-6s %s" % ("PASS" if rc == 0 else "FAIL", label))
        if rc != 0:
            failed += 1
    print("\n%d 组全部通过" % len(results) if not failed else "\n%d 组失败" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
