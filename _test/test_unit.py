#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py 后端单元/集成测试（进程内，monkeypatch 掉 subprocess.run，不执行真引擎）。

覆盖：sig 指纹、三态 exist_policy、断点续传判定、节流落盘、run_task 异常兜底、计数守恒、
闪退残骸识别（image_looks_truncated）与 skip 的「残骸例外」。
"""
import builtins
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "caesium", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import harness as H  # noqa: E402
import server as srv  # noqa: E402

# 复用假引擎的参数解析逻辑，保证"进程内 stub"和"真进程假引擎"行为一致
_spec = importlib.util.spec_from_file_location("fake_clt", os.path.join(HERE, "fake_clt.py"))
fake_clt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_clt)


class FakeProc(object):
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class FakeRun(object):
    """替代 subprocess.run：记录每次 spawn，并按 -o/-O/--suffix/--format 落盘。"""

    def __init__(self, fail_contains=""):
        self.calls = []
        self.fail_contains = fail_contains

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        parsed = fake_clt.parse_args([str(x) for x in cmd[1:]])
        src = parsed["src"]
        if not src or not os.path.isfile(src):
            return FakeProc(rc=2, err="source not found")
        if self.fail_contains and self.fail_contains in src:
            return FakeProc(rc=3, err="forced failure")
        out_dir = parsed["out_dir"] or os.path.dirname(src)
        os.makedirs(out_dir, exist_ok=True)
        ext = os.path.splitext(src)[1].lower()
        if parsed["fmt"]:
            ext = fake_clt.EXT_MAP.get(str(parsed["fmt"]).lower(), ext)
        base = os.path.splitext(os.path.basename(src))[0]
        target = os.path.join(out_dir, base + parsed["suffix"] + ext)
        if parsed["overwrite"] != "all" and os.path.isfile(target):
            return FakeProc(rc=0, out="File already exists, skipped due overwrite policy")
        payload = ("FAKE q=%s\n" % (parsed["quality"] or "na")).encode("ascii")
        with open(target, "wb") as f:
            f.write(payload + b"x" * 300)
        return FakeProc(rc=0, out="fake: wrote %s" % os.path.basename(target))


def base_opts(**kw):
    opts = {"mode": "quality", "lossless": False, "jpeg_quality": 90, "png_quality": 90,
            "png_opt_level": 3, "webp_quality": 90, "tiff_algorithm": "deflate", "chroma": "auto",
            "progressive": True, "zopfli": False, "keep_exif": True, "suffix": "", "format": "keep",
            "keep_dates": True, "long_edge": 0, "width": 0, "height": 0, "percent": 0,
            "no_upscale": False, "max_size_kb": 200, "exist_policy": "skip"}
    opts.update(kw)
    return opts


def setup_tmp():
    """每个用例独立的临时工作区（源目录 + 输出目录 + workdir）。"""
    root = tempfile.mkdtemp(prefix="caesium_unit_")
    src_dir = os.path.join(root, "src")
    out_dir = os.path.join(root, "out")
    work = os.path.join(root, "work")
    os.makedirs(src_dir)
    os.makedirs(out_dir)
    os.makedirs(work)
    srv.RESULTS_FILE = os.path.join(work, "results.json")
    srv.RESULTS = {"version": 1, "records": {}}
    srv.RESULTS_DIRTY = 0
    srv.RESULTS_LAST_FLUSH = 0.0
    srv.CLT = os.path.join(HERE, "fake_clt.cmd")
    with srv.TASKS_LOCK:
        srv.TASKS.clear()
    return root, src_dir, out_dir, work


def make_src(src_dir, name="a.jpg", data=b"ORIGINAL"):
    p = os.path.join(src_dir, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


# ---- 夹具：合法的图片字节（只求魔数/结束标记正确，不要求真能解码） ----
JPEG_COMPLETE = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x11" * 2000 + b"\xff\xd9"
JPEG_TRUNCATED = JPEG_COMPLETE[:-2]          # 砍掉 EOI，正是闪退时留在磁盘上的样子
PNG_COMPLETE = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 13 + b"\x00" * 4
                + b"\x00\x00\x00\x00IEND\xaeB`\x82")
PNG_TRUNCATED = PNG_COMPLETE[:-12]           # 砍掉 IEND chunk


def make_file(dirpath, name, data):
    p = os.path.join(dirpath, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def cleanup(root):
    shutil.rmtree(root, ignore_errors=True)


# ---- 夹具：v1.7.2 用的 JPEG（带真实 SOF，宽高可解析） ----
def build_jpeg(width=200, height=100, app_segments=0, app_size=0xFFFF, tail=b""):
    """拼一张 JPEG：SOI + 若干 APP2(可调长度，用于把 SOF 推到很后面) + SOF0 + 填充 + EOI。

    app_segments/app_size 用来模拟"元数据很大"的照片，验证分块读取真的不用读完整个文件。
    """
    body = bytearray(b"\xff\xd8")
    for _ in range(app_segments):
        body += b"\xff\xe2" + struct.pack(">H", app_size) + b"\x00" * (app_size - 2)
    body += (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
             + struct.pack(">HH", height, width) + b"\x03"
             + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    body += tail
    body += b"\xff\xd9"
    return bytes(body)


class CountingReader(object):
    """包一层文件对象，统计被读出的字节数（证明"没有整文件读"）。"""

    def __init__(self, f, counter):
        self._f = f
        self._counter = counter

    def read(self, n=-1):
        data = self._f.read(n)
        self._counter[0] += len(data)
        return data

    def seek(self, *args):
        return self._f.seek(*args)

    def close(self):
        return self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False

    def __getattr__(self, name):
        return getattr(self._f, name)


def counting_open(counter):
    """返回 (真实 open, 记账版 open)；用法见 _measure_read_bytes()。"""
    real = builtins.open

    def wrapped(path, mode="r", *a, **k):
        f = real(path, mode, *a, **k)
        if "b" in str(mode):
            return CountingReader(f, counter)
        return f

    return real, wrapped


def _measure_read_bytes(fn, path):
    """跑 fn(path)，返回 (结果, 一共读了多少字节)。"""
    counter = [0]
    real, wrapped = counting_open(counter)
    builtins.open = wrapped
    try:
        got = fn(path)
    finally:
        builtins.open = real
    return got, counter[0]


# 旧实现（v1.7.1 及以前）：整文件读入内存后顺序扫 marker。只用于证明"结果一致"。
def _old_jpeg_size(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    try:
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            if marker in (0xD9, 0xDA):
                break
            seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            if seg_len < 2:
                break
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return struct.unpack(">HH", data[pos + 5:pos + 9])
            pos += 2 + seg_len
    except Exception:
        pass
    return None


def _wait_scan(sid, timeout=120, page=500):
    """等扫描结束，再按 page 大小**分页**把结果全部拉回来，返回 (末页快照, files, 页大小列表)。"""
    deadline = time.time() + timeout
    while True:
        with srv.SCAN_LOCK:
            st = srv.SCANS[sid]
        snap = srv.scan_snapshot(st, 0, 1)
        if snap["state"] != "running":
            break
        H.is_true(time.time() < deadline, "扫描超时（state=%s total=%s）" % (snap["state"], snap["total"]))
        time.sleep(0.01)
    files, pages = [], []
    while True:
        snap = srv.scan_snapshot(st, len(files), page)
        files.extend(snap["files"])
        pages.append(len(snap["files"]))
        if not snap["files"] or len(files) >= snap["total"]:
            return snap, files, pages


def _make_many(dirpath, n, name_fmt="p%04d.jpg"):
    for i in range(n):
        with open(os.path.join(dirpath, name_fmt % i), "wb") as f:
            f.write(JPEG_COMPLETE)


# ------------------------------------------------------------------ 参数指纹
def test_sig_is_stable_for_same_params():
    a = srv.results_signature(base_opts())
    b = srv.results_signature(base_opts())
    H.eq(a, b, "同参数指纹应一致")
    H.eq(len(a), 8, "指纹应为 8 位")


def test_sig_ignores_policy_but_tracks_quality():
    base = srv.results_signature(base_opts())
    H.eq(srv.results_signature(base_opts(exist_policy="overwrite")), base, "exist_policy 不应影响指纹")
    H.eq(srv.results_signature(base_opts(exist_policy="rename")), base, "exist_policy 不应影响指纹")
    H.is_true(srv.results_signature(base_opts(overwrite=True)) == base, "overwrite 布尔量不应影响指纹")
    H.is_true(srv.results_signature(base_opts(jpeg_quality=60)) != base, "jpeg_quality 变化必须改变指纹")
    H.is_true(srv.results_signature(base_opts(png_opt_level=6)) != base, "png_opt_level 变化必须改变指纹")
    H.is_true(srv.results_signature(base_opts(suffix="_c")) != base, "suffix 变化必须改变指纹")
    H.is_true(srv.results_signature(base_opts(format="webp")) != base, "format 变化必须改变指纹")


def test_normalize_exist_policy():
    H.eq(srv.normalize_exist_policy({"exist_policy": "rename"}), "rename")
    H.eq(srv.normalize_exist_policy({"exist_policy": "OVERWRITE"}), "overwrite")
    H.eq(srv.normalize_exist_policy({}), "skip", "缺省应为 skip")
    H.eq(srv.normalize_exist_policy({"exist_policy": "bogus"}), "skip", "非法值应回落 skip")
    H.eq(srv.normalize_exist_policy({"overwrite": True}), "overwrite", "老客户端 overwrite=True 应映射覆盖")


def test_resolve_out_format():
    H.eq(srv.resolve_out_format({"format": "keep"}, ".jpg"), ("keep", "jpeg", ".jpg"))
    H.eq(srv.resolve_out_format({"format": "keep"}, ".gif"), ("keep", "gif", ".gif"))
    H.eq(srv.resolve_out_format({"format": "webp"}, ".jpg"), ("webp", "webp", ".webp"))
    H.eq(srv.resolve_out_format({"format": "keep"}, ".bmp"), ("keep", "jpeg", ".jpg"), "bmp 按 jpeg 处理")


# ------------------------------------------------------------------ 三态策略
def test_skip_policy_does_not_spawn_engine():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        H.eq((ok, status, err), (True, "ok", ""), "首次压缩应成功")
        H.eq(len(run.calls), 1, "首次压缩应 spawn 一次引擎")
        before = read_bytes(out)
        # 第二次：目标已存在 → 必须返回 skipped 且**不再 spawn**
        ok2, out2, err2, status2 = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        H.eq((ok2, status2), (True, "skipped"), "已存在文件应返回 skipped")
        H.eq(len(run.calls), 1, "skip 策略下不允许再 spawn 引擎")
        H.eq(out2, out, "跳过后返回的应是已存在文件")
        H.eq(read_bytes(out), before, "跳过时不得改动已有文件")
        H.eq([x for x in os.listdir(out_dir) if x.startswith(".caesium-tmp")], [], "不应残留临时目录")
    finally:
        cleanup(root)


def test_skip_policy_ignores_empty_file():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        open(os.path.join(out_dir, "a.jpg"), "wb").close()  # 0 字节残留
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        H.eq(status, "ok", "0 字节残留文件视为未压缩，应重压")
        H.eq(len(run.calls), 1)
        H.is_true(os.path.getsize(out) > 0, "重压后不应是空文件")
    finally:
        cleanup(root)


# ------------------------------------------------------------------ 残骸例外（C02）
def test_image_looks_truncated_rules():
    """残骸判定的 4 类图片输入 + 「非图片不算残骸」的边界。

    判据必须精确：**有合法图片魔数、但缺结束标记**才算残骸。放宽成"非图片也算残骸"
    会把用户自己的同名文件（C01a）当成垃圾覆盖掉。"""
    root, src_dir, _out_dir, _work = setup_tmp()
    try:
        complete_jpeg = make_file(src_dir, "ok.jpg", JPEG_COMPLETE)
        cut_jpeg = make_file(src_dir, "cut.jpg", JPEG_TRUNCATED)
        complete_png = make_file(src_dir, "ok.png", PNG_COMPLETE)
        cut_png = make_file(src_dir, "cut.png", PNG_TRUNCATED)

        H.eq(srv.image_looks_truncated(complete_jpeg), False, "完整 JPEG（尾部有 FFD9）不是残骸")
        H.eq(srv.image_looks_truncated(cut_jpeg), True, "截断 JPEG（缺 FFD9）必须判残骸")
        H.eq(srv.image_looks_truncated(complete_png), False, "完整 PNG（有 IEND）不是残骸")
        H.eq(srv.image_looks_truncated(cut_png), True, "截断 PNG（缺 IEND）必须判残骸")

        # 非图片 / 无魔数 → 不算残骸（用户自己的文件，C01a 语义不变）
        foreign = make_file(src_dir, "foreign.jpg", b"USER-FOREIGN-FILE")
        binary = make_file(src_dir, "blob.jpg", bytes(range(256)) * 8)
        empty = make_file(src_dir, "empty.jpg", b"")
        H.eq(srv.image_looks_truncated(foreign), False, "非图片内容不算残骸（保护用户文件）")
        H.eq(srv.image_looks_truncated(binary), False, "无图片魔数的二进制不算残骸")
        H.eq(srv.image_looks_truncated(empty), False, "0 字节文件不算残骸（走原有 size>0 判定）")
        H.eq(srv.image_looks_truncated(os.path.join(src_dir, "nope.jpg")), False,
             "文件不存在不许抛异常，按「不是残骸」处理")

        # 只读头尾：尾部标记在最后 2KB 之内就够（不整文件读入内存）
        big = make_file(src_dir, "big.jpg", b"\xff\xd8" + b"\x42" * (3 * 1024 * 1024) + b"\xff\xd9")
        H.eq(srv.image_looks_truncated(big), False, "3MB 完整 JPEG 也只靠尾部 2KB 判定")
        os.unlink(big)
        big_cut = make_file(src_dir, "bigcut.jpg", b"\xff\xd8" + b"\x42" * (3 * 1024 * 1024))
        H.eq(srv.image_looks_truncated(big_cut), True, "3MB 截断 JPEG 必须判残骸")

        # WebP：RIFF 声明的长度 > 实际长度才算残骸；尾部追加数据不算（不做过度误判）
        webp_declared = struct.pack("<I", 4000)
        webp_cut = make_file(src_dir, "cut.webp", b"RIFF" + webp_declared + b"WEBPVP8 " + b"\x00" * 100)
        webp_ok = make_file(src_dir, "ok.webp", b"RIFF" + struct.pack("<I", 108) + b"WEBPVP8 " + b"\x00" * 100)
        webp_tail = make_file(src_dir, "tail.webp", b"RIFF" + struct.pack("<I", 50) + b"WEBPVP8 " + b"\x00" * 100)
        H.eq(srv.image_looks_truncated(webp_cut), True, "WebP 声明长度大于实际长度 → 残骸")
        H.eq(srv.image_looks_truncated(webp_ok), False, "WebP 声明长度与实际一致 → 不是残骸")
        H.eq(srv.image_looks_truncated(webp_tail), False, "WebP 尾部多出数据 → 不算残骸（宁漏判不误判）")

        # GIF / TIFF 没有低成本且不会误判的判据 → 明确不做（不判残骸）
        H.eq(srv.image_looks_truncated(make_file(src_dir, "a.gif", b"GIF89a" + b"\x00" * 40)), False,
             "GIF 不在残骸判定范围内（宁可漏判，不可误判）")
        H.eq(srv.image_looks_truncated(make_file(src_dir, "a.tiff", b"II*\x00" + b"\x00" * 40)), False,
             "TIFF 不在残骸判定范围内")
    finally:
        cleanup(root)


def test_image_looks_truncated_motion_photo():
    """EOI 之后还有附加数据的 JPEG（手机 Motion Photo）**不是**残骸。

    只看尾部 2KB 会把这类用户照片判成残骸 → skip 策略被绕过 → 直接覆盖原图（QA C01d）。
    修法：尾部没有 EOI 就按 64KB 从尾向前回扫，找到即停。
    """
    root, src_dir, _out, _work = setup_tmp()
    try:
        # EOI 在文件尾 → 一眼就是完整图（零额外读）
        plain = make_file(src_dir, "plain.jpg", JPEG_COMPLETE)
        H.eq(srv.image_looks_truncated(plain), False, "EOI 在末尾的完整 JPEG 不是残骸")

        # Motion Photo：EOI 之后跟 5KB 附加数据（比尾部 2KB 窗口远得多）
        motion = make_file(src_dir, "motion.jpg", JPEG_COMPLETE + b"\x00" * 5000)
        H.eq(srv.image_looks_truncated(motion), False,
             "Motion Photo（EOI 距文件尾 5KB）不能判残骸 —— 否则会覆盖用户原图")

        # 尾部 100KB 附加数据也要能回扫到
        far = make_file(src_dir, "far.jpg", JPEG_COMPLETE + b"\x11" * (100 * 1024))
        H.eq(srv.image_looks_truncated(far), False, "EOI 距文件尾 100KB 仍不能误判")

        # 真截断（整份文件里都没有 EOI）依旧必须判残骸
        for name, data in (("cut.jpg", JPEG_TRUNCATED),
                           ("cut_big.jpg", b"\xff\xd8" + b"\x42" * (300 * 1024)),
                           ("cut_png.png", PNG_TRUNCATED)):
            H.eq(srv.image_looks_truncated(make_file(src_dir, name, data)), True,
                 "%s 必须仍判残骸（残骸例外不能被这次修复弄丢）" % name)

        # 回扫预算：文件 > 8MB 且尾部 8MB 内都没有 EOI → 不判残骸（宁可漏判不可误判）
        huge_no_eoi = make_file(src_dir, "huge.jpg", b"\xff\xd8" + b"\x00" * (9 * 1024 * 1024))
        H.eq(srv.image_looks_truncated(huge_no_eoi), False,
             "超过 8MB 回扫预算仍找不到 EOI → 按「不是残骸」处理（不许赌）")
        # 9MB 但 EOI 在最后 → 正常
        huge_ok = make_file(src_dir, "huge_ok.jpg",
                            b"\xff\xd8" + b"\x00" * (9 * 1024 * 1024) + b"\xff\xd9")
        H.eq(srv.image_looks_truncated(huge_ok), False, "9MB 完整 JPEG 不是残骸")
    finally:
        cleanup(root)


def test_motion_photo_is_never_overwritten_by_skip():
    """端到端：输出目录里是用户的 Motion Photo + skip 策略 → 必须跳过，一个字节都不许改。

    这是 C01d 的真实后果：误判成残骸 = 绕过 skip = **覆盖用户照片**。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "m.jpg", data=build_jpeg(200, 100))
        motion = JPEG_COMPLETE + b"\x00" * 5000
        user_file = os.path.join(out_dir, "m.jpg")
        with open(user_file, "wb") as f:
            f.write(motion)
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        H.eq((ok, status), (True, "skipped"), "Motion Photo 必须按 skip 跳过")
        H.eq(len(run.calls), 0, "跳过时绝不能 spawn 引擎")
        H.eq(read_bytes(user_file), motion, "用户照片必须逐字节保持不变")
    finally:
        cleanup(root)


