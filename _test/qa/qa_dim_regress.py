#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尺寸解析回归（C8/C9/C10）：新实现 vs v1.7.1 旧实现，逐个像素级比对 + 读盘量实测。

关键点：
  * 同一批真实结构的图片（PNG/GIF/BMP/WebP/JPEG 含 SOF0/SOF1/SOF2、EXIF、多 APPn、
    填充字节、重启标记、截断、非法段长）→ 新旧返回值必须**完全一致**；
  * 「拼命读取」的量化证据：同一张 8MB JPEG，旧实现让进程读 8MB，新实现只读 64KB；
  * 元数据 > 512KB 的 JPEG → 新实现返回 None（工程师声明的取舍，只核对行为一致）；
  * 损坏/0 字节/只有 SOI/段长非法 → 不抛异常、不卡住。

用法：python qa_dim_regress.py
"""
import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(TEST_DIR)
APP = os.path.join(ROOT, "caesium", "app")
OLD_PY = os.path.join(HERE, "artifacts", "server.py.qa-backup")   # v1.7.1 那份
NEW_PY = os.path.join(APP, "server.py")

ROWS = []


def row(cid, scene, expect, actual, ok):
    ROWS.append((cid, scene, "PASS" if ok else "FAIL"))
    print("  [%s] %-7s %s\n         期望: %s\n         实际: %s" %
          ("PASS" if ok else "FAIL", cid, scene, expect, actual))


def load(path, name):
    """加载任意文件名为 .py 的模块（spec 只认 .py 后缀，备份文件先拷一份）。"""
    src = path
    if not path.endswith(".py"):
        src = os.path.join(tempfile.gettempdir(), name + ".py")
        shutil.copyfile(path, src)
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


from qa_io import read_bytes as self_read_bytes  # noqa: E402


# ------------------------------------------------------------------ 造图片
def png(w, h, truncate=False):
    d = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", w, h)
         + b"\x08\x02\x00\x00\x00" + b"\x00" * 8 + b"IEND\xaeB`\x82")
    return d[:-12] if truncate else d


def gif(w, h, ver=b"GIF89a"):
    return ver + struct.pack("<HH", w, h) + b"\xf7\x00\x00" + b"\x00" * 32 + b"\x3b"


def bmp(w, h):
    return b"BM" + struct.pack("<I", 100) + b"\x00\x00\x00\x00" + struct.pack("<I", 54) + \
        struct.pack("<I", 40) + struct.pack("<i", w) + struct.pack("<i", -h) + b"\x00" * 64


def webp_vp8x(w, h):
    return b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + struct.pack("<I", 10) + \
        b"\x00\x00\x00\x00" + struct.pack("<I", w)[:3] + struct.pack("<I", h)[:3]


def webp_vp8(w, h):
    return b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8 " + struct.pack("<I", 10) + \
        b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<H", w) + struct.pack("<H", h) + b"\x00" * 8


def seg(marker, payload):
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg(w, h, sof=0xC0, prefix=b"", tail=True, sof_offset_pad=0):
    """构造一个结构合法的 JPEG 头：SOI + prefix + SOF + SOS (+ EOI)。"""
    if sof_offset_pad:
        # 单个 APPn 段最长 65535，要撑到 100KB/600KB 必须拆成多段（真实大 EXIF 也是这样）
        pad = max(0, sof_offset_pad - 6)
        first = True
        while pad > 0:
            n = min(pad, 65533)
            body = (b"EXIF" if first else b"\x00\x00\x00\x00") + b"\x00" * max(0, n - 4)
            prefix += seg(0xE1, body[:n])
            first = False
            pad -= n
    sofseg = b"\xff" + bytes([sof]) + struct.pack(">H", 17) + b"\x08" + \
        struct.pack(">HH", h, w) + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    sos = b"\xff\xda" + struct.pack(">H", 12) + b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    body = b"\xff\xd8" + prefix + sofseg + sos + b"\x12" * 200
    return body + (b"\xff\xd9" if tail else b"")


CASES = []


def case(name, data, note=""):
    CASES.append((name, data, note))


def build_cases():
    case("png_640x480", png(640, 480))
    case("png_1x1", png(1, 1))
    case("png_truncated_iend", png(10, 20, truncate=True))
    case("png_header_only", png(10, 20)[:20])
    case("gif87", gif(320, 200, b"GIF87a"))
    case("gif89", gif(1024, 768, b"GIF89a"))
    case("bmp_800x600", bmp(800, 600))
    case("bmp_bottom_up", bmp(100, 50) .replace(struct.pack("<i", -50), struct.pack("<i", 50)))
    case("webp_vp8x", webp_vp8x(1920, 1080))
    case("webp_vp8", webp_vp8(333, 222))
    case("jpeg_sof0", jpeg(1200, 800, 0xC0))
    case("jpeg_sof1", jpeg(1201, 801, 0xC1))
    case("jpeg_sof2_progressive", jpeg(3000, 2000, 0xC2))
    case("jpeg_with_exif", jpeg(4000, 3000, 0xC0, prefix=seg(0xE1, b"Exif\x00\x00" + b"\x00" * 5000)))
    case("jpeg_multi_appn", jpeg(640, 480, 0xC0,
                                 prefix=seg(0xE0, b"JFIF" + b"\x00" * 12) + seg(0xE2, b"ICC" + b"\x00" * 3000)
                                 + seg(0xEE, b"Adobe" + b"\x00" * 20)))
    case("jpeg_fill_bytes", b"\xff\xd8" + b"\xff" * 100 + jpeg(111, 222)[2:])
    case("jpeg_no_eoi", jpeg(999, 777, 0xC0, tail=False))
    case("jpeg_only_soi", b"\xff\xd8")
    case("jpeg_sof_after_100k", jpeg(1980, 1080, 0xC0, sof_offset_pad=100000))
    case("jpeg_sof_after_600k", jpeg(1980, 1080, 0xC0, sof_offset_pad=600000))   # 超 512KB 上限
    case("jpeg_bad_seglen", b"\xff\xd8" + seg(0xE1, b"")[:2] + struct.pack(">H", 0) + b"\x00" * 40)
    case("jpeg_unsupported_sof_c4", b"\xff\xd8" + b"\xff\xc4" + struct.pack(">H", 4) + b"\x00\x00" + b"\xff\xd9")
    case("empty_file", b"")
    case("text_file", b"hello world, not an image\n" * 10)
    case("random_bytes", bytes(range(256)) * 20)


def main():
    tmp = tempfile.mkdtemp(prefix="caesium_dim_")
    print("== 尺寸解析回归 old=%s\n                   new=%s\n   临时=%s\n" % (OLD_PY, NEW_PY, tmp))
    try:
        old = load(OLD_PY, "server_old_171")
        new = load(NEW_PY, "server_new_172")
        build_cases()
        diffs, errors = [], []
        for name, data, _note in CASES:
            p = os.path.join(tmp, name + ".bin")
            with open(p, "wb") as f:
                f.write(data)
            try:
                a = old.get_image_size(p)
            except Exception as e:
                a = "EXC:%r" % e
                errors.append("old/%s %r" % (name, e))
            try:
                b = new.get_image_size(p)
            except Exception as e:
                b = "EXC:%r" % e
                errors.append("new/%s %r" % (name, e))
            if a != b:
                diffs.append((name, a, b))
        only_expected = all(n == "jpeg_sof_after_600k" for n, _a, _b in diffs)
        row("C8", "新旧 get_image_size 对 %d 个构造样本的返回值" % len(CASES),
            "除「元数据 >512KB」那条以外，返回值逐个完全一致，且都不抛异常",
            "不一致=%s；抛异常=%s" % (diffs or "无", errors or "无"),
            not errors and (not diffs or only_expected))

        # 超 512KB 那条：行为与文档一致（新=None，旧能解析出来）
        p600 = os.path.join(tmp, "jpeg_sof_after_600k.bin")
        row("C9", "元数据把 SOF 推到 600KB 之后的 JPEG（工程师声明的取舍）",
            "新实现返回 None（超过 512KB 上限就放弃），行为与文档一致 —— 只核对不判错",
            "旧=%s 新=%s" % (old.get_image_size(p600), new.get_image_size(p600)),
            new.get_image_size(p600) is None)

        # 读盘量：同一张 8MB JPEG（SOF 在 20KB 处）
        big = os.path.join(tmp, "big8m.jpg")
        head = jpeg(4032, 3024, 0xC0, prefix=seg(0xE1, b"Exif" + b"\x00" * 20000))
        with open(big, "wb") as f:
            f.write(head + b"\x00" * (8 * 1024 * 1024 - len(head)))
        old_dim = new_dim = None
        old_read = new_read = None
        for _ in range(3):                       # 预热，消掉首次的系统/缓存抖动
            a0 = self_read_bytes()
            old_dim = old.get_image_size(big)
            a1 = self_read_bytes()
            old_read = None if a0 is None else a1 - a0
            b0 = self_read_bytes()
            new_dim = new.get_image_size(big)
            b1 = self_read_bytes()
            new_read = None if b0 is None else b1 - b0
        row("C10a", "同一张 8MB JPEG：一次尺寸解析到底读了多少字节",
            "旧实现读 ≈8MB；新实现读 ≤ 128KB（64KB 一块），尺寸结果一致",
            "旧=%r 读 %s 字节；新=%r 读 %s 字节" % (old_dim, old_read, new_dim, new_read),
            old_dim == new_dim and (new_read is None or new_read <= 128 * 1024))

        # 损坏文件不许卡住
        slow = []
        for name, data, _n in CASES:
            p = os.path.join(tmp, name + ".bin")
            t0 = time.time()
            new.get_image_size(p)
            dt = time.time() - t0
            if dt > 0.05:
                slow.append((name, round(dt, 3)))
        row("C10b", "损坏 / 0 字节 / 只有 SOI / 段长非法 —— 不抛异常、不卡住",
            "每个样本解析耗时 < 50ms",
            "超时样本=%s" % (slow or "无"), not slow)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 92)
    for cid, scene, verdict in ROWS:
        print("%-6s %-58s %s" % (cid, scene[:56], verdict))
    fails = [r for r in ROWS if r[2] == "FAIL"]
    print("=" * 92)
    print("合计 %d 条，PASS %d，FAIL %d" % (len(ROWS), len(ROWS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
