#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA 专用假引擎（比工程师的 fake_clt.py 更"恶意"）。

支持四种"坏行为"，用来暴露真实引擎可能出现的中间态：
    QA_MODE=full     正常写完整文件（默认）
    QA_MODE=partial  只写前半段就退出（模拟引擎在编码中途崩掉 / 被 OOM 杀掉）
    QA_MODE=hang     写前半段后挂住不退出（模拟"正在压"的那一刻，供外部 kill）
    QA_MODE=chunk    分块慢写（模拟两个任务同时写同一个输出文件的竞态）

环境变量：
    QA_LOG          jsonl 追加每次 spawn（src/质量/覆盖策略/输出目录/nonce）
    QA_SLEEP        写文件前 sleep 秒数
    QA_PARTIAL_N    半截文件的字节数（默认 97）
    QA_EXIT         partial 模式的退出码（默认 9）
    QA_BYTES        输出文件体积（默认 4096）
    QA_NONCE        本次产物的唯一标记，落进文件头，用于判定"文件里到底是谁写的"
严格只输出 ASCII，避免 Windows 代码页问题。
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


def log_invocation(args, nonce, target):
    path = os.environ.get("QA_LOG", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), "src": args["src"],
                                "out_dir": args["out_dir"], "overwrite": args["overwrite"],
                                "suffix": args["suffix"], "quality": args["quality"],
                                "nonce": nonce, "target": target}) + "\n")
    except Exception:
        pass


def envnum(name, default=0):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def main():
    args = parse_args(sys.argv[1:])
    nonce = (os.environ.get("QA_NONCE", "") or "n") + "-%d" % os.getpid()
    src = args["src"]
    out_dir = args["out_dir"] or (os.path.dirname(src) if src else ".")
    ext = os.path.splitext(src)[1].lower() if src else ".jpg"
    if args["fmt"]:
        ext = EXT_MAP.get(str(args["fmt"]).lower(), ext)
    base = os.path.splitext(os.path.basename(src))[0] if src else "out"
    target = os.path.join(out_dir, base + args["suffix"] + ext)
    log_invocation(args, nonce, target)

    sleep_for = float(os.environ.get("QA_SLEEP", "0") or 0)
    if sleep_for > 0:
        time.sleep(sleep_for)
    if not src or not os.path.isfile(src):
        sys.stderr.write("qa-fake: source not found\n")
        return 2
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return 4
    if args["overwrite"] != "all" and os.path.isfile(target):
        print("File already exists, skipped due overwrite policy")
        return 0

    total = max(1, envnum("QA_BYTES", 4096))
    if os.environ.get("QA_SCALE_BY_Q"):
        try:
            total = max(1024, int(args["quality"] or 90) * envnum("QA_UNIT", 40960))
        except ValueError:
            pass
    head = ("QA-FAKE nonce=%s q=%s\n" % (nonce, args["quality"] or "na")).encode("ascii")
    mode = (os.environ.get("QA_MODE", "full") or "full").lower()
    if mode == "fail":
        # 引擎直接失败，什么都不写（模拟"文件损坏 / 磁盘满 / 引擎崩溃且未落盘"）
        sys.stderr.write("qa-fake: encoder failed\n")
        return envnum("QA_EXIT", 3)

    if mode in ("partial", "hang"):
        n = max(1, envnum("QA_PARTIAL_N", 97))
        with open(target, "wb") as f:
            f.write((head + b"p" * total)[:n])
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if mode == "hang":
            time.sleep(3600)      # 等外部 kill
            return 0
        return envnum("QA_EXIT", 9)

    with open(target, "wb") as f:
        if mode == "chunk":
            chunk = 64 * 1024
            tag = ("<%s>" % nonce).encode("ascii")
            # 整个文件体都由 nonce 构成 → 两个任务交错写时能看出"混了两种内容"
            body = (tag * (total // len(tag) + 1))[:total]
            payload = (head + body)[:total] if total > len(head) else head[:total]
            time.sleep(0.25)                       # 让两个任务都进到写文件阶段
            for off in range(0, len(payload), chunk):
                f.write(payload[off:off + chunk])
                f.flush()
                time.sleep(0.02)
        else:
            f.write(head + b"x" * max(0, total - len(head)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
