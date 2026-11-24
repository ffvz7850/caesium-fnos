#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假的 caesiumclt 引擎（Windows/Linux 通用），用于本地测试 server.py。

用法（由 server.py 通过 CAESIUM_CLT 调起，参数与真引擎一致）：
    fake_clt.py [-q N] [--format F] [--suffix S] [-o OUTDIR] [-O all|never] SRC

环境变量：
    CAESIUM_FAKE_LOG     每被调用一次就往这个文件追加一行 JSON（spawn 计数 + 参数留证）
    CAESIUM_FAKE_SLEEP   处理每个文件前先 sleep 这么多秒（制造"压到一半"的时机）
    CAESIUM_FAKE_FAIL    源文件路径包含该子串 → 退出码 3（模拟单文件失败）
    CAESIUM_FAKE_BYTES   输出文件额外填充的字节数（默认按 -q 推导，便于断言"改参数重压"）

输出文件内容里带 q 值和时间戳，测试可以据此断言"这次到底重压了没有、用的什么参数"。
严格只输出 ASCII，避免 Windows 代码页（GBK/cp936）解码问题。
"""
import json
import os
import sys
import time

VALUE_FLAGS = {"-o", "-O", "-q", "--format", "--suffix", "--max-size", "--png-opt-level",
               "--jpeg-chroma-subsampling", "--tiff-algorithm"}
EXT_MAP = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp", "tiff": ".tiff",
           "gif": ".gif", "bmp": ".bmp", "tif": ".tiff"}


def parse_args(argv):
    """返回 dict：out_dir / overwrite / suffix / fmt / quality / src"""
    out = {"out_dir": None, "overwrite": "never", "suffix": "", "fmt": None,
           "quality": None, "src": None, "argv": list(argv)}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS and i + 1 < len(argv):
            val = argv[i + 1]
            if a == "-o":
                out["out_dir"] = val
            elif a == "-O":
                out["overwrite"] = val
            elif a == "--suffix":
                out["suffix"] = val
            elif a == "--format":
                out["fmt"] = val
            elif a == "-q":
                out["quality"] = val
            i += 2
            continue
        if not a.startswith("-"):
            out["src"] = a
        i += 1
    return out


def log_invocation(args):
    path = os.environ.get("CAESIUM_FAKE_LOG", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), "src": args["src"],
                                "out_dir": args["out_dir"], "overwrite": args["overwrite"],
                                "suffix": args["suffix"], "quality": args["quality"],
                                "argv": args["argv"]}, ensure_ascii=True) + "\n")
    except Exception:
        pass


def main():
    args = parse_args(sys.argv[1:])
    log_invocation(args)
    sleep_for = float(os.environ.get("CAESIUM_FAKE_SLEEP", "0") or 0)
    if sleep_for > 0:
        time.sleep(sleep_for)
    src = args["src"]
    if not src or not os.path.isfile(src):
        sys.stderr.write("fake: source not found: %s\n" % src)
        return 2
    fail_mark = os.environ.get("CAESIUM_FAKE_FAIL", "")
    if fail_mark and fail_mark in src:
        sys.stderr.write("fake: forced failure on %s\n" % os.path.basename(src))
        return 3
    out_dir = args["out_dir"] or os.path.dirname(src)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        sys.stderr.write("fake: cannot create out dir: %s\n" % e)
        return 4
    ext = os.path.splitext(src)[1].lower()
    if args["fmt"]:
        ext = EXT_MAP.get(str(args["fmt"]).lower(), ext)
    base = os.path.splitext(os.path.basename(src))[0]
    target = os.path.join(out_dir, base + args["suffix"] + ext)
    if args["overwrite"] != "all" and os.path.isfile(target):
        # 与真引擎一致的提示串
        print("File already exists, skipped due overwrite policy")
        return 0
    quality = args["quality"] or "na"
    try:
        extra = int(os.environ.get("CAESIUM_FAKE_BYTES", "0") or 0)
    except ValueError:
        extra = 0
    if extra <= 0:
        try:
            extra = 200 + int(quality) * 3
        except ValueError:
            extra = 240
    payload = ("FAKE q=%s ts=%.6f src=%s\n" % (quality, time.time(), os.path.basename(src))).encode("ascii")
    with open(target, "wb") as f:
        f.write(payload + b"x" * extra)
    print("fake: wrote %s" % os.path.basename(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