def test_skip_policy_recompresses_truncated_residue():
    """端到端：输出目录里放着「截断的 JPEG」+ skip 策略 → 必须重压成完整图。

    这是升级路径上的真实存量：v1.7.0 的引擎直写最终路径，闪退后残留的坏图非空、
    又没有归属记录，旧逻辑会 skip 掉它，用户拿到坏图还显示「已跳过」。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "half.jpg", data=JPEG_COMPLETE)
        residue = os.path.join(out_dir, "half.jpg")
        with open(residue, "wb") as f:
            f.write(JPEG_TRUNCATED)          # 20 KB 级的截断残骸
        H.is_true(os.path.getsize(residue) > 2000, "残骸应大于尾部扫描窗口，确保走的是尾扫")
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        H.eq((ok, status), (True, "ok"), "截断残骸必须重压，不能 skipped")
        H.eq(len(run.calls), 1, "残骸例外下必须 spawn 一次引擎")
        H.eq(out, residue, "重压后产物仍是同一个目标路径")
        data = read_bytes(residue)
        H.is_true(data.startswith(b"FAKE"), "目标路径上应是新的完整成品，实际 %r" % data[:16])
        H.eq(srv.image_looks_truncated(residue), False, "重压后不再是残骸")
        H.eq([n for n in os.listdir(out_dir) if n.startswith(".caesium-tmp")], [],
             "重压后不许残留临时目录")
        # 再压一次：这次是真的完整成品 → 必须回到正常的 skip 语义（零 spawn）
        ok2, out2, err2, status2 = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        H.eq((ok2, status2), (True, "skipped"), "残骸修好后应回到 skip 语义")
        H.eq(len(run.calls), 1, "正常成品不许再 spawn 引擎")
    finally:
        cleanup(root)


def test_run_task_recompresses_truncated_residue_and_records():
    """run_task 层：残骸 → 记 ok + 写归属记录（只记我们真压出来的文件）。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "r2.jpg", data=JPEG_COMPLETE)
        with open(os.path.join(out_dir, "r2.jpg"), "wb") as f:
            f.write(JPEG_TRUNCATED)
        srv.subprocess.run = FakeRun()
        t = _run_task([{"path": src, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t["ok"], t["skipped"], t["fail"]), (1, 0, 0), "残骸必须重压并记 ok")
        H.eq(t["done"], t["ok"] + t["skipped"] + t["fail"], "计数口径必须守恒")
        rec = srv.get_results().get(src)
        H.is_true(isinstance(rec, dict) and rec.get("status") == "ok",
                  "重压成功后必须写归属记录（下次改参数才能重压）")
        H.is_true(read_bytes(os.path.join(out_dir, "r2.jpg")).startswith(b"FAKE"),
                  "目标路径上应是完整成品")
    finally:
        cleanup(root)


def test_overwrite_policy_really_overwrites():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        existing = os.path.join(out_dir, "a.jpg")
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(exist_policy="overwrite"), ".jpg")
        H.eq(status, "ok")
        H.eq(len(run.calls), 1, "overwrite 策略应 spawn")
        H.eq(read_bytes(existing), b"FAKE q=90\n" + b"x" * 300, "覆盖策略必须重写目标文件")
        cmd = run.calls[0]
        H.eq(cmd[cmd.index("-O") + 1], "all", "overwrite 策略应传 -O all")
    finally:
        cleanup(root)


