#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分段跑 mutate.py（环境对单条命令的时长有限制，整跑会被 SIGTERM）。

用法：python _mutate_chunk.py <起始序号1-based> <本段条数>
每段都会：跑基线 → 逐个变异并还原 → 校验 sha256 还原 → 打印本段结论。
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("mutate", os.path.join(HERE, "mutate.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

start = int(sys.argv[1]) - 1
count = int(sys.argv[2])
chunk = m.MUTATIONS[start:start + count]
print("== chunk %d..%d / %d ==" % (start + 1, start + len(chunk), len(m.MUTATIONS)))
for i, item in enumerate(chunk, start + 1):
    print("   %2d. %s" % (i, item[0]))
m.MUTATIONS = chunk
sys.argv = [sys.argv[0]]
rc = m.main()
print("== chunk rc=%d ==" % rc)
sys.exit(rc)
