#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖的最小测试骨架（不引入 pytest，保证在任何机器上能直接跑）。"""
import sys
import traceback

PASSED = []
FAILED = []


def run(namespace, prefix="test_"):
    """跑 namespace 里所有 test_* 函数，返回失败数。"""
    cases = [(n, f) for n, f in sorted(namespace.items()) if n.startswith(prefix) and callable(f)]
    for name, fn in cases:
        try:
            fn()
        except Exception:
            FAILED.append((name, traceback.format_exc()))
            print("FAIL %s" % name)
        else:
            PASSED.append(name)
            print("PASS %s" % name)
    print("\n== %d passed, %d failed ==" % (len(PASSED), len(FAILED)))
    for name, tb in FAILED:
        print("\n---- %s ----\n%s" % (name, tb))
    return len(FAILED)


def finish(namespace, prefix="test_"):
    sys.exit(1 if run(namespace, prefix) else 0)


def eq(actual, expected, what=""):
    if actual != expected:
        raise AssertionError("%s 期望 %r，实际 %r" % (what or "值不匹配", expected, actual))


def is_true(cond, what="条件应为真"):
    if not cond:
        raise AssertionError(what)


def contains(haystack, needle, what=""):
    if needle not in haystack:
        raise AssertionError("%s 期望包含 %r，实际 %r" % (what or "文本", needle, haystack))