def test_rename_policy_keeps_both_copies():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        first = os.path.join(out_dir, "a.jpg")
        with open(first, "wb") as f:
            f.write(b"OLD")
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(exist_policy="rename"), ".jpg")
        H.eq(status, "ok", "rename 策略应成功")
        H.eq(os.path.basename(out), "a (2).jpg", "冲突时应落为 a (2).jpg")
        H.eq(read_bytes(first), b"OLD", "原有文件绝不能被覆盖")
        H.is_true(read_bytes(out).startswith(b"FAKE"), "新文件应是本次压缩结果")
        H.eq([x for x in os.listdir(out_dir) if x.startswith(".caesium-tmp")], [], "临时目录必须清理")
        H.eq(run.calls[0][run.calls[0].index("-O") + 1], "never", "rename 策略引擎侧也不许覆盖")
        # 再压一次 → a (3).jpg
        ok3, out3, err3, status3 = srv.compress_file(src, out_dir, base_opts(exist_policy="rename"), ".jpg")
        H.eq(os.path.basename(out3), "a (3).jpg", "第二次冲突应递增到 (3)")
    finally:
        cleanup(root)


def test_rename_policy_respects_suffix():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        with open(os.path.join(out_dir, "a_c.jpg"), "wb") as f:
            f.write(b"OLD")
        srv.subprocess.run = FakeRun()
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(exist_policy="rename", suffix="_c"), ".jpg")
        H.eq(status, "ok")
        H.eq(os.path.basename(out), "a_c (2).jpg", "带后缀时冲突命名应保留后缀")
    finally:
        cleanup(root)


def test_same_folder_forces_overwrite():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "a.jpg", b"ORIGINAL")
        run = FakeRun()
        srv.subprocess.run = run
        # 目标 == 源文件：即使请求的是 skip，也必须按覆盖处理
        ok, out, err, status = srv.compress_file(src, src_dir, base_opts(exist_policy="skip"), ".jpg")
        H.eq(status, "ok", "输出到原文件夹时必须真覆盖")
        H.eq(out, src)
        H.eq(len(run.calls), 1)
        H.is_true(read_bytes(src).startswith(b"FAKE"), "原图应被覆盖")
    finally:
        cleanup(root)


# ------------------------------------------------------------------ 断点续传
def test_result_is_done_rules():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        out = os.path.join(out_dir, "a.jpg")
        with open(out, "wb") as f:
            f.write(b"x" * 100)
        sig = srv.results_signature(base_opts())
        srv.record_result(src, {"status": "ok", "out": out, "in_size": 7, "out_size": 100,
                                "ts": 1, "sig": sig})
        srv.flush_results(force=True)
        H.is_true(srv.result_is_done(src, sig) is not None, "完全匹配应判定已完成")
        H.is_true(srv.result_is_done(src, "deadbeef") is None, "参数指纹不同必须判定未完成")
        H.is_true(srv.result_is_done(src, sig, out_dir) is not None, "目标目录一致应判定已完成")
        H.is_true(srv.result_is_done(src, sig, src_dir) is None, "目标目录不同必须判定未完成")
        # 输出文件被删 → 未完成
        os.unlink(out)
        H.is_true(srv.result_is_done(src, sig) is None, "输出文件不存在必须判定未完成")
        # 大小不符 → 未完成
        with open(out, "wb") as f:
            f.write(b"y" * 99)
        H.is_true(srv.result_is_done(src, sig) is None, "输出文件大小变化必须判定未完成")
        # 状态为 error → 未完成
        with open(out, "wb") as f:
            f.write(b"x" * 100)
        srv.record_result(src, {"status": "error", "out": out, "sig": sig})
        H.is_true(srv.result_is_done(src, sig) is None, "error 记录不算已完成")
    finally:
        cleanup(root)


def test_results_flush_throttle_and_force():
    root, _src_dir, _out_dir, _work = setup_tmp()
    try:
        path = srv.RESULTS_FILE
        srv.record_result("/p/1.jpg", {"status": "ok", "sig": "aaaa"})
        H.is_true(os.path.isfile(path), "首条记录应立即落盘（距上次写盘已超过 1 秒）")
        for i in range(2, 5):
            srv.record_result("/p/%d.jpg" % i, {"status": "ok", "sig": "aaaa"})
        with open(path, "r", encoding="utf-8") as f:
            disk = json.load(f)
        H.eq(len(disk["records"]), 1, "1 秒内且不足 5 条时应节流，不落盘")
        srv.flush_results(force=True)
        with open(path, "r", encoding="utf-8") as f:
            disk = json.load(f)
        H.eq(len(disk["records"]), 4, "force 落盘应写入全部记录")
    finally:
        cleanup(root)


def test_results_survive_reload():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        srv.subprocess.run = FakeRun()
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        sig = srv.results_signature(base_opts())
        srv.record_result(src, {"status": "ok", "out": out, "in_size": 8,
                                "out_size": os.path.getsize(out), "ts": 1, "sig": sig})
        srv.flush_results(force=True)
        srv.RESULTS = {"version": 1, "records": {}}   # 模拟进程重启后内存清空
        srv.load_results()
        H.is_true(srv.result_is_done(src, sig) is not None, "重启后 records 必须能从磁盘恢复")
    finally:
        cleanup(root)


def _run_task(files, out_dir, opts, keep=True):
    t = srv.new_task(files, out_dir, opts, keep)
    srv.run_task(t)
    return t


