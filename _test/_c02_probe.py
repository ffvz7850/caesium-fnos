#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探针（报告用，不入库）：用**截断 JPEG** 复现 QA C02 的真实场景。

跑法： python _c02_probe.py
它借用 qa/qa_adv.py 的启动/接口封装，只替换 C02 的夹具（非图片字节 → 截断 JPEG），
用来证明「残骸例外」在真 HTTP 服务 + 真进程假引擎下确实生效，同时 C01a 仍然 PASS。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
QA = os.path.join(HERE, "qa")
sys.path.insert(0, QA)
sys.path.insert(0, HERE)

import qa_adv as Q  # noqa: E402

# 完整的 JPEG（尾部有 FFD9）与截断的 JPEG（缺 FFD9，正是闪退残骸的样子）
JPEG_COMPLETE = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                 + b"\x11" * 20000 + b"\xff\xd9")
JPEG_TRUNCATED = JPEG_COMPLETE[:-2]


def main():
    root = tempfile.mkdtemp(prefix="caesium_probe_")
    Q.STATE["root"] = root
    Q.STATE["work"] = os.path.join(root, "work")
    Q.STATE["log"] = os.path.join(root, "engine.log")
    os.makedirs(Q.STATE["work"], exist_ok=True)
    Q.STATE["port"] = Q.free_port()
    Q.start_server()
    try:
        # ---------- C02（真夹具：截断 JPEG） ----------
        src = Q.workdir("p02_src")
        out = Q.workdir("p02_out")
        a = Q.make_src(src, "half.jpg", data=JPEG_COMPLETE)
        residue = os.path.join(out, "half.jpg")
        with open(residue, "wb") as f:
            f.write(JPEG_TRUNCATED)
        size_before = os.path.getsize(residue)
        Q.clear_engine_calls()
        t = Q.compress(Q.items([a], src), out, dict(Q.BASE_OPTS, exist_policy="skip"))
        with open(residue, "rb") as f:
            head = f.read(8)
        size_after = os.path.getsize(residue)
        print("[C02-截断JPEG] state=%s ok=%s skipped=%s fail=%s spawn=%d 残骸 %d B → %d B 头字节=%r" %
              (t["state"], t["ok"], t["skipped"], t["fail"], len(Q.engine_calls()),
               size_before, size_after, head))
        # 判据：ok=1 / skipped=0 / spawn=1，且目标路径上已经是**这次引擎的产物**（不再是残骸）
        ok_c02 = (t["ok"] == 1 and t["skipped"] == 0 and len(Q.engine_calls()) == 1
                  and head.startswith(b"QA-FAKE"))
        print("   => %s" % ("PASS 已重压" if ok_c02 else "FAIL"))

        # ---------- C02 后半：修好后再压一次必须回到 skip（零 spawn） ----------
        Q.clear_engine_calls()
        t2 = Q.compress(Q.items([a], src), out, dict(Q.BASE_OPTS, exist_policy="skip"))
        ok_c02b = (t2["skipped"] == 1 and len(Q.engine_calls()) == 0)
        print("[C02-已修好再压] ok=%s skipped=%s spawn=%d => %s" %
              (t2["ok"], t2["skipped"], len(Q.engine_calls()), "PASS" if ok_c02b else "FAIL"))

        # ---------- C01a（回归：非图片的用户文件必须继续被 skip 保护） ----------
        src2 = Q.workdir("p01_src")
        out2 = Q.workdir("p01_out")
        b = Q.make_src(src2, "b.jpg", data=JPEG_COMPLETE)
        foreign = os.path.join(out2, "b.jpg")
        with open(foreign, "wb") as f:
            f.write(b"USER-FOREIGN-FILE")     # 同名但不是图片 → 不是残骸
        Q.clear_engine_calls()
        t3 = Q.compress(Q.items([b], src2), out2, dict(Q.BASE_OPTS, exist_policy="skip"))
        with open(foreign, "rb") as f:
            content = f.read()
        rec = Q.results().get(b)
        ok_c01a = (t3["skipped"] == 1 and len(Q.engine_calls()) == 0
                   and content == b"USER-FOREIGN-FILE" and rec is None)
        print("[C01a-非图片同名文件] skipped=%s spawn=%d 内容=%r record=%s => %s" %
              (t3["skipped"], len(Q.engine_calls()), content, rec, "PASS" if ok_c01a else "FAIL"))

        # ---------- 附：完整 JPEG 已存在时仍然 skip（不许误判成残骸） ----------
        src3 = Q.workdir("p03_src")
        out3 = Q.workdir("p03_out")
        c = Q.make_src(src3, "c.jpg", data=JPEG_COMPLETE)
        with open(os.path.join(out3, "c.jpg"), "wb") as f:
            f.write(JPEG_COMPLETE)
        Q.clear_engine_calls()
        t4 = Q.compress(Q.items([c], src3), out3, dict(Q.BASE_OPTS, exist_policy="skip"))
        ok_p03 = (t4["skipped"] == 1 and len(Q.engine_calls()) == 0)
        print("[附] 输出目录已有一张**完整**JPEG → skipped=%s spawn=%d => %s" %
              (t4["skipped"], len(Q.engine_calls()), "PASS" if ok_p03 else "FAIL"))

        print("\n汇总：C02=%s C02b=%s C01a=%s 完整图不误判=%s" %
              tuple("PASS" if x else "FAIL" for x in (ok_c02, ok_c02b, ok_c01a, ok_p03)))
        return 0 if all((ok_c02, ok_c02b, ok_c01a, ok_p03)) else 1
    finally:
        Q.stop_server()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