def test_resume_skips_done_files_without_spawning():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir, "a.jpg")
        b = make_src(src_dir, "b.jpg")
        files = [{"path": a, "root": src_dir}, {"path": b, "root": src_dir}]
        run = FakeRun()
        srv.subprocess.run = run
        t1 = _run_task(files, out_dir, base_opts(), keep=False)
        H.eq(t1["state"], "done")
        H.eq((t1["ok"], t1["skipped"], t1["fail"]), (2, 0, 0))
        H.eq(len(run.calls), 2, "首次应 spawn 两次")
        # 模拟"闪退后重启再压一次"
        srv.RESULTS = {"version": 1, "records": {}}
        srv.load_results()
        run2 = FakeRun()
        srv.subprocess.run = run2
        t2 = _run_task(files, out_dir, base_opts(), keep=False)
        H.eq((t2["ok"], t2["skipped"], t2["fail"]), (0, 2, 0), "已完成文件应全部 skipped")
        H.eq(len(run2.calls), 0, "断点续传命中时不允许 spawn 引擎")
        H.eq(t2["total"], t2["ok"] + t2["skipped"] + t2["fail"], "total 必须等于 ok+skipped+fail")
        statuses = [r["status"] for r in t2["results"]]
        H.eq(statuses, ["skipped", "skipped"])
        H.is_true(all(r.get("out") for r in t2["results"]), "skipped 结果应带回输出路径")
    finally:
        cleanup(root)


def test_changed_quality_forces_recompress():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir)
        files = [{"path": a, "root": src_dir}]
        run = FakeRun()
        srv.subprocess.run = run
        _run_task(files, out_dir, base_opts(jpeg_quality=90), keep=False)
        H.eq(len(run.calls), 1)
        # 参数没变 → 跳过
        t2 = _run_task(files, out_dir, base_opts(jpeg_quality=90), keep=False)
        H.eq(t2["skipped"], 1)
        H.eq(len(run.calls), 1)
        # 改质量 → 必须重压
        t3 = _run_task(files, out_dir, base_opts(jpeg_quality=60), keep=False)
        H.eq(t3["ok"], 1, "参数变化后必须重压")
        H.eq(len(run.calls), 2, "参数变化后应再次 spawn")
    finally:
        cleanup(root)


def test_run_task_exception_lands_in_error():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir)
        files = [{"path": a, "root": src_dir}]
        srv.subprocess.run = FakeRun()

        def boom(*args, **kwargs):
            raise RuntimeError("injected failure")

        original = srv.compress_file
        srv.compress_file = boom
        try:
            t = _run_task(files, out_dir, base_opts(), keep=False)
        finally:
            srv.compress_file = original
        H.eq(t["state"], "error", "未捕获异常必须落到 error，不能停在 running")
        H.contains(t["error"] or "", "injected failure", "error 应带上原因")
        H.is_true(t["finished"] is not None, "finally 必须写 finished")
        H.is_true(t["current"] is None, "finally 必须清空 current")
        H.is_true(os.path.isfile(srv.RESULTS_FILE), "异常退出也必须强制落盘")
    finally:
        cleanup(root)


def test_run_task_missing_file_counts_as_fail():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        files = [{"path": os.path.join(src_dir, "nope.jpg"), "root": src_dir}]
        srv.subprocess.run = FakeRun()
        t = _run_task(files, out_dir, base_opts(), keep=False)
        H.eq(t["state"], "done")
        H.eq((t["done"], t["fail"]), (1, 1))
        H.eq(t["total"], t["ok"] + t["skipped"] + t["fail"])
        H.eq(t["results"][0]["status"], "error")
    finally:
        cleanup(root)


def test_run_task_mixed_counts_are_consistent():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir, "a.jpg")
        b = make_src(src_dir, "b.jpg")
        c = make_src(src_dir, "c-fail.jpg")
        files = [{"path": a, "root": src_dir}, {"path": b, "root": src_dir},
                 {"path": c, "root": src_dir}]
        srv.subprocess.run = FakeRun(fail_contains="c-fail")
        t1 = _run_task(files, out_dir, base_opts(), keep=False)
        H.eq((t1["ok"], t1["skipped"], t1["fail"]), (2, 0, 1), "单文件失败应计入 fail")
        H.eq(t1["total"], t1["ok"] + t1["skipped"] + t1["fail"])
        # 多线程下 results 顺序按完成先后，不能按下标假设；按 path 找失败项
        bad = [r for r in t1["results"] if r.get("status") == "error"]
        H.eq(len(bad), 1, "失败结果条数")
        H.is_true(bad[0].get("error"), "失败结果应带错误信息")
        H.eq(bad[0]["path"], c, "失败项应是被标记的文件")
    finally:
        cleanup(root)


def test_run_task_cancel_marks_canceled():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir, "a.jpg")
        files = [{"path": a, "root": src_dir}]
        srv.subprocess.run = FakeRun()
        t = srv.new_task(files, out_dir, base_opts(), False)
        t["cancel"] = True
        srv.run_task(t)
        H.eq(t["state"], "canceled", "取消后状态应为 canceled")
        # 取消是"提前收工"，done 才是已处理数；未处理的不计入任何桶
        H.eq(t["done"], t["ok"] + t["skipped"] + t["fail"], "done 必须等于 ok+skipped+fail")
        H.is_true(t["finished"] is not None)
    finally:
        cleanup(root)


def test_skip_policy_respects_foreign_existing_file():
    """输出目录里"不是我们压的"同名文件：skip 策略必须原样保留、且不 spawn 引擎。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir)
        foreign = os.path.join(out_dir, "a.jpg")
        with open(foreign, "wb") as f:
            f.write(b"FOREIGN-OTHER-TOOL")
        run = FakeRun()
        srv.subprocess.run = run
        t = _run_task([{"path": a, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t["ok"], t["skipped"], t["fail"]), (0, 1, 0), "外部已有文件应记为 skipped")
        H.eq(len(run.calls), 0, "外部已有文件不应 spawn 引擎")
        H.eq(read_bytes(foreign), b"FOREIGN-OTHER-TOOL", "外部文件必须原样保留")
        H.eq(t["done"], t["ok"] + t["skipped"] + t["fail"])
    finally:
        cleanup(root)


def test_param_change_never_overwrites_foreign_file():
    """skip 放过的外部文件不能登记归属，否则改参数会把用户自己的文件覆盖掉。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir)
        foreign = os.path.join(out_dir, "a.jpg")
        with open(foreign, "wb") as f:
            f.write(b"FOREIGN-OTHER-TOOL")
        run = FakeRun()
        srv.subprocess.run = run
        t1 = _run_task([{"path": a, "root": src_dir}], out_dir, base_opts(jpeg_quality=90), keep=False)
        H.eq(t1["skipped"], 1)
        H.is_true(not srv.has_record(a), "skip 放过的外部文件不应写入归属记录")
        # 改参数再来一遍：仍然必须原样保留
        t2 = _run_task([{"path": a, "root": src_dir}], out_dir, base_opts(jpeg_quality=55), keep=False)
        H.eq((t2["ok"], t2["skipped"]), (0, 1), "外部文件在改参数后也应继续跳过")
        H.eq(len(run.calls), 0, "外部文件在任何参数下都不应被覆盖")
        H.eq(read_bytes(foreign), b"FOREIGN-OTHER-TOOL", "外部文件必须原样保留")
    finally:
        cleanup(root)


def test_force_recompress_ignores_existing_and_records():
    """「全部重压」：即使文件已存在、记录也在，也必须真重压。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir)
        files = [{"path": a, "root": src_dir}]
        run = FakeRun()
        srv.subprocess.run = run
        _run_task(files, out_dir, base_opts(), keep=False)
        H.eq(len(run.calls), 1)
        srv.clear_results()
        t = srv.new_task(files, out_dir, base_opts(), False, True)
        srv.run_task(t)
        H.eq((t["ok"], t["skipped"]), (1, 0), "force_recompress 时不允许 skipped")
        H.eq(len(run.calls), 2, "force_recompress 必须再次 spawn 引擎")
    finally:
        cleanup(root)


def test_task_snapshot_has_new_fields():
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        a = make_src(src_dir)
        srv.subprocess.run = FakeRun()
        t = _run_task([{"path": a, "root": src_dir}], out_dir, base_opts(), keep=False)
        snap = srv.task_snapshot(t)
        for key in ("state", "total", "done", "ok", "skipped", "fail", "results", "error", "elapsed"):
            H.is_true(key in snap, "任务快照缺少字段 %s" % key)
        H.eq(snap["skipped"], 0)
        H.eq(snap["total"], 1)
    finally:
        cleanup(root)


def test_build_clt_cmd_never_overwrites_unless_requested():
    cmd, ext = srv.build_clt_cmd("/x/a.jpg", "/out", base_opts(exist_policy="skip"), ".jpg")
    H.eq(cmd[cmd.index("-O") + 1], "never")
    H.eq(ext, ".jpg")
    cmd2, _ = srv.build_clt_cmd("/x/a.jpg", "/out", base_opts(exist_policy="overwrite"), ".jpg")
    H.eq(cmd2[cmd2.index("-O") + 1], "all")
    cmd3, _ = srv.build_clt_cmd("/x/a.jpg", "/out", base_opts(exist_policy="rename"), ".jpg")
    H.eq(cmd3[cmd3.index("-O") + 1], "never")


# ================================================================== P0-1 原子落盘
class FakePartialRun(object):
    """引擎写到一半就崩（退出码非零 + 留下半截文件）——闪退/被 OOM 杀掉的样子。"""

    def __init__(self, rc=9, nbytes=97):
        self.calls = []
        self.rc = rc
        self.nbytes = nbytes

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        parsed = fake_clt.parse_args([str(x) for x in cmd[1:]])
        src = parsed["src"]
        out_dir = parsed["out_dir"] or os.path.dirname(src)
        os.makedirs(out_dir, exist_ok=True)
        ext = os.path.splitext(src)[1].lower()
        base = os.path.splitext(os.path.basename(src))[0]
        target = os.path.join(out_dir, base + parsed["suffix"] + ext)
        with open(target, "wb") as f:
            f.write(b"HALF-WRITTEN" * (self.nbytes // 12 + 1))
        return FakeProc(rc=self.rc, err="encoder crashed midway")


def test_atomic_output_never_leaves_partial_file():
    """引擎中途崩掉：最终路径上不许出现半截文件，也不许残留临时目录。

    这是「闪退后接着压」的正确性根基 —— 只要最终路径上可能残留半成品，
    下次的 skip 判定就会把它当成"已完成"永久跳过（QA C02/C06）。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        bad = FakePartialRun(rc=9, nbytes=97)
        srv.subprocess.run = bad
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        H.eq((ok, out, status), (False, None, "error"), "引擎非零退出必须判失败且不返回产物")
        H.is_true(err, "失败必须带原因")
        H.eq(sorted(os.listdir(out_dir)), [],
              "失败后输出目录必须干净（既不许有半截成品，也不许残留 .caesium-tmp-*）")
        # 换回正常引擎再压：必须真的重压，而不是"目标已存在 → 跳过"
        run = FakeRun()
        srv.subprocess.run = run
        ok2, out2, err2, status2 = srv.compress_file(src, out_dir, base_opts(), ".jpg")
        H.eq((ok2, status2), (True, "ok"), "上次失败没留下半成品，这次必须真压")
        H.eq(len(run.calls), 1, "应 spawn 一次引擎")
        H.is_true(read_bytes(out2).startswith(b"FAKE"), "最终路径上应是完整成品")
        H.eq(sorted(os.listdir(out_dir)), ["a.jpg"], "残留临时目录必须清理")
    finally:
        cleanup(root)


def test_same_folder_crash_keeps_original_intact():
    """输出到原文件夹（覆盖原图）+ 引擎中途崩：原图必须一个字节都不变。"""
    root, src_dir, _out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "keep.jpg", data=b"PRECIOUS-ORIGINAL" * 2000)
        original = read_bytes(src)
        srv.subprocess.run = FakePartialRun(rc=9, nbytes=500)
        ok, out, err, status = srv.compress_file(src, src_dir,
                                                base_opts(exist_policy="overwrite"), ".jpg")
        H.eq((ok, status), (False, "error"))
        H.eq(read_bytes(src), original, "覆盖原图失败时原图必须完好（不许被截断）")
        H.eq(sorted(os.listdir(src_dir)), ["keep.jpg"], "不许残留临时目录")
    finally:
        cleanup(root)


def test_rename_policy_crash_leaves_no_copy():
    """rename（保留两份）+ 引擎中途崩：不许产出一个"半截的副本"。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "r.jpg", data=b"ORIGINAL")
        srv.subprocess.run = FakePartialRun(rc=9, nbytes=60)
        ok, out, err, status = srv.compress_file(src, out_dir,
                                                base_opts(exist_policy="rename"), ".jpg")
        H.eq((ok, status), (False, "error"))
        H.eq(sorted(os.listdir(out_dir)), [], "失败时不许落盘任何副本")
        H.eq(read_bytes(src), b"ORIGINAL", "源文件不许被改动")
    finally:
        cleanup(root)


# ================================================================== P1-2 returncode
def test_engine_nonzero_exit_is_failure_even_with_stale_output():
    """引擎非零退出、但目标路径上留着上一次的产物 → 必须报失败，旧产物原地不动。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "stale.jpg")
        srv.subprocess.run = FakeRun()
        ok1, out1, _e1, st1 = srv.compress_file(src, out_dir, base_opts(exist_policy="overwrite"), ".jpg")
        H.eq((ok1, st1), (True, "ok"))
        before = read_bytes(out1)
        # 引擎"直接失败、什么都不写"（磁盘满 / 源图损坏 / 引擎崩）
        srv.subprocess.run = FakeRun(fail_contains="stale.jpg")
        ok2, out2, err2, st2 = srv.compress_file(
            src, out_dir, base_opts(jpeg_quality=30, exist_policy="overwrite"), ".jpg")
        H.eq((ok2, out2, st2), (False, None, "error"), "引擎非零退出不得报成功")
        H.eq(read_bytes(out1), before, "失败时不许改动目标路径上的旧产物")
        H.is_true(err2 and "forced failure" in err2, "错误文案应来自引擎 stderr，实际 %r" % err2)
    finally:
        cleanup(root)


def test_run_task_engine_failure_is_never_recorded_as_ok():
    """run_task 层：引擎失败 → fail 计数 + 记录 status=error；下一次必须重压并成功。

    两种真实故障形态都要按住（只看"有没有产出文件"是不够的）：
      (a) 引擎直接失败、一个字节都没写（源图损坏 / 磁盘满）；
      (b) 引擎写了半截文件再崩（被 OOM 杀掉）——**有产物但不可信**，
          这正是必须靠 returncode 才能识别的那种，最容易被误记成 ok。
    """
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        # (a) 引擎直接失败、什么都没写
        srv.subprocess.run = FakeRun(fail_contains="a.jpg")
        t = _run_task([{"path": src, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t["ok"], t["skipped"], t["fail"]), (0, 0, 1), "引擎失败必须记 fail")
        H.eq(t["results"][0]["status"], "error")
        rec = srv.get_results().get(src)
        H.is_true(isinstance(rec, dict) and rec.get("status") == "error",
                  "失败只能记成 error，绝不能写成 ok 记录（否则永久跳过）")

        # (b) 引擎写了半截文件再崩 → 退出码非零，产物必须整个丢弃
        srv.subprocess.run = FakePartialRun(rc=9, nbytes=120)
        t_p = _run_task([{"path": src, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t_p["ok"], t_p["skipped"], t_p["fail"]), (0, 0, 1),
             "引擎留下半截文件也绝不能算成功")
        H.eq(t_p["results"][0]["status"], "error")
        H.is_true(not os.path.isfile(os.path.join(out_dir, "a.jpg")),
                  "半截文件不许落到最终路径")
        H.eq([n for n in os.listdir(out_dir) if n.startswith(".caesium-tmp")], [],
             "崩溃后临时目录必须清理")
        rec_p = srv.get_results().get(src)
        H.is_true(isinstance(rec_p, dict) and rec_p.get("status") == "error",
                  "留下半截文件的失败同样只能记 error（QA C11a）")

        # (c) 换回正常引擎 → 必须重压并成功，且记录能被判为已完成
        run = FakeRun()
        srv.subprocess.run = run
        t2 = _run_task([{"path": src, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t2["ok"], t2["skipped"]), (1, 0), "上次失败后必须重压")
        H.eq(len(run.calls), 1, "上次失败后必须重新 spawn 引擎")
        H.is_true(srv.result_is_done(src, srv.results_signature(base_opts()), out_dir) is not None,
                  "成功后记录必须能判为已完成")
    finally:
        cleanup(root)


# ================================================================== P1-4 并发互斥
class FakeSlowRun(FakeRun):
    """分块慢写，文件体由本次质量值构成：两个写者交错时内容会混在一起。"""

    def __init__(self, chunks=40, delay=0.01):
        FakeRun.__init__(self)
        self.chunks = chunks
        self.delay = delay

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        parsed = fake_clt.parse_args([str(x) for x in cmd[1:]])
        src = parsed["src"]
        out_dir = parsed["out_dir"] or os.path.dirname(src)
        os.makedirs(out_dir, exist_ok=True)
        ext = os.path.splitext(src)[1].lower()
        base = os.path.splitext(os.path.basename(src))[0]
        target = os.path.join(out_dir, base + parsed["suffix"] + ext)
        tag = ("<%s>" % (parsed["quality"] or "na")).encode("ascii")
        with open(target, "wb") as f:
            for _ in range(self.chunks):
                f.write(tag)
                f.flush()
                time.sleep(self.delay)
        return FakeProc(rc=0, out="fake slow: wrote %s" % os.path.basename(target))


def test_concurrent_same_output_is_serialized():
    """两个任务并发压同一个输出文件（参数不同）：最终文件只能是其中一次的完整产物。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "race.jpg", data=b"R" * 1000)
        run = FakeSlowRun(chunks=40, delay=0.01)
        srv.subprocess.run = run
        t1 = srv.new_task([{"path": src, "root": src_dir}], out_dir,
                          base_opts(exist_policy="overwrite", jpeg_quality=95), False)
        t2 = srv.new_task([{"path": src, "root": src_dir}], out_dir,
                          base_opts(exist_policy="overwrite", jpeg_quality=20), False)
        th1 = threading.Thread(target=srv.run_task, args=(t1,))
        th2 = threading.Thread(target=srv.run_task, args=(t2,))
        th1.start()
        time.sleep(0.05)          # 让 t1 先拿到锁并开始写
        th2.start()
        th1.join(60)
        th2.join(60)
        H.eq((t1["ok"], t2["ok"]), (1, 1), "两个任务都应是 ok")
        data = read_bytes(os.path.join(out_dir, "race.jpg"))
        tags = set(re.findall(rb"<\d+>", data))
        H.eq(len(data), 40 * len(b"<95>"), "最终文件应是「一次写入」的完整长度，实际 %d 字节" % len(data))
        H.eq(len(tags), 1, "文件里只允许有一个写者的标记，实际混入 %s" % sorted(tags))
        H.eq([n for n in os.listdir(out_dir) if n.startswith(".caesium-tmp")], [],
             "并发结束后不许残留临时目录")
    finally:
        cleanup(root)


def test_concurrent_skip_policy_writes_only_once():
    """skip 策略下两个任务同时压同一个目标：只能有一个真压，另一个必须看到成品后跳过。

    「存在性检查」如果不在 per-path 锁内，两个任务会同时判定"目标不存在"并各自压缩，
    skip 语义就形同虚设（而且白干一遍活）。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "once.jpg", data=b"O" * 1000)
        run = FakeSlowRun(chunks=20, delay=0.01)
        srv.subprocess.run = run
        t1 = srv.new_task([{"path": src, "root": src_dir}], out_dir, base_opts(), False)
        t2 = srv.new_task([{"path": src, "root": src_dir}], out_dir, base_opts(), False)
        th1 = threading.Thread(target=srv.run_task, args=(t1,))
        th2 = threading.Thread(target=srv.run_task, args=(t2,))
        th1.start()
        time.sleep(0.05)
        th2.start()
        th1.join(60)
        th2.join(60)
        H.eq(len(run.calls), 1, "同一目标只允许 spawn 一次引擎，实际 %d 次" % len(run.calls))
        H.eq(sorted([t1["ok"], t2["ok"]]), [0, 1], "恰好一个任务真压（ok=1）")
        H.eq(sorted([t1["skipped"], t2["skipped"]]), [0, 1], "恰好一个任务跳过（skipped=1）")
        H.eq(len(read_bytes(os.path.join(out_dir, "once.jpg"))), 20 * len(b"<90>"),
            "最终文件应是完整成品")
    finally:
        cleanup(root)


def test_sweep_removes_stale_tmp_dirs_only():
    """闪退残骸清理：只清 1 天前的 .caesium-tmp-*，别动别的任务正在写的临时目录。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        stale = os.path.join(out_dir, srv.TMP_PREFIX + "deadbeef")
        fresh = os.path.join(out_dir, srv.TMP_PREFIX + "cafe0000")
        for d in (stale, fresh):
            os.makedirs(d)
            with open(os.path.join(d, "half.jpg"), "wb") as f:
                f.write(b"HALF")
        old = time.time() - 2 * 24 * 3600
        os.utime(stale, (old, old))
        srv.subprocess.run = FakeRun()
        ok, out, err, status = srv.compress_file(make_src(src_dir), out_dir, base_opts(), ".jpg")
        H.eq((ok, status), (True, "ok"))
        H.is_true(not os.path.isdir(stale), "2 天前的闪退残骸必须被清掉")
        H.is_true(os.path.isdir(fresh), "刚建的临时目录不能清（可能还有任务在写）")
    finally:
        cleanup(root)


# ================================================================== P2-6 文案
def test_rename_skip_carries_tip_note():
    """rename（保留两份）模式第二次跳过时，结果里必须带「再存一份请点全部重压」的说明。

    逻辑按"续传正确性优先"保持不变（不为了多出一份而重压），只把话说清楚。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "two.jpg")
        srv.subprocess.run = FakeRun()
        t1 = _run_task([{"path": src, "root": src_dir}], out_dir,
                       base_opts(exist_policy="rename"), keep=False)
        H.eq((t1["ok"], t1["skipped"]), (1, 0), "第一次应压出副本")
        t2 = _run_task([{"path": src, "root": src_dir}], out_dir,
                       base_opts(exist_policy="rename"), keep=False)
        H.eq((t2["ok"], t2["skipped"]), (0, 1), "第二次应跳过（续传正确性优先）")
        note = t2["results"][0].get("note") or ""
        H.contains(note, "全部重压", "rename 跳过时必须提示「全部重压」才能再存一份，实际 %r" % note)
        # 非 rename 策略不该带这个提示（避免刷屏）
        t3 = _run_task([{"path": src, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t3["ok"], t3["skipped"]), (0, 1), "skip 策略应跳过")
        H.eq(t3["results"][0].get("note"), None, "skip 策略的结果不该带 rename 提示")
    finally:
        cleanup(root)


# ================================================================== P2-7 同大小替换
def test_result_is_done_detects_same_size_replace():
    """产物被换成"同样大小"的别的文件 → 不能再判已完成（比 out_mtime）。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir)
        srv.subprocess.run = FakeRun()
        _run_task([{"path": src, "root": src_dir}], out_dir, base_opts(), keep=False)
        out = os.path.join(out_dir, "a.jpg")
        sig = srv.results_signature(base_opts())
        H.is_true(srv.result_is_done(src, sig, out_dir) is not None, "同参数应判已完成")
        size = os.path.getsize(out)
        with open(out, "wb") as f:
            f.write(b"Z" * size)                      # 同大小替换
        stamp = time.time() + 5
        os.utime(out, (stamp, stamp))                 # 保证 mtime 一定不同
        H.is_true(srv.result_is_done(src, sig, out_dir) is None,
                  "大小相同但 out_mtime 变了 → 必须判未完成并重压")
        H.is_true(srv.result_is_done(src, srv.results_signature(base_opts(jpeg_quality=10)), out_dir) is None,
                  "换参数也必须判未完成")
    finally:
        cleanup(root)


# ================================================================== v1.7.2 尺寸解析
def test_jpeg_size_never_reads_whole_file():
    """8MB 的 JPEG：尺寸解析最多只许读几百 KB，绝不能整文件读进内存。

    老实现 `data = f.read()` 一张 8MB 照片白读 8MB；扫 2000 张就是 15.6 GB
    —— 线上 bug 的「飞牛后台还在拼命读取像是没停」。"""
    root, src_dir, _out, _work = setup_tmp()
    try:
        # SOF 前面塞 3 段 64KB 的 APP2（模拟 ICC/EXIF 很大的照片），总长 > 8MB
        data = build_jpeg(width=640, height=480, app_segments=3)
        data = data + b"\x00" * (8 * 1024 * 1024 - len(data))
        p = make_file(src_dir, "big.jpg", data)
        H.is_true(os.path.getsize(p) >= 8 * 1024 * 1024, "夹具应是 8MB 以上的图")

        wh, read_bytes_n = _measure_read_bytes(srv.get_image_size, p)
        H.eq(wh, (480, 640), "宽高应与旧实现一致（注意：旧实现返回的顺序是 (height, width)）")
        H.is_true(read_bytes_n < 600 * 1024,
                  "尺寸解析读取量应 < 600KB，实际 %.1f KB（整文件是 %d KB）" %
                  (read_bytes_n / 1024.0, os.path.getsize(p) / 1024))
        print("     [实测] 8MB JPEG 只读了 %.1f KB（整文件 %.1f MB）" %
              (read_bytes_n / 1024.0, os.path.getsize(p) / 1048576.0))

        # 对照：旧实现（整文件读）确实把 8MB 全读了一遍
        old_wh, old_read = _measure_read_bytes(_old_jpeg_size, p)
        H.eq(old_wh, wh, "新旧实现解析结果必须相同")
        H.is_true(old_read >= 8 * 1024 * 1024, "旧实现应读满整个文件（%.1f MB）" % (old_read / 1048576.0))
        print("     [对照] 旧实现读了 %.1f MB" % (old_read / 1048576.0))
    finally:
        cleanup(root)


def test_jpeg_size_matches_old_parser():
    """同样的图片，新旧实现返回同样的宽高（含各种 marker 排布 / 截断 / 异常情况）。"""
    root, src_dir, _out, _work = setup_tmp()
    cases = {
        "plain.jpg": build_jpeg(320, 200),
        "sof1.jpg": build_jpeg(1000, 500).replace(b"\xff\xc0", b"\xff\xc1"),
        "sof2.jpg": build_jpeg(64, 64).replace(b"\xff\xc0", b"\xff\xc2"),
        # marker 之间塞 0xFF 填充（旧实现的 pos+=1 分支）
        "filler.jpg": b"\xff\xd8" + b"\xff" * 30 + build_jpeg(11, 22)[2:],
        # APP1 + APP2 + COM 混排
        "many_segments.jpg": (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", 40) + b"\x00" * 38
                              + b"\xff\xfe" + struct.pack(">H", 18) + b"\x00" * 16
                              + build_jpeg(77, 33)[2:]),
        # 只有 SOI（截断）
        "only_soi.jpg": b"\xff\xd8",
        # SOF 之前就被截断
        "cut_before_sof.jpg": build_jpeg(10, 10, app_segments=1)[:200],
        # 没有 SOF（只有 SOS 直接进扫描数据）
        "no_sof.jpg": b"\xff\xd8\xff\xda" + struct.pack(">H", 12) + b"\x00" * 10 + b"\xff\xd9",
        # 段长度非法（< 2）
        "bad_len.jpg": b"\xff\xd8\xff\xe0\x00\x01" + b"\x00" * 20,
    }
    try:
        for name, data in cases.items():
            p = make_file(src_dir, name, data)
            H.eq(srv.get_image_size(p), _old_jpeg_size(p), "%s 的宽高应与旧实现一致" % name)
        # 非 JPEG 快路径不受影响
        png = make_file(src_dir, "a.png", PNG_COMPLETE[:16] + struct.pack(">II", 800, 600) + PNG_COMPLETE[24:])
        H.eq(srv.get_image_size(png), (800, 600), "PNG 宽高应正常")
        gif = make_file(src_dir, "a.gif", b"GIF89a" + struct.pack("<HH", 120, 90) + b"\x00" * 30)
        H.eq(srv.get_image_size(gif), (120, 90), "GIF 宽高应正常")
    finally:
        cleanup(root)


# ================================================================== v1.7.2 异步扫描
def test_scan_returns_immediately_and_pages_are_complete():
    """/api/scan 必须立刻返回（不等扫盘）；分页拼起来 == 全量结果，无重复无遗漏。"""
    root, src_dir, _out, _work = setup_tmp()
    n = 1600
    try:
        _make_many(src_dir, n)
        t0 = time.time()
        sid = srv.new_scan([src_dir])
        elapsed_ms = (time.time() - t0) * 1000
        H.is_true(elapsed_ms < 50,
                  "new_scan 必须立刻返回（本次 %.1f ms，同步扫 1600 个文件要几百 ms）" % elapsed_ms)
        print("     [实测] POST /api/scan 耗时 %.1f ms（%d 个文件）" % (elapsed_ms, n))

        # 进度可观测：running 期间 scanned/total 单调增长
        progress, deadline = [], time.time() + 120
        while time.time() < deadline:
            with srv.SCAN_LOCK:
                st = srv.SCANS[sid]
            snap = srv.scan_snapshot(st, 0, 1)
            progress.append((snap["scanned"], snap["total"]))
            if snap["state"] != "running":
                break
            time.sleep(0.02)
        H.is_true(len(set(p[1] for p in progress)) >= 2,
                  "扫描过程中 total 必须递增（实际采样 %s）" % progress[:5])
        H.is_true(all(p[0] <= q[0] for p, q in zip(progress, progress[1:])),
                  "scanned 必须单调不减")
        print("     [实测] 进度采样 %s … %s（共 %d 次）" %
              (progress[:3], progress[-1:], len(progress)))

        snap, files, pages = _wait_scan(sid)
        H.eq(snap["state"], "done", "扫描应正常结束")
        H.eq(snap["total"], n, "total 应为 %d" % n)
        H.eq(len(files), n, "分页拼起来应有 %d 条" % n)
        H.eq(len(set(x["path"] for x in files)), n, "分页拼接不许有重复")
        expect = set(os.path.join(src_dir, x) for x in os.listdir(src_dir))
        H.eq(set(x["path"] for x in files), expect, "分页拼接不许有遗漏")
        H.is_true(snap["scanned"] >= n, "scanned（已访问条目数）应 ≥ 文件数，实际 %d" % snap["scanned"])
        H.eq(snap["truncated"], False)
        H.eq(pages, [500, 500, 500, 100], "1600 条按 limit=500 应恰好分 4 页（500/500/500/100）")
        print("     [实测] 分页页大小 %s（共 %d 页）" % (pages, len(pages)))

        # 同步版本（同后缀代码）结果必须与异步分页一致
        sync_files = srv.scan_images([src_dir])
        H.eq(set(x["path"] for x in sync_files), expect, "同步扫描结果应与异步一致")
    finally:
        cleanup(root)


def test_scan_single_flight():
    """单飞：连发两次扫描，旧任务必须变 canceled，且同时只有一个在跑。"""
    root, src_dir, _out, _work = setup_tmp()
    try:
        _make_many(src_dir, 800)
        sid1 = srv.new_scan([src_dir])
        time.sleep(0.02)
        sid2 = srv.new_scan([src_dir])
        with srv.SCAN_LOCK:
            s1 = dict(srv.SCANS[sid1])
            running = [x["id"] for x in srv.SCANS.values() if x["state"] == "running"]
        H.eq(s1["state"], "canceled", "旧扫描必须被置为 canceled")
        H.is_true(sid2 in running, "新扫描应处于 running")
        H.is_true(len(running) <= 1, "同一时间只允许一个扫描在跑，实际 %s" % running)
        # 旧线程真的停了：等一会儿它的 total 不再增长
        total1 = len(s1["files"])
        time.sleep(0.3)
        with srv.SCAN_LOCK:
            total2 = len(srv.SCANS[sid1]["files"])
        H.eq(srv.SCANS[sid1]["state"], "canceled")
        H.is_true(total2 <= total1 + 2, "被取消的旧扫描不该继续收录（%d → %d）" % (total1, total2))
        _wait_scan(sid2, timeout=120)      # 让新扫描跑完，别影响后续用例
    finally:
        cleanup(root)


def test_scan_cancel_stops_growth():
    """取消：立刻变 canceled，之后 total/scanned 都不再增长。"""
    root, src_dir, _out, _work = setup_tmp()
    try:
        _make_many(src_dir, 800)
        sid = srv.new_scan([src_dir])
        time.sleep(0.03)
        H.is_true(srv.cancel_scan(sid), "取消应命中")
        with srv.SCAN_LOCK:
            st = srv.SCANS[sid]
        snap1 = srv.scan_snapshot(st, 0, 1)
        H.eq(snap1["state"], "canceled", "取消后 state 必须是 canceled")
        time.sleep(0.4)
        snap2 = srv.scan_snapshot(st, 0, 1)
        H.eq(snap2["state"], "canceled")
        H.eq(snap2["total"], snap1["total"], "取消后 total 不许再增长")
        H.eq(snap2["scanned"], snap1["scanned"], "取消后 scanned 不许再增长")
        H.is_true(snap2["total"] < 800, "取消应真的打断了扫描（已收 %d/800）" % snap2["total"])
        print("     [实测] 取消时已收 %d/800 个，之后不再增长" % snap2["total"])
        H.eq(srv.cancel_scan("no-such-id"), False, "未知 scan_id 应返回 False")
    finally:
        cleanup(root)


def test_scan_cap_sets_truncated():
    """上限：达到 SCAN_MAX_FILES 就停并置 truncated=true（防几万条把内存/JSON 打爆）。"""
    root, src_dir, _out, _work = setup_tmp()
    old_cap = srv.SCAN_MAX_FILES
    try:
        _make_many(src_dir, 30)
        srv.SCAN_MAX_FILES = 10
        sid = srv.new_scan([src_dir])
        snap, files, _pages = _wait_scan(sid)
        H.eq(snap["total"], 10, "达到上限就停，total 应为 10")
        H.is_true(snap["truncated"], "达到上限必须置 truncated=true")
        H.eq(len(files), 10)
        H.eq(snap["state"], "done", "到上限是正常结束，不是 error")
    finally:
        srv.SCAN_MAX_FILES = old_cap
        cleanup(root)


def test_scan_single_file_and_missing_path():
    """paths 里可以给单个文件；不存在/空串要安静跳过（畸形输入不许打挂扫描线程）。"""
    root, src_dir, _out, _work = setup_tmp()
    try:
        one = make_src(src_dir, "one.jpg", data=build_jpeg(120, 60))
        other = make_file(src_dir, "note.txt", b"not an image")
        sid = srv.new_scan([one, other, os.path.join(src_dir, "nope.jpg"), ""])
        snap, files, _pages = _wait_scan(sid)
        H.eq(snap["state"], "done", "畸形输入不该让扫描变成 error")
        H.eq([x["path"] for x in files], [one], "只应收到那个图片文件")
        H.eq(files[0]["root"], src_dir, "单文件的 root 应是它所在目录")
        H.is_true(files[0]["w"] is not None and files[0]["h"] is not None, "宽高应解析出来")
    finally:
        cleanup(root)


def test_jpeg_eoi_cross_chunk_boundary():
    """EOI 的 FF/D9 恰好被 64KB 回扫块边界拆开时，仍必须判「不是残骸」（QA A5）。

    否则一张合法 JPEG 会被当成闪退残骸，skip 策略被绕过 → 覆盖用户原图。
    同样要覆盖「回扫块 ↔ 尾部 2KB 窗口」的交界。
    """
    root, src_dir, _out, _work = setup_tmp()
    try:
        tail_len = srv.TRUNC_TAIL_BYTES
        chunk = srv.EOI_SCAN_CHUNK
        size = tail_len + 2 * chunk
        boundary = size - tail_len - chunk          # 64KB 块边界（更靠尾那块的起点）

        buf = bytearray(b"\x00" * size)
        buf[:2] = b"\xff\xd8"                      # SOI
        buf[boundary - 1] = 0xFF                   # FF 在更靠头的块末尾
        buf[boundary] = 0xD9                       # D9 在更靠尾的块开头
        p = make_file(src_dir, "cross.jpg", bytes(buf))
        H.eq(srv.image_looks_truncated(p), False,
             "FF/D9 跨 64KB 块边界时必须拼起来找到 EOI，不许误判残骸")

        buf2 = bytearray(b"\x00" * size)
        buf2[:2] = b"\xff\xd8"
        buf2[size - tail_len - 1] = 0xFF           # FF 在回扫块末尾
        buf2[size - tail_len] = 0xD9               # D9 在尾部 2KB 窗口开头
        p2 = make_file(src_dir, "cross_tail.jpg", bytes(buf2))
        H.eq(srv.image_looks_truncated(p2), False,
             "FF/D9 跨「回扫块 ↔ 尾部窗口」交界时也必须找到 EOI")

        # 边界上没有任何 EOI → 仍然是真残骸（不能因为滑窗把判定改松）
        buf3 = bytearray(bytes(buf))
        buf3[boundary - 1] = 0x00
        p3 = make_file(src_dir, "cross_cut.jpg", bytes(buf3))
        H.eq(srv.image_looks_truncated(p3), True, "没有 EOI 的残骸必须仍判 True")
    finally:
        cleanup(root)


def test_queue_save_is_atomic_and_cached():
    """队列落盘：先写 .tmp 再 replace（不许留半截 JSON），并维护内存缓存。"""
    root, _src, _out, work = setup_tmp()
    old_file = srv.QUEUE_FILE
    try:
        srv.QUEUE_FILE = os.path.join(work, "queue.json")
        srv.QUEUE_CACHE["files"] = None
        items = [{"path": "/s/%d.jpg" % i, "name": "%d.jpg" % i} for i in range(5)]
        H.is_true(srv.save_queue(items), "保存应成功")
        H.eq(len(srv.load_queue()), 5, "应能读回 5 条")
        H.is_true(not os.path.isfile(srv.QUEUE_FILE + ".tmp"), "不许残留 .tmp 文件")
        srv.QUEUE_CACHE["files"] = None                 # 模拟进程重启（缓存丢了）
        H.eq([x["path"] for x in srv.load_queue()], [x["path"] for x in items],
             "重启后应能从磁盘读回同样的顺序")
    finally:
        srv.QUEUE_FILE = old_file
        srv.QUEUE_CACHE["files"] = None
        cleanup(root)


# ============================================================ v1.7.4 GIF 直通复制
GIF_BYTES = b"GIF89a" + struct.pack("<HH", 120, 90) + b"\x00" * 40 + b"\x3b"


def test_gif_is_copied_not_compressed():
    """GIF 直通：不 spawn 引擎、输出与源逐字节相同、状态是 copied。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "a.gif", data=GIF_BYTES)
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(), ".gif")
        H.eq((ok, status), (True, "copied"), "GIF 必须走直通复制")
        H.eq(len(run.calls), 0, "GIF 直通绝不能 spawn 引擎")
        H.is_true(bool(out) and os.path.isfile(out), "应产出输出文件")
        H.eq(os.path.basename(out), "a.gif", "输出文件名保持 .gif")
        H.eq(read_bytes(out), GIF_BYTES, "输出必须与源逐字节相同")
        H.eq([n for n in os.listdir(out_dir) if n.startswith(".caesium-tmp")], [],
             "复制也要走临时目录，不许残留")
        H.eq(read_bytes(src), GIF_BYTES, "源文件不许被改动")
    finally:
        cleanup(root)


def test_gif_respects_exist_policies():
    """GIF 直通同样遵守 skip / overwrite / rename 三态，且原子落盘。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "a.gif", data=GIF_BYTES)
        run = FakeRun()
        srv.subprocess.run = run
        target = os.path.join(out_dir, "a.gif")

        # skip：目标已存在（用户自己的文件）→ 跳过，一个字节都不许动
        with open(target, "wb") as f:
            f.write(b"USER-OWN-GIF")
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(exist_policy="skip"), ".gif")
        H.eq((ok, status), (True, "skipped"), "skip 应跳过")
        H.eq(read_bytes(target), b"USER-OWN-GIF", "skip 不许改已有文件")

        # overwrite：原子覆盖成源内容
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(exist_policy="overwrite"), ".gif")
        H.eq((ok, status), (True, "copied"), "overwrite 应复制覆盖")
        H.eq(read_bytes(target), GIF_BYTES, "overwrite 应得到源内容")

        # rename：再连做两次 → a (2).gif / a (3).gif，绝不覆盖
        for n in (2, 3):
            ok, out, err, status = srv.compress_file(src, out_dir, base_opts(exist_policy="rename"), ".gif")
            H.eq((ok, status), (True, "copied"), "rename 应复制成新名字")
            H.eq(os.path.basename(out), "a (%d).gif" % n, "rename 命名应为 a (%d).gif" % n)
            H.eq(read_bytes(out), GIF_BYTES, "rename 产物内容应与源一致")
        H.eq(sorted(n for n in os.listdir(out_dir) if n.lower().endswith(".gif")),
             ["a (2).gif", "a (3).gif", "a.gif"], "三份 GIF 都在，原文件未被覆盖")
        H.eq(len(run.calls), 0, "三种策略都不许 spawn 引擎")
        H.eq([n for n in os.listdir(out_dir) if n.startswith(".caesium-tmp")], [],
             "不许残留 .caesium-tmp-*")
        H.eq(read_bytes(src), GIF_BYTES, "源文件不许被改")
    finally:
        cleanup(root)


def test_gif_same_folder_not_self_copied():
    """输出到原文件夹：GIF 不能自我复制，直接返回 copied（源即成品）。"""
    root, src_dir, _out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "a.gif", data=GIF_BYTES)
        before = os.path.getmtime(src)
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, src_dir, base_opts(exist_policy="overwrite"), ".gif")
        H.eq((ok, status), (True, "copied"), "同目录应直接判 copied")
        H.eq(os.path.abspath(out), os.path.abspath(src), "输出就是源本身")
        H.eq(read_bytes(src), GIF_BYTES, "源文件内容不变")
        H.eq(os.path.getmtime(src), before, "源文件 mtime 不变")
        H.eq(len(run.calls), 0, "不许 spawn 引擎")
        H.eq([n for n in os.listdir(src_dir) if n.startswith(".caesium-tmp")], [],
             "不许留下临时目录")
    finally:
        cleanup(root)


def test_gif_to_webp_still_uses_engine():
    """显式选「导出为 WebP」时，.gif 源仍要交给引擎转码 —— 直通分支不许劫持。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        src = make_src(src_dir, "a.gif", data=GIF_BYTES)
        run = FakeRun()
        srv.subprocess.run = run
        ok, out, err, status = srv.compress_file(src, out_dir, base_opts(format="webp"), ".gif")
        H.eq(len(run.calls), 1, "format=webp 必须 spawn 引擎转码")
        H.eq(status, "ok", "转码成功应记 ok")
        H.is_true(bool(out) and out.lower().endswith(".webp"), "应产出 .webp，实际 %r" % (out,))
    finally:
        cleanup(root)


def test_gif_resume_after_copy_is_skipped():
    """copied 记录必须落盘：重启后同参数再压 → skipped，0 次 spawn。"""
    root, src_dir, out_dir, work = setup_tmp()
    try:
        gif = make_src(src_dir, "anim.gif", data=GIF_BYTES)
        run = FakeRun()
        srv.subprocess.run = run
        t1 = _run_task([{"path": gif, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t1["ok"], t1["copied"], t1["skipped"], t1["fail"]), (0, 1, 0, 0),
             "首次应记 1 个 copied")
        H.eq(t1["results"][0]["status"], "copied")
        rec = srv.get_results().get(gif)
        H.is_true(isinstance(rec, dict) and rec.get("status") == "copied",
                  "copied 必须写进 results.json，否则断点续传不认识 GIF")

        # 模拟重启：清内存缓存，从磁盘重新读
        srv.RESULTS = {"version": 1, "records": {}}
        srv.RESULTS_FILE = os.path.join(work, "results.json")
        srv.load_results()
        run.calls.clear()
        t2 = _run_task([{"path": gif, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t2["ok"], t2["copied"], t2["skipped"]), (0, 0, 1), "同参数再压应 judged 已完成 → skipped")
        H.eq(len(run.calls), 0, "续传不许 spawn 引擎")
        H.eq([n for n in os.listdir(out_dir) if n.startswith(".caesium-tmp")], [])
    finally:
        cleanup(root)


def test_task_identity_includes_copied():
    """混合 jpg/gif：total == ok + copied + skipped + fail；快照带 copied。"""
    root, src_dir, out_dir, _work = setup_tmp()
    try:
        jpg = make_src(src_dir, "p.jpg", data=JPEG_COMPLETE)
        g1 = make_src(src_dir, "g1.gif", data=GIF_BYTES)
        g2 = make_src(src_dir, "g2.gif", data=GIF_BYTES + b"\x00\x00")
        run = FakeRun()
        srv.subprocess.run = run
        t = _run_task([{"path": jpg, "root": src_dir},
                       {"path": g1, "root": src_dir},
                       {"path": g2, "root": src_dir}], out_dir, base_opts(), keep=False)
        H.eq((t["ok"], t["copied"], t["skipped"], t["fail"]), (1, 2, 0, 0), "1 jpg + 2 gif")
        H.eq(t["total"], t["ok"] + t["copied"] + t["skipped"] + t["fail"],
             "恒等式 total == ok + copied + skipped + fail")
        snap = srv.task_snapshot(t)
        H.eq(snap["copied"], 2, "任务快照必须带 copied")
        H.eq(snap["done"], 3)
        H.eq(len(run.calls), 1, "只有 jpg 那一次 spawn")

        # 取消场景：done == ok + copied + skipped + fail（total 可以大于 done）
        t2 = srv.new_task([{"path": jpg, "root": src_dir}, {"path": g1, "root": src_dir}],
                          out_dir, base_opts(), False)
        t2["cancel"] = True
        srv.run_task(t2)
        H.eq(t2["state"], "canceled")
        H.eq(t2["done"], t2["ok"] + t2["copied"] + t2["skipped"] + t2["fail"],
             "取消场景 done == ok + copied + skipped + fail")
        H.is_true(t2["done"] < t2["total"], "取消时 done 应小于 total")
    finally:
        cleanup(root)


if __name__ == "__main__":
    H.finish(dict(globals()))
