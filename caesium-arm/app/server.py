#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Caesium for fnOS — 一比一复刻原版界面的图片压缩工具（v1.8.1 多线程版）
左侧文件列表(名称/分辨率/大小/已节省/状态) + 右侧三标签(压缩选项/图片尺寸/输出)
压缩选项：JPEG质量 / PNG优化级别 / WebP质量 / TIFF压缩算法，JPEG色度二次采样+渐进式，PNG Zopfli
v1.7.1 新增：断点续传（results.json 逐文件落盘 + 参数指纹校验）、已存在文件三态策略
（跳过 / 覆盖 / 保留两份）、任务异常兜底不卡 running、/api/img 流式发送防 OOM
v1.7.1 修复：落盘改为「压到 .caesium-tmp-<uuid>/ → 校验引擎退出码 → os.replace」原子替换
（闪退/引擎崩溃时最终路径上不会出现半个文件）、同一输出路径并发互斥、记录里加 out_mtime
v1.7.1 修复②：skip 策略多加一个「残骸例外」——v1.7.0 的引擎是直写最终路径的，它闪退后
必然在输出目录留下截断的图；这些旧残骸非空又没有归属记录，若按 skip 永久跳过，用户就会
拿着坏图还以为「已跳过」。image_looks_truncated() 识别出来照常重压（os.replace 天然覆盖）
v1.7.2 修复：大目录扫描异步化——/api/scan 改为「立刻返回 scan_id + 后台线程扫 + 分页取结果」
（原来同步扫盘，几万文件让飞牛网关超时回 HTML，前端报 Unexpected token '<'；且超时后线程
还在走盘、反复点击会并行扫同一棵树）。尺寸解析也不再整文件读取：JPEG 走 64KB 分块、
上限 512KB，读完 SOF 就停（一张 8MB 照片原来要读满 8MB）。前端所有 fetch 走统一安全
JSON 解析（收到 HTML 错误页时提示「服务未响应或超时」，不再把解析异常丢给用户）
v1.7.3 修复（可理解性）：列表文件名下显示**所属文件夹名**（不同目录的同名图一眼可辨），
重复添加时的提示由「跳过 N 个重复」改为「N 个文件已在列表中（同一路径），未重复添加；
如需全部重加请先点「清空」」。文件始终按完整 path 去重，没有丢过文件。
v1.7.4 新增：**GIF 直通复制** —— `format=keep` 且源是 .gif 时不调引擎、直接复制一份
到输出目录（状态记为「已复制」，节省率显示「—」）。引擎只有全局 -q、没有 GIF 专属
质量参数，且自带"没变小就不采用"逻辑，压不动却记成 ok + 0%，既慢又误导（用户要求）。
v1.7.5 新增：**列表分页 + 渲染重写** —— 上万文件时整表铺进 DOM 会卡死，现在只渲染当前页
（默认 5000 条/页，可切 1000/2000/5000/10000）；行内事件改为 tbody 事件委托、
去重从 O(n²) 的 files.some 改成 Set 索引、断点续传查询分批、任务提交改为队列引用
（大列表的 POST 体会撞飞牛网关 1MB 上限，超了直接 413）
v1.8.1 修复（P0/P1）：results.json 落盘改为「FLUSH_LOCK + uuid 临时文件 + generation
校验」串行化，彻底杜绝并发覆盖/损坏 JSON 与 DIRTY 提前清零；CAESIUM_WORKERS 对前端
自动档 workers=0 也生效；取消可 terminate 本任务在途 caesiumclt（不误伤其它任务）；
新增跨任务的全局进程预算（min(CPU,16)），避免多开压垮小内存 NAS。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CLT = os.environ.get("CAESIUM_CLT") or os.path.join(APP_DIR, "caesiumclt")
MAX_JSON = 8 * 1024 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
STREAM_CHUNK = 64 * 1024            # /api/img 流式发送分块大小
MAX_IMAGE_BYTES = 64 * 1024 * 1024  # 单张预览图上限，超过直接 413（防 OOM）
PREVIEW_DIR = None  # 由 main 设置
GW_PREFIX = "/app/caesium"  # 飞牛统一网关前缀（micro_app 模式），server 自行剥离
SDK_FILE = os.path.join(APP_DIR, "www", "trimjs-web-app.js")  # 飞牛官方授权 SDK（@trimjs/web-app）
# 独立浏览器授权回调页（openAppAuth → callback.html → sessionStorage → 跳回应用）
CALLBACK_PAGE = (
    "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>Caesium · 授权回调</title>"
    "<style>body{font-family:system-ui,sans-serif;background:#14161a;color:#e6e8ec;"
    "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
    "pre{white-space:pre-wrap;word-break:break-all;color:#98a0b3}</style></head><body>"
    "<main style=\"max-width:560px;padding:24px\"><h2>正在处理授权结果…</h2>"
    "<pre id=\"detail\" style=\"white-space:pre-wrap;word-break:break-all;color:#98a0b3\"></pre></main>"
    "<script>"
    "(function(){var u=new URL(location.href);"
    "var paths=u.searchParams.getAll('path').concat(u.searchParams.getAll('paths'));"
    "var result={status:u.searchParams.get('status'),paths:paths,state:u.searchParams.get('state')};"
    "document.getElementById('detail').textContent=JSON.stringify(result,null,2);"
    "var clean=paths.filter(function(p){return typeof p==='string'&&p.startsWith('/');});"
    "if(clean.length){try{sessionStorage.setItem('caesium.pendingPaths',JSON.stringify(clean));}catch(e){}}"
    "var st=sessionStorage.getItem('caesium.authState');"
    "if(result.state&&st&&result.state!==st){"
    "document.getElementById('detail').textContent+='\\n\\n\u26a0\ufe0f state \u6821\u9a8c\u5931\u8d25\uff0c\u5df2\u5ffd\u7565\u672c\u6b21\u7ed3\u679c';}"
    "setTimeout(function(){location.replace('/app/caesium/');},1200);})();"
    "</script></body></html>"
)

# ---------------------------------------------------------------- 图片尺寸
# JPEG 的宽高在 SOF 段里，正常图片都在文件开头几百字节内。老实现为了找 marker
# 直接 `data = f.read()` 把**整张图读进内存**：一张 8MB 的照片就白读 8MB，扫 2000
# 张 = 读 15.6 GB —— 这就是用户说的「飞牛后台还在拼命读取像是没停」。
# 现在按 64KB 分块读、最多读 512KB 就放弃，读到 SOF / SOS(0xDA) / EOI(0xD9) 立刻停。
JPEG_CHUNK = 64 * 1024        # 每次读多少
JPEG_MAX_SCAN = 512 * 1024    # 最多读多少（超过说明这张图的元数据异常膨胀，放弃解析）
JPEG_SOF_MARKERS = frozenset(m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC))
JPEG_STANDALONE = frozenset((0xD8, 0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7))


def _jpeg_size(path):
    """分块读 JPEG 的 SOF 段拿到 (w, h)；失败返回 None。

    读入量上限 = JPEG_MAX_SCAN（512KB），**绝不整文件读**；正常照片实际只读
    64KB 一块。语义与旧的「整文件读 + 顺序扫 marker」完全一致。
    """
    try:
        f = open(path, "rb")
    except OSError:
        return None
    try:
        buf = b""
        eof = False
        read_total = 0
        pos = 2                       # 跳过 SOI(FFD8)
        while True:
            # 保证 buf 里至少够解析 pos 处的 marker（需要 pos+9 字节）
            while not eof and len(buf) < pos + 9 and read_total < JPEG_MAX_SCAN:
                chunk = f.read(min(JPEG_CHUNK, JPEG_MAX_SCAN - read_total))
                if not chunk:
                    eof = True
                    break
                buf += chunk
                read_total += len(chunk)
            if pos + 9 >= len(buf):
                return None           # 数据不够（文件被截断，或元数据超过 512KB 上限）
            if buf[pos] != 0xFF:
                pos += 1
                continue
            marker = buf[pos + 1]
            if marker in JPEG_STANDALONE:
                pos += 2
                continue
            if marker in (0xD9, 0xDA):
                return None           # EOI / 开始扫描数据 → 再往后也没有尺寸信息了
            seg_len = struct.unpack(">H", buf[pos + 2:pos + 4])[0]
            if seg_len < 2:
                return None
            if marker in JPEG_SOF_MARKERS:
                return struct.unpack(">HH", buf[pos + 5:pos + 9])
            pos += 2 + seg_len
    except Exception:
        return None
    finally:
        try:
            f.close()
        except Exception:
            pass


def get_image_size(path):
    """取图片宽高 (w, h)；解析不出来返回 None。

    只读文件头：PNG/GIF/BMP/WebP 读前 64 字节，JPEG 走 _jpeg_size()（≤512KB 分块）。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except Exception:
        return None
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            if head[12:16] == b"IHDR" and len(head) >= 24:
                return struct.unpack(">II", head[16:24])
        elif head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
            return struct.unpack("<HH", head[6:10])
        elif head[:2] == b"BM" and len(head) >= 26:
            return struct.unpack("<i", head[18:22])[0], abs(struct.unpack("<i", head[22:26])[0])
        elif head[:4] == b"RIFF" and head[8:12] == b"WEBP" and len(head) >= 30:
            vp = head[12:16]
            if vp == b"VP8X":
                return struct.unpack("<I", head[24:27] + b"\x00")[0] & 0xFFFFFF, \
                       struct.unpack("<I", head[27:30] + b"\x00")[0] & 0xFFFFFF
            if vp == b"VP8 " and len(head) >= 30:
                return struct.unpack("<H", head[26:28])[0] & 0x3FFF, \
                       struct.unpack("<H", head[28:30])[0] & 0x3FFF
        elif head[:2] == b"\xff\xd8":
            return _jpeg_size(path)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 闪退残骸识别
# 背景：v1.7.0 的引擎是「直写最终路径」的，进程被杀/闪退时输出目录里必然留下
# 一个**被截断的图**。升到 v1.7.1 后，这些旧残骸「非空、又没有归属记录」，
# skip 策略会把它们当成"已存在的文件"永久跳过 —— 用户拿到坏图还显示「已跳过」。
# 所以 skip 判定要多一个例外：疑似残骸 → 照常重压。
#
# 判定刻意做得**极保守**（宁可漏判，不可误判）：
#   * 只有「有合法图片魔数、但缺少正常结束标记」才算残骸；
#   * JPEG 看文件尾 2KB 里有没有 FFD9（EOI）；PNG 看文件尾 2KB 里有没有
#     IEND chunk（含固定 CRC AE 42 60 82）；WebP 看 RIFF 声明的长度是否
#     比实际文件更长（只在"短于声明"时判残骸，尾部追加数据不算）；
#   * GIF / TIFF / BMP / AVIF / 无魔数 / 非图片 → 一律**不算残骸**
#     （没有低成本且不会误判的判据，宁可不做）。这是 C01a 的语义边界：
#     用户自己的同名文件必须继续被 skip 保护，绝不能被当成残骸覆盖掉。
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"IEND\xaeB`\x82"       # IEND chunk 头（长度 0）+ 固定 CRC，足够特异
RIFF_MAGIC = b"RIFF"
WEBP_TAG = b"WEBP"
TRUNC_HEAD_BYTES = 32              # 文件头只看这么多（判魔数足够）
TRUNC_TAIL_BYTES = 2048            # 文件尾只看这么多（判结束标记足够）
EOI_SCAN_CHUNK = 64 * 1024         # 尾部没有 EOI 时，按 64KB 从尾向前回扫
EOI_SCAN_MAX = 8 * 1024 * 1024     # 回扫预算：超出就"不判残骸"（宁可漏判，不可误判）


def _read_head_tail(path, head_n=TRUNC_HEAD_BYTES, tail_n=TRUNC_TAIL_BYTES):
    """读文件头 head_n 字节 + 文件尾 tail_n 字节，返回 (head, tail)。

    只做 seek/read 小段，**不把整个文件读进内存**（NAS 上动辄几十 MB 的原图，
    skip 判定是每张图都要跑的，不能有 O(文件大小) 的开销）。
    """
    head, tail = b"", b""
    with open(path, "rb") as f:
        head = f.read(head_n)
        size = f.seek(0, os.SEEK_END)
        if size:
            f.seek(max(0, size - tail_n))
            tail = f.read(tail_n)
    return head, tail


def _jpeg_has_eoi(path, size):
    """JPEG 里到底有没有 EOI（FFD9）。先看尾部 2KB，没有再按 64KB 从尾向前回扫。

    为什么不能只看尾部 2KB：手机 **Motion Photo**（以及某些相机/编辑器）会在 EOI
    之后再追加几 KB~几 MB 的元数据/视频帧，EOI 距文件尾远超 2KB。只看尾部就会把
    用户的正常照片判成"残骸"，然后在 skip 策略下**直接覆盖掉原图**（QA C01d）。

    回扫预算 EOI_SCAN_MAX（8MB）：用完还没找到就不再猜，返回 True（= 当它有 EOI，
    不判残骸）。正常图片 EOI 就在最后 2 字节附近，零额外读。

    分块之间用 **1 字节滑窗**（`carry`）：从尾向前回扫时，若 FF 落在前一块的最后一个
    字节、D9 落在后一块的第一个字节，两块各自独立匹配 `FFD9` 都会漏掉，合法 JPEG
    就会被判成残骸并覆盖用户原图（QA A5）。把"上一块（更靠文件尾）的首字节"留在
    内存里拼到本块末尾即可，**不额外读盘**（否则 8MB 预算会被 I/O 放大，QA A8）。
    预算仍按新增字节数计，语义不变。
    """
    tail_len = min(size, TRUNC_TAIL_BYTES) if size > 0 else 0
    with open(path, "rb") as f:
        if tail_len:
            f.seek(size - tail_len)
            tail = f.read(tail_len)
        else:
            tail = b""
        if JPEG_EOI in tail:
            return True
        carry = tail[:1]             # 更靠文件尾那一块的首字节（跨块拼接用）
        scanned = tail_len
        end = size - tail_len
        while end > 0:
            if scanned >= EOI_SCAN_MAX:
                return True          # 预算用完 → 无法判断 → 按"不是残骸"处理
            start = max(0, end - EOI_SCAN_CHUNK)
            f.seek(start)
            chunk = f.read(end - start)
            if JPEG_EOI in (chunk + carry):
                return True
            carry = chunk[:1]
            scanned += len(chunk)
            end = start
    return False                     # 文件里根本没有 EOI → 真的被截断了


def _webp_declares_more_than_exists(head, path):
    """WebP：RIFF 头里声明的总长度 > 实际文件长度 → 文件被截断。

    RIFF 布局：'RIFF' + <uint32 size, 小端> + 'WEBP' + chunks，
    其中 size = 文件总长 - 8。所以 size+8 > 实际长度 = 少了尾巴（截断）。
    反过来 size+8 < 实际长度（尾部追加了数据）**不算**残骸，避免误判。
    size 为 0 / 0xFFFFFFFF 是流式写出时的占位值，信息不可用 → 不判。
    """
    try:
        declared = struct.unpack("<I", head[4:8])[0]
    except struct.error:
        return False
    if declared in (0, 0xFFFFFFFF):
        return False
    try:
        actual = os.path.getsize(path)
    except OSError:
        return False
    return declared + 8 > actual


def image_looks_truncated(path):
    """这个文件是不是「闪退留下的半截图」（有图片魔数、缺结束标记）？

    True  = 疑似残骸 → 调用方应当重压（覆盖它），不能按 skip 跳过；
    False = 完整图片 / 非图片 / 无魔数 / 无法判断 → 按普通 skip 语义处理。

    JPEG 的 EOI 判定走 _jpeg_has_eoi()（尾部没有就向前回扫，最多 8MB）——
    只看尾部 2KB 会把带尾部元数据的 Motion Photo 误判成残骸并覆盖用户原图。
    其它格式仍按尾部 2KB 判。任何异常（文件消失、权限不足）都当成「不是残骸」，
    因为误判成残骸会覆盖用户的文件，代价远大于漏判。
    """
    try:
        head, tail = _read_head_tail(path)
    except (OSError, ValueError):
        return False
    if not head:
        return False
    try:
        if head[:2] == JPEG_SOI:
            try:
                size = os.path.getsize(path)
            except OSError:
                return False
            return not _jpeg_has_eoi(path, size)
        if head[:8] == PNG_MAGIC:
            return PNG_IEND not in tail
        if head[:4] == RIFF_MAGIC and head[8:12] == WEBP_TAG:
            return _webp_declares_more_than_exists(head, path)
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- 小工具
def _as_int(value, default=0):
    """宽松整数转换：'12' → 12，'abc' / None / [] → default（畸形请求不许打挂处理线程）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 任务管理
TASKS = {}
TASKS_LOCK = threading.Lock()


# ---------------------------------------------------------------- 结果持久化（断点续传）
# 每个文件的压缩结果逐条落到 <workdir>/results.json，进程闪退/重启后仍能判断
# 「这个文件在**当前参数**下已经压好了」，从而避免重头再压一遍。
RESULTS_FILE = None
RESULTS = {"version": 1, "records": {}}
RESULTS_LOCK = threading.Lock()          # 保护 RESULTS / DIRTY / GENERATION 等内存状态
RESULTS_DIRTY = 0
RESULTS_LAST_FLUSH = 0.0
RESULT_FLUSH_INTERVAL = 1.0   # 节流：最快每 1 秒落一次盘
RESULT_FLUSH_EVERY = 5        # 或每 5 个文件强制落一次盘

# P0 修复：落盘单独一把锁，避免阻塞计数锁；generation 用来识别"快照取出后又有新变更"。
# FLUSH_LOCK 串行化 open→write→os.replace，杜绝旧快照覆盖新快照 / rename 后 fd 被污染。
FLUSH_LOCK = threading.Lock()
RESULTS_GENERATION = 0        # RESULTS 每次变更 +1（record / clear / load）
RESULTS_FLUSHED_GEN = 0       # 最后一次**成功落盘**的快照 generation
RESULTS_FLUSHED_FILE = None   # FLUSHED_GEN 对应的 RESULTS_FILE（换 workdir 时自动归零）

# 影响输出图像内容的参数（决定"算不算已完成"）；**不含** overwrite / exist_policy，
# 因为那是「怎么落盘」的策略，不改变图像内容。
SIG_FIELDS = ("mode", "lossless", "jpeg_quality", "png_quality", "png_opt_level",
              "webp_quality", "tiff_algorithm", "chroma", "progressive", "zopfli",
              "keep_exif", "keep_dates", "format", "suffix", "long_edge", "width",
              "height", "percent", "no_upscale", "max_size_kb")


def results_signature(options):
    """压缩参数指纹（md5 前 8 位）。参数一变指纹就变 → 已完成记录自动失效。"""
    opts = options or {}
    payload = {}
    for key in SIG_FIELDS:
        val = opts.get(key)
        if isinstance(val, bool):
            val = 1 if val else 0
        elif val is None:
            val = ""
        payload[key] = val
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def _log_flush_error(message):
    """落盘失败留痕（stderr + results.json.flush.log），绝不静默吞异常。"""
    line = "[caesium %s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        if RESULTS_FILE:
            with open(RESULTS_FILE + ".flush.log", "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _sync_flush_state_locked():
    """workdir 变化时把落盘进度归零（测试夹具每次换临时目录也随之隔离）。须持 RESULTS_LOCK。"""
    global RESULTS_FLUSHED_GEN, RESULTS_FLUSHED_FILE
    if RESULTS_FLUSHED_FILE != RESULTS_FILE:
        RESULTS_FLUSHED_FILE = RESULTS_FILE
        RESULTS_FLUSHED_GEN = 0


def load_results():
    """从磁盘恢复 results.json。文件损坏/不存在时返回空记录集（不抛异常）。"""
    global RESULTS, RESULTS_GENERATION, RESULTS_DIRTY, RESULTS_FLUSHED_GEN
    records, version = {}, 1
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            try:
                version = int(data.get("version", 1) or 1)
            except Exception:
                version = 1
            raw = data.get("records", {})
            if isinstance(raw, dict):
                for key, val in raw.items():
                    if isinstance(key, str) and key and isinstance(val, dict):
                        records[key] = val
    except Exception:
        records, version = {}, 1
    with RESULTS_LOCK:
        RESULTS = {"version": version, "records": records}
        RESULTS_GENERATION += 1
        RESULTS_DIRTY = 0
        # 磁盘内容就是刚读到的这份：对齐 generation，避免写回旧快照
        _sync_flush_state_locked()
        RESULTS_FLUSHED_GEN = RESULTS_GENERATION
    return RESULTS


def flush_results(force=False):
    """把 RESULTS 落盘。默认节流（≥1s 或每 5 条），force=True 立即写。

    P0：快照读取 + generation 读取在 RESULTS_LOCK 内完成；真正的写盘在独立的
    FLUSH_LOCK 内串行执行，临时文件名唯一（uuid）。只有**写盘成功**才清 DIRTY，
    且仅当期间没有更新的变更（generation 未前进）；失败保留脏标记并记日志，下次重试。
    """
    global RESULTS_DIRTY, RESULTS_LAST_FLUSH, RESULTS_FLUSHED_GEN
    with RESULTS_LOCK:
        _sync_flush_state_locked()
        if not RESULTS_FILE:
            return
        if not force:
            if RESULTS_DIRTY == 0:
                return
            if RESULTS_DIRTY < RESULT_FLUSH_EVERY and (time.time() - RESULTS_LAST_FLUSH) < RESULT_FLUSH_INTERVAL:
                return
        # 快照与 generation 必须同一把锁内取，二者是一对一致的状态
        payload = json.dumps(RESULTS, ensure_ascii=False)
        gen = RESULTS_GENERATION

    folder = os.path.dirname(RESULTS_FILE)
    tmp = None
    try:
        with FLUSH_LOCK:
            with RESULTS_LOCK:
                _sync_flush_state_locked()
                # 更新的快照已经落盘（或由 load_results 对齐过）→ 丢弃这份陈旧 payload
                if gen < RESULTS_FLUSHED_GEN:
                    return
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = "%s.tmp.%s" % (RESULTS_FILE, uuid.uuid4().hex)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, RESULTS_FILE)
            tmp = None
            with RESULTS_LOCK:
                RESULTS_FLUSHED_GEN = gen
                # 期间又攒了新变更 → DIRTY 必须保留，等下一次 flush 写新快照
                if RESULTS_GENERATION == gen:
                    RESULTS_DIRTY = 0
                RESULTS_LAST_FLUSH = time.time()
    except Exception as e:
        # 失败绝不静默：保留 DIRTY 供下次重试，日志留痕，临时文件清理
        _log_flush_error("flush_results failed (gen=%d): %s: %s" %
                         (gen, type(e).__name__, e))
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def record_result(path, record):
    """记录单个文件的压缩结果，并按节流规则尝试落盘。"""
    global RESULTS_DIRTY, RESULTS_GENERATION
    if not path or not isinstance(record, dict):
        return
    with RESULTS_LOCK:
        RESULTS.setdefault("records", {})[path] = record
        RESULTS_DIRTY += 1
        RESULTS_GENERATION += 1
    flush_results()


def clear_results():
    """清空全部压缩记录（「全部重压」用）。"""
    global RESULTS_DIRTY, RESULTS_GENERATION
    with RESULTS_LOCK:
        RESULTS["records"] = {}
        RESULTS_DIRTY += 1
        RESULTS_GENERATION += 1
    flush_results(force=True)


def get_results():
    with RESULTS_LOCK:
        return dict(RESULTS.get("records", {}))


def has_record(path):
    """这个文件是否被我们压过（有记录即可，不要求参数一致）。
    用于区分「我们自己上一次的产物」和「输出目录里别人放的同名文件」：
    前者在参数变化时应当重压，后者按 skip 策略跳过。"""
    if not path:
        return False
    with RESULTS_LOCK:
        return isinstance(RESULTS.get("records", {}).get(path), dict)


def _mtime_ms(path):
    """文件修改时间（毫秒整数，None 表示取不到）。用于识别"被换掉的产物"。"""
    try:
        return int(round(os.path.getmtime(path) * 1000))
    except OSError:
        return None
    except Exception:
        return None


def result_is_done(path, sig, dst_dir=None):
    """已完成判定：记录存在 + 参数指纹一致 + 输出文件仍在、大小相符、修改时间相符。

    dst_dir 非空时额外要求记录里的 out 就在当前目标目录下（防止「上次输出到 A 目录、
    这次目标改成 B 目录」时被误判为已完成）。
    out_mtime 是 v1.7.1 补的：光比大小识别不出"被换成同样大小的别的文件"（QA C03c）。
    """
    if not path or not sig:
        return None
    with RESULTS_LOCK:
        rec = RESULTS.get("records", {}).get(path)
    if not isinstance(rec, dict):
        return None
    if rec.get("sig") != sig:
        return None
    if rec.get("status") not in ("ok", "skipped", "copied"):
        return None
    out = rec.get("out")
    if not out or not os.path.isfile(out):
        return None
    if dst_dir:
        try:
            if os.path.dirname(os.path.abspath(out)) != os.path.abspath(dst_dir):
                return None
        except Exception:
            return None
    try:
        size = os.path.getsize(out)
    except OSError:
        return None
    rec_size = rec.get("out_size")
    if rec_size is not None:
        try:
            if size != int(rec_size):
                return None
        except Exception:
            return None
    rec_mtime = rec.get("out_mtime")
    if rec_mtime is not None:
        # 老记录没有 out_mtime（v1.7.0 写的），跳过这项校验以保持向后兼容
        try:
            if _mtime_ms(out) != int(rec_mtime):
                return None
        except Exception:
            return None
    return dict(rec)


def count_done(paths, sig):
    """批量查询已完成记录，返回 {path: record}。"""
    done = {}
    for p in paths or []:
        rec = result_is_done(p, sig)
        if rec:
            done[p] = rec
    return done


# ---------------------------------------------------------------- 任务
def new_task(files, output, options, keep_structure, force_recompress=False):
    tid = uuid.uuid4().hex[:12]
    t = {"id": tid, "state": "pending", "files": files, "output": output,
         "options": options, "keep_structure": keep_structure,
         "total": len(files), "done": 0, "ok": 0, "copied": 0, "skipped": 0, "fail": 0,
         "sig": None, "error": None, "force_recompress": bool(force_recompress),
         "current": None, "started": None, "finished": None, "cancel": False, "results": []}
    with TASKS_LOCK:
        TASKS[tid] = t
    return t


def task_snapshot(t):
    """任务的对外快照（/api/tasks/<id> 与 /api/tasks/active 共用同一形状）。"""
    elapsed = None
    if t.get("started"):
        elapsed = int((t.get("finished") or time.time()) - t["started"])
    return {"id": t["id"], "state": t["state"], "total": t["total"],
            "done": t["done"], "ok": t["ok"], "copied": t.get("copied", 0),
            "skipped": t.get("skipped", 0),
            "fail": t["fail"], "current": t["current"], "output": t["output"],
            "workers": t.get("workers", 1), "keep_structure": t["keep_structure"], "error": t.get("error"),
            "elapsed": elapsed, "results": t["results"]}


def resolve_dst_dir(item, task, output):
    """算出单个文件的目标目录：显式 out_dir > 保留目录结构 > 平铺到输出目录。"""
    src = item.get("path", "")
    out_dir = item.get("out_dir")
    if out_dir:
        return out_dir
    if task.get("keep_structure"):
        root = item.get("root", "")
        if root:
            try:
                rel = os.path.relpath(src, root)
            except Exception:
                rel = os.path.basename(src)
        else:
            rel = os.path.basename(src)
        if rel.startswith(".."):
            rel = os.path.basename(src)
        return os.path.dirname(os.path.join(output, rel))
    return output


def resolve_out_format(options, src_ext):
    """返回 (fmt, fmt2, out_ext)。
    fmt = 用户选择的导出格式（keep/jpeg/png/webp/tiff），
    fmt2 = 实际编码格式，out_ext = 落盘扩展名。"""
    opts = options or {}
    fmt = str(opts.get("format", "keep")).lower()
    if fmt not in ("keep", "jpeg", "png", "webp", "tiff"):
        fmt = "keep"
    src_ext = src_ext or ""
    if fmt == "keep":
        fmt2 = src_ext.lstrip(".").lower()  # 按源文件扩展名决定
        if fmt2 not in ("jpeg", "png", "webp", "tiff", "gif"):
            fmt2 = "jpeg"  # bmp/avif 等未覆盖格式按 JPEG 处理
    else:
        fmt2 = fmt
    out_ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "tiff": ".tiff"}.get(fmt2, src_ext)
    return fmt, fmt2, out_ext


EXIST_POLICIES = ("skip", "overwrite", "rename")


def normalize_exist_policy(options):
    """已存在文件处理策略：skip（默认）/ overwrite / rename。"""
    opts = options or {}
    policy = str(opts.get("exist_policy") or "").strip().lower()
    if policy not in EXIST_POLICIES:
        # 兼容旧客户端：只有 overwrite 布尔量
        policy = "overwrite" if opts.get("overwrite") else "skip"
    return policy


def build_clt_cmd(src, dst_dir, options, out_format):
    """按最终输出格式组装 caesiumclt 命令（每个格式独立质量设置）。返回 (cmd, out_ext)"""
    options = options or {}
    cmd = [CLT]
    mode = str(options.get("mode", "quality"))
    lossless = bool(options.get("lossless")) or mode == "lossless"
    q_jpeg = max(1, min(100, int(options.get("jpeg_quality", 80) or 80)))
    q_webp = max(1, min(100, int(options.get("webp_quality", 80) or 80)))
    q_png = max(1, min(100, int(options.get("png_quality", 90) or 90)))
    opt_png = max(0, min(6, int(options.get("png_opt_level", 3) or 3)))

    # 目标格式
    fmt, fmt2, out_ext = resolve_out_format(options, out_format)

    if mode == "target":
        try:
            kb = max(1, int(options.get("max_size_kb", 200) or 200))
        except Exception:
            kb = 200
        cmd += ["--max-size", str(kb * 1024)]
    elif lossless:
        cmd.append("--lossless")
    else:
        # 按最终格式选质量
        if fmt2 == "jpeg":
            cmd += ["-q", str(q_jpeg)]
        elif fmt2 == "png":
            cmd += ["-q", str(q_png)]
        elif fmt2 == "webp":
            cmd += ["-q", str(q_webp)]
        # tiff/gif 等无损格式：不传 -q

    if fmt != "keep":
        cmd += ["--format", fmt]
    # JPEG 高级
    if fmt2 == "jpeg" and not lossless and mode != "target":
        chroma = str(options.get("chroma", "auto")).lower()
        if chroma not in ("4:4:4", "4:2:2", "4:2:0", "4:1:1", "auto"):
            chroma = "auto"
        cmd += ["--jpeg-chroma-subsampling", chroma]
        if not options.get("progressive", True):
            cmd.append("--jpeg-baseline")
    # PNG 高级
    if fmt2 == "png":
        cmd += ["--png-opt-level", str(opt_png)]
        if options.get("zopfli"):
            cmd.append("--zopfli")
    # TIFF 压缩算法
    if fmt2 == "tiff":
        alg = str(options.get("tiff_algorithm", "deflate")).lower()
        if alg not in ("uncompressed", "lzw", "deflate", "packbits"):
            alg = "deflate"
        cmd += ["--tiff-algorithm", alg]

    if options.get("keep_exif"):
        cmd.append("-e")
    if options.get("keep_dates"):
        cmd.append("--keep-dates")
    suffix = str(options.get("suffix", "") or "")
    if suffix:
        cmd += ["--suffix", suffix]
    # skip/rename 都要求引擎绝不覆盖（skip 已在 spawn 前拦掉了已存在文件，这里是二次保险）；
    # only overwrite 才用 -O all。
    overwrite_flag = "all" if normalize_exist_policy(options) == "overwrite" else "never"
    cmd += ["-o", dst_dir, "-O", overwrite_flag, src]
    return cmd, out_ext


def _claim_target(dst_dir, base, suffix, out_ext, attempts=500):
    """原子占位一个不冲突的输出文件名：name.ext → name (2).ext → name (3).ext …
    用 O_CREAT|O_EXCL 占位，多任务并发也不会互相覆盖。占位失败返回 None。"""
    n = 1
    while n <= attempts:
        name = (base + suffix + out_ext) if n == 1 else ("%s%s (%d)%s" % (base, suffix, n, out_ext))
        path = os.path.join(dst_dir, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            n += 1
            continue
        except OSError:
            return None
        try:
            os.close(fd)
        except OSError:
            pass
        return path
    return None


def _pick_output(dst_dir, expect_path, before):
    """定位引擎真正写出的文件：先看期望路径，再退回"目录新增项"（兼容格式/后缀变化）。"""
    if expect_path and os.path.isfile(expect_path):
        return expect_path
    try:
        new = sorted(set(os.listdir(dst_dir)) - before)
    except Exception:
        return None
    for name in new:
        p = os.path.join(dst_dir, name)
        if os.path.isfile(p):
            return p
    return None


# ------------------------------------------------ P1-2/P1-3 引擎进程管控
# P1-3：跨任务的全局进程预算。每跑一次 caesiumclt 前 acquire、结束 release，
# 双开/多任务叠起来也不会突破 min(CPU,16) 个在途引擎，避免小内存 NAS 被 OOM。
def _default_process_budget():
    try:
        return max(1, min(os.cpu_count() or 4, 16))
    except Exception:
        return 4


PROCESS_BUDGET_SIZE = _default_process_budget()
PROCESS_BUDGET = threading.BoundedSemaphore(PROCESS_BUDGET_SIZE)

# P1-2：在途进程登记表。按 task_id 分组，/api/cancel 与 worker 只终止本任务的进程，
# 绝不波及其它并发任务。threading.local 标记"当前线程正在为哪个任务工作"。
_TASK_PROCS = {}
_TASK_PROCS_LOCK = threading.Lock()
_TASK_CTX = threading.local()
_ORIGINAL_SUBPROCESS_RUN = subprocess.run   # 用于识别 monkeypatch（测试/FakeRun）


def _bind_task_context(task):
    _TASK_CTX.task_id = task.get("id") if isinstance(task, dict) else None


def _clear_task_context():
    _TASK_CTX.task_id = None


def _register_task_proc(task_id, proc):
    if not task_id or proc is None:
        return
    with _TASK_PROCS_LOCK:
        _TASK_PROCS.setdefault(task_id, set()).add(proc)


def _unregister_task_proc(task_id, proc):
    if not task_id or proc is None:
        return
    with _TASK_PROCS_LOCK:
        procs = _TASK_PROCS.get(task_id)
        if procs is not None:
            procs.discard(proc)
            if not procs:
                _TASK_PROCS.pop(task_id, None)


def _drop_task_procs(task_id):
    with _TASK_PROCS_LOCK:
        _TASK_PROCS.pop(task_id, None)


def cancel_task_processes(task_id):
    """终止指定任务的所有在途 caesiumclt，返回被 terminate 的个数。

    先 terminate（优雅），2 秒后仍不退出的兜底 kill；wait 回收由 _run_clt 的
    finally 负责，这里不阻塞 /api/cancel 的处理线程。
    """
    if not task_id:
        return 0
    with _TASK_PROCS_LOCK:
        procs = list(_TASK_PROCS.get(task_id, ()))
    hit = 0
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
                hit += 1
        except Exception:
            pass
    if hit:
        def _force_kill_holdouts():
            time.sleep(2.0)
            for proc in procs:
                try:
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
        threading.Thread(target=_force_kill_holdouts, daemon=True,
                         name="caesium-cancel-kill").start()
    return hit


def _run_clt(cmd):
    """跑一次引擎，返回 (proc, err_text)。proc 为 None 表示启动失败。

    生产路径用 Popen + 全局信号量：进程句柄登记到当前任务，取消时可定点终止；
    communicate 结束/异常后必定 wait 回收，不留僵尸进程。
    兼容路径：若 subprocess.run 被注入（单测的 FakeRun 等），沿用原调用形状，
    保持既有测试契约不变。
    """
    task_id = getattr(_TASK_CTX, "task_id", None)
    if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
        with PROCESS_BUDGET:
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=3600), ""
            except Exception as e:
                return None, "caesiumclt error: %s" % e

    with PROCESS_BUDGET:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        except Exception as e:
            return None, "caesiumclt error: %s" % e
        _register_task_proc(task_id, proc)
        out, err, rc = "", "", -1
        try:
            try:
                out, err = proc.communicate(timeout=3600)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    out, err = proc.communicate()
                except Exception:
                    out, err = "", ""
            except Exception:
                out, err = "", ""
            rc = proc.returncode
        finally:
            _unregister_task_proc(task_id, proc)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait()      # 回收子进程，杜绝僵尸
            except Exception:
                pass
    return subprocess.CompletedProcess(cmd, rc, out, err), ""


def _clt_error_text(proc, fallback="no output file"):
    if proc is None:
        return fallback
    err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return err[:500] if err else fallback


# ---------------------------------------------------------------- 原子落盘
# 三种策略（skip 命中除外）统一走同一条流水线：
#   压到 <dst_dir>/.caesium-tmp-<uuid>/  →  校验 returncode  →  os.replace 到最终路径
#   → finally 删临时目录
# 好处：最终路径上永远不会出现"半个文件"。用户闪退 / 引擎被 OOM 杀掉时，
# 残骸只可能落在 .caesium-tmp-* 里（1 天后自动清理），绝不会被下次的 skip
# 判定当成"已完成"而永久跳过；"输出到原文件夹（覆盖原图）"失败时原图也完好。
TMP_PREFIX = ".caesium-tmp-"
TMP_MAX_AGE = 24 * 60 * 60          # 临时目录保留时长（秒），超过即视为闪退残骸
_PATH_LOCKS = {}                    # 最终输出路径 → 互斥锁（P1-4：并发写同一文件）
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path):
    """取某个最终输出路径的进程内互斥锁。

    前端 setLocked() 只能挡住同一个页面重复点击；换窗口 / 双开 / 刷新后再提交
    会让两个任务同时写同一个输出文件，正文会交错成一个"既不完整也不正确"的文件。
    """
    try:
        key = os.path.normcase(os.path.abspath(path))
    except Exception:
        key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _sweep_tmp_dirs(dst_dir, max_age=TMP_MAX_AGE):
    """清掉输出目录里超过 max_age 的 .caesium-tmp-* 残留（上次闪退留下的现场）。返回清理个数。"""
    try:
        names = os.listdir(dst_dir)
    except OSError:
        return 0
    removed = 0
    now = time.time()
    for name in names:
        if not name.startswith(TMP_PREFIX):
            continue
        path = os.path.join(dst_dir, name)
        try:
            if not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) < max_age:
                continue          # 可能还有别的任务正在写，别动
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _new_tmp_dir(dst_dir):
    """在输出目录下建临时工作目录。返回 (tmp_dir, err)；失败时 tmp_dir 为 None。

    必须建在**同一个目录**下：os.replace 只有同文件系统内才是原子操作。
    """
    tmp_dir = os.path.join(dst_dir, TMP_PREFIX + uuid.uuid4().hex)
    try:
        os.makedirs(tmp_dir, exist_ok=True)
    except Exception as e:
        return None, "create temp dir failed: %s" % e
    return tmp_dir, ""


def _encode_to_tmp(src, tmp_dir, options, out_format, base, suffix, out_ext):
    """在临时目录里跑一次引擎。返回 (tmp_out, err)；tmp_out 为 None 表示这次压缩失败。

    判"成功"三个条件缺一不可：
      1. 引擎真的启动起来了；
      2. 退出码 == 0（非零 = 编码中途失败，产物不可信，见 QA C11a/C11d）；
      3. 临时目录里确实出现了一个非空成品。
    只看"找没找到文件"是不够的：引擎崩了也会留下半截文件。
    """
    cmd, _ = build_clt_cmd(src, tmp_dir, options, out_format)
    proc, launch_err = _run_clt(cmd)
    if proc is None:
        return None, launch_err or "caesiumclt launch failed"
    if proc.returncode != 0:
        return None, _clt_error_text(proc, "caesiumclt exited with code %s" % proc.returncode)
    expected = os.path.join(tmp_dir, base + suffix + out_ext)
    tmp_out = None
    if os.path.isfile(expected) and os.path.getsize(expected) > 0:
        tmp_out = expected
    else:
        tmp_out = _pick_output(tmp_dir, None, set())
    if not tmp_out:
        return None, _clt_error_text(proc)
    try:
        if os.path.getsize(tmp_out) <= 0:
            return None, "caesiumclt produced an empty file"
    except OSError as e:
        return None, "stat output failed: %s" % e
    return tmp_out, ""


def compress_file(src, dst_dir, options, out_format):
    """压缩单个文件。

    返回 (ok, out_path, err, status)，status ∈ ok | skipped | copied | error。
    - **GIF 直通复制（v1.7.4）**：`format=keep` 且源是 .gif 时不调引擎，直接复制一份
      （见 _copy_through）。引擎只有全局 -q、没有 GIF 专属质量参数，且它自带
      "没变小就不采用"的逻辑 —— 压不动却记成 ok + 节省 0%，既慢又误导。
      用户若显式选「导出为 WebP/PNG」，.gif 源仍交给引擎转码（此时 _fmt2 不是 gif）。
    - skip 策略：先算目标路径，已存在且非空、**且不是疑似残骸** → 直接返回，
      **不 spawn 引擎**（零开销）；
    - overwrite 策略：压到临时目录后 os.replace 覆盖最终路径（覆盖也是原子的）；
    - rename 策略：压到 .caesium-tmp-<uuid>/ 再 os.replace 成 "name (2).ext"，绝不覆盖。
    三种策略的"真压"路径都经过临时目录 + os.replace，最终路径永不出半个文件。

    skip 的「残骸例外」（v1.7.1 第 3 轮）：v1.7.0 的引擎直写最终路径，闪退必然留下
    截断的图；这些旧残骸非空又没有归属记录，如果按 skip 永久跳过，用户就拿着坏图
    还看到「已跳过」。所以判定为 image_looks_truncated() 时**不走 skip**，照常重压；
    重压走 _encode_to_tmp + os.replace，目标已存在也会被原子覆盖，不需要 -O all。
    非图片 / 无魔数 / 无法判断 → 仍然 skip（C01a：用户自己的文件必须被保护）。
    """
    options = options or {}
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except Exception as e:
        return False, None, "create output dir failed: %s" % e, "error"

    _fmt, _fmt2, out_ext = resolve_out_format(options, out_format)
    base = os.path.splitext(os.path.basename(src))[0]
    suffix = str(options.get("suffix", "") or "")
    policy = normalize_exist_policy(options)
    out_path = os.path.join(dst_dir, base + suffix + out_ext)

    # 输出到原文件夹：目标就是源文件本身，永远"已存在"，只能覆盖
    try:
        if os.path.abspath(out_path) == os.path.abspath(src):
            policy = "overwrite"
    except Exception:
        pass

    _sweep_tmp_dirs(dst_dir)   # 顺手清掉上次闪退留下的临时目录残留

    # 「存在性检查 + 压缩 + replace」必须在同一把锁内完成，否则两个任务会同时
    # 判定"目标不存在"并各自写入，最后得到一个内容交错的文件。
    with _path_lock(out_path):
        # GIF 直通：不 spawn 引擎，直接复制（同样要过 skip/overwrite/rename 三态）
        if _fmt2 == "gif":
            return _copy_through(src, dst_dir, out_path, policy, base, suffix, out_ext)
        if policy == "skip":
            try:
                if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                    # 非空、已存在 —— 但还要过一道「残骸例外」：v1.7.0 引擎直写最终
                    # 路径时闪退，会在这里留下一个截断的图。它不是"用户已有的成品"，
                    # 而是我们自己没写完的垃圾，必须重压；否则坏图会被永久跳过。
                    if not image_looks_truncated(out_path):
                        return True, out_path, "", "skipped"
            except OSError:
                pass
        if policy == "rename":
            return _compress_rename(src, dst_dir, options, out_format, base, suffix, out_ext)
        return _compress_atomic(src, dst_dir, out_path, options, out_format, base, suffix, out_ext)


def _copy_through(src, dst_dir, out_path, policy, base, suffix, out_ext):
    """GIF 直通：**不调引擎**，直接把源文件复制一份到输出目录（v1.7.4）。

    为什么要这么做：引擎只有全局 `-q`（0-100），没有 GIF 专属质量参数，而且它自己
    就有「Image already optimized / Interlacing ... would result in a larger file」
    这类"没变小就不采用"的逻辑 —— GIF 压不动，结果却记成 `ok` + 节省 0%，既慢又误导。
    用户明确要求：GIF 不压缩，直接复制到输出目录一份。

    复制也必须遵守既有的三态与原子落盘语义（不许直接 shutil.copy2 到最终路径，
    否则中途失败会在最终路径留下半截文件）：
      * 目标 == 源（输出到原文件夹） → 不自我复制，源文件即成品，返回 copied；
      * skip      → 目标存在、非空、且不是残骸 → skipped（不复制，语义与真压一致）；
      * rename    → 复制到 .caesium-tmp-*/ 再 os.replace 成 "name (2).gif"，绝不覆盖；
      * overwrite → 复制到 .caesium-tmp-*/ 再 os.replace 覆盖最终路径（原子）。
    copy2 是流式复制，不会把整个文件读进内存，并保留 mtime。
    """
    try:
        if os.path.abspath(out_path) == os.path.abspath(src):
            return True, src, "", "copied"      # 输出到原文件夹：源即成品，别自我复制
    except Exception:
        pass

    if policy == "skip":
        try:
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                if not image_looks_truncated(out_path):
                    return True, out_path, "", "skipped"
        except OSError:
            pass

    tmp_dir, err = _new_tmp_dir(dst_dir)
    if tmp_dir is None:
        return False, None, err, "error"
    try:
        tmp_out = os.path.join(tmp_dir, base + suffix + out_ext)
        try:
            shutil.copy2(src, tmp_out)
        except Exception as e:
            return False, None, "copy failed: %s" % e, "error"
        target = out_path
        if policy == "rename":
            target = _claim_target(dst_dir, base, suffix, out_ext)
            if not target:
                return False, None, "cannot find free output name", "error"
        try:
            os.replace(tmp_out, target)
        except Exception as e:
            if policy == "rename":
                try:
                    os.unlink(target)   # 占位用的空文件，失败要清掉
                except OSError:
                    pass
            return False, None, "move output failed: %s" % e, "error"
        return True, target, "", "copied"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _compress_atomic(src, dst_dir, out_path, options, out_format, base, suffix, out_ext):
    """压到临时目录 → os.replace 到最终路径。overwrite / same_folder 共用。"""
    tmp_dir, err = _new_tmp_dir(dst_dir)
    if tmp_dir is None:
        return False, None, err, "error"
    try:
        tmp_out, err = _encode_to_tmp(src, tmp_dir, options, out_format, base, suffix, out_ext)
        if not tmp_out:
            return False, None, err, "error"
        try:
            os.replace(tmp_out, out_path)   # 同文件系统内的原子替换
        except Exception as e:
            return False, None, "move output failed: %s" % e, "error"
        return True, out_path, "", "ok"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _compress_rename(src, dst_dir, options, out_format, base, suffix, out_ext):
    """保留两份：压到输出目录下的临时目录（绝不覆盖），成功后再改名为 name (2).ext。"""
    tmp_dir, err = _new_tmp_dir(dst_dir)
    if tmp_dir is None:
        return False, None, err, "error"
    try:
        tmp_out, err = _encode_to_tmp(src, tmp_dir, options, out_format, base, suffix, out_ext)
        if not tmp_out:
            return False, None, err, "error"
        target = _claim_target(dst_dir, base, suffix, out_ext)
        if not target:
            return False, None, "cannot find free output name", "error"
        try:
            os.replace(tmp_out, target)
        except Exception as e:
            try:
                os.unlink(target)  # 占位用的空文件，失败要清掉
            except OSError:
                pass
            return False, None, "move output failed: %s" % e, "error"
        return True, target, "", "ok"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def resolve_workers(options, nfiles):
    """并行度。options.workers > 环境变量 CAESIUM_WORKERS > 引擎默认（0=自动）。

    飞牛版走 caesiumclt（每个文件一个进程）：自动值按 CPU 逻辑核数给、封顶 16。
    也可由 options.workers（前端下拉）或环境变量 CAESIUM_WORKERS 精确指定；
    0/留空 = 自动。上限 32，且不超过文件数（空任务返回 1）。
    """
    def _parse(value):
        try:
            return int(str(value).strip())
        except Exception:
            return None

    opts = options or {}
    opt_n = _parse(opts.get("workers"))
    # P1-1：前端"自动"档发的是整数 0，和 None/空串/非法值一样应回退到环境变量。
    # 显式正数（1/2/4/8/16）优先于环境变量，优先级：显式正数 > 环境变量 > CPU 自动。
    if opt_n is None or opt_n <= 0:
        env_n = _parse(os.environ.get("CAESIUM_WORKERS", "0"))
        n = env_n if (env_n is not None and env_n > 0) else 0
    else:
        n = opt_n
    cpu = os.cpu_count() or 4
    if n <= 0:
        # 飞牛版走 caesiumclt：每个文件一个进程，自动值按逻辑核数给但封顶 16，
        # 避免小内存 NAS 上一次起几十个进程被 OOM。用户可用下拉/环境变量精确控制。
        n = min(cpu, 16)
    n = max(1, min(int(n), 32))
    return max(1, min(n, max(1, int(nfiles or 0))))




def _process_item(t, item, output, options, sig, force_all):
    """处理单个文件，返回 (entry, kind, record)。

    kind ∈ ok | skipped | copied | error；record 为要写进 results.json 的归属记录（可为 None）。
    **不修改 t 的计数**——并发时由 run_task 在锁内统一累加，避免丢计数。
    """
    src = item.get("path", "")
    if not src or not os.path.isfile(src):
        return ({"path": src, "status": "error", "error": "file not found"}, "error", None)

    t["current"] = src
    dst_dir = resolve_dst_dir(item, t, output)
    out_format = os.path.splitext(src)[1].lower()
    # 断点续传：当前参数下这个文件已经压好了 → 直接跳过，连引擎都不启动
    done_rec = None if force_all else result_is_done(src, sig, dst_dir)
    if done_rec:
        entry = {"path": src, "out": done_rec.get("out"), "status": "skipped",
                 "in_size": done_rec.get("in_size"),
                 "out_size": done_rec.get("out_size")}
        # rename（保留两份）模式下的"已存在 → 跳过"最容易让人以为坏了：
        # 续传正确性优先，逻辑不动，只把话说清楚（再存一份要点「全部重压」）。
        if normalize_exist_policy(options) == "rename":
            entry["note"] = "再存一份请点「全部重压」"
        return (entry, "skipped", None)

    # "全部重压"，或"这个文件是我们上次压的、但参数已经改了" → 必须真重压：
    # 否则 skip 策略会看到旧文件直接跳过，用户改了质量却什么都没发生。
    run_options = options
    if (force_all or has_record(src)) and normalize_exist_policy(options) != "rename":
        run_options = dict(options)
        run_options["exist_policy"] = "overwrite"
        run_options["overwrite"] = True
    ok, out_path, err, status = compress_file(src, dst_dir, run_options, out_format)
    if not (ok and out_path):
        msg = (err or "compress failed")
        return ({"path": src, "status": "error", "error": msg}, "error",
                {"status": "error", "out": None, "in_size": None, "out_size": None,
                 "error": msg[:300], "ts": int(time.time()), "sig": sig})
    try:
        in_size = os.path.getsize(src)
    except OSError:
        in_size = None
    try:
        out_size = os.path.getsize(out_path)
    except OSError:
        out_size = None
    # 三态之外还有"直通复制"（GIF，v1.7.4）：不算成功也不算跳过，
    # 单独计入 copied，保证 total == ok + copied + skipped + fail 恒成立。
    final_status = status if status in ("skipped", "copied") else "ok"
    entry = {"path": src, "out": out_path, "status": final_status,
             "in_size": in_size, "out_size": out_size}
    # 只有"我们真的产出的文件"才写归属记录。skip 策略放过的是用户自己的
    # （或别的工具压出来的）文件，不能登记成我们的产物 —— 否则用户下一次
    # 改参数时会被 has_record 判成"我们的旧版本"而被覆盖掉。
    # copied（GIF 直通）是我们产出的副本，必须登记，否则断点续传不认识它。
    rec = None
    if final_status in ("ok", "copied"):
        rec = {"status": final_status, "out": out_path,
               "in_size": in_size, "out_size": out_size,
               "out_mtime": _mtime_ms(out_path),
               "ts": int(time.time()), "sig": sig}
    return (entry, final_status, rec)




def run_task(t):
    """执行压缩任务（支持多线程并行，v1.8.1-fnOS）

    v1.7.5 是串行 for 循环；本版把每个文件交给 worker 线程，默认并行度见 resolve_workers。

    并行度来自 options.workers（0=自动）。每个 worker 独立处理一个文件：
    飞牛版 CLI 时是多个 caesiumclt 进程并行（GIL 不阻塞子进程）。
    "同一输出路径不会写出半个文件"由 compress_file 里的 _path_lock + os.replace 保证，
    与并行度无关。

    任何未捕获异常都要落到 state="error"（而不是把任务永远卡在 running 让前端转圈），
    finally 里强制把结果落盘，保证闪退最多丢最后 1 秒的进度。
    """
    t["state"] = "running"
    t["started"] = time.time()
    output = t["output"]
    options = t["options"] or {}
    sig = results_signature(options)
    t["sig"] = sig
    force_all = bool(t.get("force_recompress"))
    items = list(t["files"])
    workers = resolve_workers(options, len(items))
    t["workers"] = workers
    lock = threading.Lock()
    cursor = {"i": 0}
    fatal = {"exc": None}      # 代码级异常（不是"这个文件压失败"）→ 整个任务判 error

    def worker():
        # P1-2：把任务上下文绑到本 worker 线程，_run_clt 据此登记在途进程句柄
        _bind_task_context(t)
        try:
            while True:
                if t["cancel"] or fatal["exc"] is not None:
                    if t["cancel"]:
                        cancel_task_processes(t["id"])   # 连在途引擎一起停，不必等 3600s 超时
                    return
                with lock:
                    if cursor["i"] >= len(items):
                        return
                    item = items[cursor["i"]]
                    cursor["i"] += 1
                try:
                    entry, kind, rec = _process_item(t, item, output, options, sig, force_all)
                except Exception as e:
                    # compress_file 正常失败会返回 (False, ...)，不会抛。
                    # 真抛出来说明是代码 bug，按原有契约让整个任务落到 error（别闷着）。
                    with lock:
                        if fatal["exc"] is None:
                            fatal["exc"] = e
                    return
                with lock:
                    t["done"] += 1
                    if kind == "skipped":
                        t["skipped"] += 1
                    elif kind == "copied":
                        t["copied"] = t.get("copied", 0) + 1
                    elif kind == "ok":
                        t["ok"] += 1
                    else:
                        t["fail"] += 1
                    t["results"].append(entry)
                if rec:
                    # record_result 自带 RESULTS_LOCK；放锁外写，避免长时间占着计数锁
                    try:
                        record_result(item.get("path", ""), rec)
                    except Exception:
                        pass
        finally:
            _clear_task_context()

    try:
        if output:
            os.makedirs(output, exist_ok=True)
        if workers <= 1 or len(items) <= 1:
            worker()                                     # 单线程：保持原路径，零线程开销
        else:
            threads = [threading.Thread(target=worker, daemon=True, name="caesium-worker-%d" % i)
                       for i in range(workers)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        if fatal["exc"] is not None:
            raise fatal["exc"]
        if t["cancel"]:
            t["state"] = "canceled"
    except Exception as e:
        t["state"] = "error"
        t["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if t["state"] == "running":
            t["state"] = "done"
        t["finished"] = time.time()
        t["current"] = None
        _drop_task_procs(t["id"])       # P1-2：worker 已全部退出，清理进程登记表
        flush_results(force=True)


# ---------------------------------------------------------------- 文件夹管理
FOLDERS_FILE = None
QUEUE_FILE = None
EXCLUDED_FILE = None
# 队列持久化上限：与扫描上限（50000）同量级即可，别让 queue.json / 单次请求体失控。
# 前端分批 POST（每批 ≤500 条）时，服务端按批累加，所以上限只影响"最多能记住多少"。
QUEUE_MAX_FILES = 50000             # 与 SCAN_MAX_FILES 对齐：扫得到就该存得下，
                                    # 否则 2 万条以后的列表刷新就丢（v1.7.5 修正）
QUEUE_CACHE = {"files": None}       # 内存缓存：分批保存时不必每批重新解析磁盘上的 JSON


def load_queue():
    """读队列（优先内存缓存）。文件损坏/不存在时返回空列表，不抛异常。"""
    if QUEUE_CACHE["files"] is not None:
        return list(QUEUE_CACHE["files"])
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    QUEUE_CACHE["files"] = data
    return list(data)


def save_queue(files):
    """整份覆盖写队列。先写 .tmp 再 os.replace —— 否则进程被杀时 queue.json
    会变成半截 JSON，下次启动整个队列都读不出来（跟落盘那条一个道理）。"""
    files = [x for x in (files or []) if isinstance(x, dict)]
    try:
        folder = os.path.dirname(QUEUE_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(files, f, ensure_ascii=False, indent=1)
        os.replace(tmp, QUEUE_FILE)
    except Exception:
        return False                # 写失败不要更新缓存，否则下次读到的"已保存"是假的
    QUEUE_CACHE["files"] = files    # 写成功才更新缓存
    return True


def load_excluded():
    try:
        with open(EXCLUDED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_excluded(items):
    try:
        os.makedirs(os.path.dirname(EXCLUDED_FILE), exist_ok=True)
        with open(EXCLUDED_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _split_paths(raw):
    """拆分冒号分隔的路径列表；兼容 Windows 盘符（D:\\...），fnOS 正斜杠路径不受影响"""
    if not raw:
        return []
    parts = raw.split(":")
    out, buf = [], ""
    for part in parts:
        if buf:
            buf += ":" + part
        else:
            buf = part
        if len(part) == 1 and part.isalpha():
            continue  # 盘符（D:），不是分隔符，继续拼接
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def authorized_paths_from_env():
    """官方授权目录列表：fnOS 通过 TRIM_DATA_ACCESSIBLE_PATHS 环境变量提供（冒号分隔）。
    测试可用 CAESIUM_ACCESSIBLE_PATHS 模拟。"""
    raw = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS") or os.environ.get("CAESIUM_ACCESSIBLE_PATHS") or ""
    out = []
    for p in _split_paths(raw):
        p = p.strip()
        if p and os.path.isdir(p) and p not in out:
            out.append(p)
    return out


def load_folders():
    try:
        with open(FOLDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_folders(folders):
    try:
        with open(FOLDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(folders, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def list_volumes():
    """列出存储卷（/vol1、/vol2...）。CAESIUM_FS_ROOT 供测试覆盖系统根。"""
    root = os.environ.get("CAESIUM_FS_ROOT", "/")
    vols = []
    try:
        for n in sorted(os.listdir(root)):
            if re.match(r"^vol\d*$", n):
                p = os.path.join(root, n)
                if os.path.isdir(p):
                    vols.append(p)
    except Exception:
        pass
    return vols


CANDIDATE_SUBDIRS = ("1000", "1001", "@team", "@appshare", "photos", "@photos",
                     "@home", "homes", "users", "home")


def detect_authorized_folders():
    """兜底探测（仅当官方授权列表不可用时）：聚焦 /vol*/1000、/vol*/1000-xxx 用户主目录
    和 @team/@appshare 共享目录【里面】的文件夹，不把容器目录本身加入。"""
    found, seen = [], set()
    for vol in list_volumes():
        try:
            names = os.listdir(vol)
        except Exception:
            continue
        for name in sorted(names):
            base = os.path.join(vol, name)
            if not os.path.isdir(base):
                continue
            is_user_home = name in ("1000", "1001") or re.match(r"^1000-\d", name)
            is_share = name in ("@team", "@appshare", "@photos", "photos", "@home")
            if not (is_user_home or is_share):
                continue
            try:
                for sub in sorted(os.listdir(base)):
                    p = os.path.join(base, sub)
                    try:
                        if os.path.isdir(p) and os.access(p, os.R_OK | os.X_OK) and p not in seen:
                            seen.add(p)
                            found.append(p)
                    except Exception:
                        pass
            except Exception:
                pass
    return found


def remember_folder(path):
    """把可访问的目录持久化到历史（浏览/选择过就记住，防丢失）"""
    if not path or not os.path.isdir(path):
        return
    folders = load_folders()
    if path not in folders:
        folders.append(path)
        save_folders(folders[-200:])


def fs_roots():
    """我的文件夹 = 官方授权目录(env) + 探测兜底(env 缺失时) + 用户历史(手动添加/浏览过)。
    排除用户删除过的目录。探测结果不持久化（env 是权威来源，探测仅是兜底）。"""
    excluded = set(load_excluded())
    env_paths = authorized_paths_from_env()
    detected = detect_authorized_folders() if not env_paths else []
    saved = load_folders()
    merged, seen = [], set()
    for p in env_paths + detected + saved:
        if p in excluded or p in seen or not os.path.isdir(p):
            continue
        seen.add(p)
        merged.append(p)
    return [{"path": p, "kind": "saved"} for p in merged]


def fs_list(path):
    dirs, files = [], []
    try:
        names = os.listdir(path)
    except Exception:
        return {"dirs": [], "files": [], "error": "无法访问（未授权或路径不存在）"}
    remember_folder(path)  # 浏览成功即记住，防关闭窗口后丢失
    for n in sorted(names, key=lambda x: x.lower()):
        p = os.path.join(path, n)
        try:
            if os.path.isdir(p):
                if os.access(p, os.R_OK | os.X_OK):
                    dirs.append({"name": n, "path": p, "access": True})
            else:
                ext = os.path.splitext(n)[1].lower()
                if ext in IMAGE_EXTS:
                    size = os.path.getsize(p)
                    wh = get_image_size(p)
                    files.append({"name": n, "path": p, "size": size, "ext": ext,
                                  "w": wh[0] if wh else None, "h": wh[1] if wh else None})
        except Exception:
            pass
    return {"dirs": dirs, "files": files}


# ---------------------------------------------------------------- 大目录扫描（异步）
# 为什么必须异步：/api/scan 原来是**同步**跑的。扫几万个文件要几十秒到几分钟，
# 飞牛统一网关等不到响应就回 504 的 **HTML** 错误页，前端 r.json() 直接抛
# `Unexpected token '<', "<html> <h"...` —— 用户看到的就是「扫描失败」。
# 更糟的是那个扫描线程不会因为网关超时停下（还在走盘），用户再点一次「添加文件夹」
# 又起一个线程，多个线程并行扫同一棵树 = 用户说的「还在拼命读取像是没停」。
# 现在：POST 立刻返回 scan_id（后台线程扫）→ 结果**分页**拉（几万条不会塞进一个响应）
# → 单飞（新扫描先取消旧的）+ 硬上限 + 可取消 + 进度可观测。
SCANS = {}                          # scan_id -> scan dict
SCAN_LOCK = threading.Lock()        # 保护 SCANS，以及每个 scan 的 files/scanned 等计数
SCAN_MAX_FILES = 50000              # 单次扫描最多收录多少文件，超过即 truncated
SCAN_MAX_DEPTH = 12
SCAN_PAGE_DEFAULT = 500             # /api/scan/status 默认页大小
SCAN_PAGE_MAX = 2000                # 单页上限（别让"分页"又变成一个大响应）
SCAN_KEEP = 8                       # 内存里最多保留几个历史扫描


def _new_scan_state(paths):
    """建一个扫描状态字典（不启动线程）。"""
    return {"id": uuid.uuid4().hex[:12], "state": "running",
            "roots": [p for p in (paths or []) if isinstance(p, str) and p],
            "files": [], "seen": set(), "scanned": 0, "truncated": False,
            "error": None, "cancel": False, "started": time.time(), "finished": None}


def _prune_scans_locked():
    """内存里只留最近 SCAN_KEEP 个扫描（正在跑的不动）。调用方必须持有 SCAN_LOCK。"""
    if len(SCANS) <= SCAN_KEEP:
        return
    for old in sorted(SCANS.values(), key=lambda x: x.get("started") or 0):
        if len(SCANS) <= SCAN_KEEP:
            return
        if old.get("state") == "running":
            continue
        SCANS.pop(old.get("id"), None)


def _scan_stopped(s):
    """该停了：用户取消 / 已达文件上限。每次循环都要检查（取消要能立刻生效）。"""
    return bool(s.get("cancel") or s.get("truncated"))


def _scan_add(s, path, name, root):
    """收录一个图片文件。返回 False 表示"该停了"（取消 / 到上限）。

    尺寸解析走 get_image_size()（≤512KB 分块读），绝不整文件读。
    """
    if s.get("cancel"):
        return False
    if len(s["files"]) >= SCAN_MAX_FILES:
        s["truncated"] = True
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return True                    # 单个文件读不到就跳过，不算失败、也不停
    wh = get_image_size(path)
    ext = os.path.splitext(name)[1].lower()
    s["files"].append({"path": path, "name": name, "size": size, "ext": ext,
                       "root": root, "w": wh[0] if wh else None, "h": wh[1] if wh else None})
    return True


def _scan_walk(s, root, d, depth):
    """递归遍历一个目录（深度上限 SCAN_MAX_DEPTH），每处理一个条目就检查是否该停。"""
    if _scan_stopped(s) or depth > SCAN_MAX_DEPTH:
        return
    try:
        names = os.listdir(d)
    except Exception:
        return
    for n in sorted(names, key=lambda x: x.lower()):
        if _scan_stopped(s):
            return
        p = os.path.join(d, n)
        with SCAN_LOCK:
            # 取消/到上限的检查必须和 scanned 自增在同一把锁里：否则 cancel_scan()
            # 返回之后还可能再自增一次，前端看到的"已读取"会莫名跳一格。
            if _scan_stopped(s):
                return
            s["scanned"] += 1          # scanned = 已访问的目录项数（含目录/非图片），进度用
        try:
            if os.path.isdir(p):
                _scan_walk(s, root, p, depth + 1)
                continue
            if os.path.splitext(n)[1].lower() not in IMAGE_EXTS:
                continue
            with SCAN_LOCK:
                if p in s["seen"]:
                    continue
                s["seen"].add(p)
                keep_going = _scan_add(s, p, n, root)
            if not keep_going:
                return
        except Exception:
            continue


def run_scan(s):
    """扫描线程主体。任何异常都落到 state=error，绝不把任务永远卡在 running。"""
    try:
        for p in list(s.get("roots") or []):
            if _scan_stopped(s):
                break
            if os.path.isdir(p):
                _scan_walk(s, p, p, 0)
            elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in IMAGE_EXTS:
                with SCAN_LOCK:
                    if p in s["seen"]:
                        continue
                    s["seen"].add(p)
                    _scan_add(s, p, os.path.basename(p), os.path.dirname(p))
    except Exception as e:
        with SCAN_LOCK:
            s["state"] = "error"
            s["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        with SCAN_LOCK:
            if s.get("state") == "running":
                s["state"] = "canceled" if s.get("cancel") else "done"
            if not s.get("finished"):
                s["finished"] = time.time()


def new_scan(paths):
    """建扫描任务并启动后台线程，返回 scan_id。**立刻返回**，不在这里扫盘。

    单飞：新扫描会把所有还在 running 的旧扫描置为 canceled（旧线程下一次循环就停，
    不会出现两个线程并行扫同一棵树）。
    """
    with SCAN_LOCK:
        for old in SCANS.values():
            if old.get("state") == "running":
                old["cancel"] = True
                old["state"] = "canceled"
                old["finished"] = old.get("finished") or time.time()
        s = _new_scan_state(paths)
        SCANS[s["id"]] = s
        _prune_scans_locked()
    th = threading.Thread(target=run_scan, args=(s,), daemon=True)
    th.start()
    return s["id"]


def cancel_scan(scan_id):
    """置取消标志并立刻把 state 改成 canceled；返回是否命中。

    线程只会在下一次循环（每个目录项）检查 flag，所以"取消后不再增长"最多差一个条目。
    """
    if not scan_id:
        return False
    with SCAN_LOCK:
        s = SCANS.get(scan_id)
        if not s:
            return False
        if s.get("state") == "running":
            s["cancel"] = True
            s["state"] = "canceled"
            s["finished"] = s.get("finished") or time.time()
        return True


def scan_snapshot(s, offset=0, limit=SCAN_PAGE_DEFAULT):
    """分页快照：只返回 [offset, offset+limit) 这一段。

    total = 已发现的图片总数；scanned = 已访问的目录项数（进度用）。
    几万条的完整列表绝不进单个响应（那样又变成十几 MB 的 JSON 了）。
    """
    offset = max(0, _as_int(offset, 0))
    limit = _as_int(limit, SCAN_PAGE_DEFAULT)
    if limit <= 0:
        limit = SCAN_PAGE_DEFAULT
    limit = min(limit, SCAN_PAGE_MAX)
    with SCAN_LOCK:
        all_files = s.get("files") or []
        total = len(all_files)
        chunk = [dict(x) for x in all_files[offset:offset + limit]]
        started = s.get("started") or time.time()
        return {"id": s.get("id"), "state": s.get("state"), "scanned": s.get("scanned", 0),
                "total": total, "files": chunk, "error": s.get("error"),
                "truncated": bool(s.get("truncated")), "offset": offset,
                "next_offset": offset + len(chunk),
                "elapsed": int((s.get("finished") or time.time()) - started)}


def scan_images(paths):
    """同步扫描（无进度、不可取消），返回文件列表。

    只给单元测试/脚本复用；HTTP 接口 /api/scan 走的是异步 new_scan + 分页查询。
    实现复用同一套 walker，保证"同步结果 == 异步分页拼起来的结果"。
    """
    s = _new_scan_state(paths)
    run_scan(s)
    return [dict(x) for x in s["files"]]


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caesium - Image Compressor</title>
<style>
:root{--c:#0e7d6b;--c2:#0a5a4d;--bg:#f0f2f1;--card:#fff;--line:#dde3e1;--tx:#222;--mut:#7a8683;--warn:#b45309;--err:#c0392b;--ok:#0e7d6b;--sel:#e8f4f0;--tab:#f7f9f8}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--tx);display:flex;flex-direction:column;min-height:100vh;font-size:13px}
/* 标题栏 */
.titlebar{background:#163a31;color:#fff;display:flex;align-items:center;gap:10px;padding:8px 14px;flex:0 0 auto}
.titlebar img{width:26px;height:26px;border-radius:6px}
.titlebar h1{font-size:15px;font-weight:600}
.titlebar .sp{flex:1}
.titlebar .engine{font-size:12px;opacity:.8;display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:#7fd8c3;display:inline-block}
.dot.off{background:#e05c5c}
/* 工具栏 */
.toolbar{background:var(--card);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:6px;padding:7px 12px;flex:0 0 auto;flex-wrap:wrap}
.tbtn{border:1px solid var(--line);background:#fff;color:var(--tx);border-radius:7px;padding:6px 12px;font-size:12.5px;cursor:pointer;transition:.15s;white-space:nowrap}
.tbtn:hover{border-color:var(--c);color:var(--c)}
.tbtn:disabled{opacity:.45;cursor:not-allowed}
.tbtn.danger:hover{border-color:var(--err);color:var(--err)}
.tbtn.primary{background:var(--c);border-color:var(--c);color:#fff;font-weight:600;padding:6px 18px}
.tbtn.primary:hover{background:var(--c2);color:#fff}
.toolbar .sp{flex:1}
/* 主区 */
.main{flex:1 1 auto;display:flex;min-height:0}
.listpane{flex:1 1 auto;display:flex;flex-direction:column;min-width:0;background:var(--card);margin:10px 0 10px 10px;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.listhead{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--line);font-size:12px;color:var(--mut);flex:0 0 auto}
.listhead .sp{flex:1}
.count{color:var(--c2);font-weight:600}
.tblwrap{flex:1 1 auto;overflow:auto;min-height:0}
table{width:100%;border-collapse:collapse;font-size:12px}
th{position:sticky;top:0;background:var(--tab);color:var(--mut);font-weight:500;text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);z-index:1;white-space:nowrap}
td{padding:6px 9px;border-bottom:1px solid #eef2f0;vertical-align:middle;white-space:nowrap}
tr:hover td{background:#f6faf8}
tr.sel td{background:var(--sel)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td .p{color:var(--mut);font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;display:block}
/* v1.7.3：文件名下方显示所属文件夹，区分"不同目录的同名图" */
.fdir{display:block;font-size:11px;color:var(--mut);opacity:.9;margin-top:1px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cb{width:15px;height:15px;accent-color:var(--c)}
.st-ok{color:var(--ok);font-weight:600}
.st-err{color:var(--err)}
.st-skip{color:#6b8a7d;font-weight:600}
/* v1.7.4：GIF 直通复制（不压缩），用现有灰色变量，不引入新配色 */
.st-copy{color:var(--mut);font-weight:600}
.st-wait{color:var(--mut)}
.st-run{color:var(--warn)}
.save{color:var(--ok)}
.empty{padding:30px 20px;text-align:center;color:var(--mut);font-size:12.5px}
/* v1.7.5：列表分页条（几千上万条时只渲染当前页，避免一次性铺满 DOM 卡死） */
.pager{flex:0 0 auto;display:flex;align-items:center;gap:6px;padding:6px 12px;border-top:1px solid var(--line);font-size:11.5px;color:var(--mut);background:#fbfdfc}
.pager .sp{flex:1}
.pbtn{border:1px solid var(--line);background:#fff;border-radius:6px;padding:3px 9px;font-size:11.5px;color:var(--txt,#333);cursor:pointer;line-height:1.5}
.pbtn:hover:not(:disabled){border-color:var(--c);color:var(--c)}
.pbtn:disabled{opacity:.4;cursor:not-allowed}
.pginfo{color:var(--c2);font-weight:600;white-space:nowrap}
.psize{display:inline-flex;align-items:center;gap:3px}
.psize select{border:1px solid var(--line);border-radius:6px;background:#fff;font-size:11.5px;padding:2px 4px;color:var(--txt,#333)}
.pgrange{white-space:nowrap;opacity:.85}
/* 预览条 */
.prevbar{flex:0 0 108px;border-top:1px solid var(--line);display:flex;align-items:center;gap:14px;padding:8px 14px;background:#fbfdfc}
.prevbox{display:flex;flex-direction:column;align-items:center;gap:4px;flex:0 0 auto}
.prevbox img{max-width:132px;max-height:66px;border:1px solid var(--line);border-radius:6px;background:#fff;object-fit:contain}
.prevbox img:not([src]){display:none}
.prevbox span{font-size:11px;color:var(--mut)}
.prevmid{flex:0 0 130px;text-align:center;font-size:11.5px;color:var(--mut);line-height:1.7}
.prevmid b{display:block;font-size:15px;color:var(--c2)}
.prevmid .bad{color:var(--warn)}
.prevnote{flex:1;font-size:11px;color:var(--mut)}
/* 右侧选项栏 */
.opts{flex:0 0 332px;background:var(--card);margin:10px 10px 10px 0;border:1px solid var(--line);border-radius:10px;display:flex;flex-direction:column;overflow:hidden;min-height:0}
.tabs{display:flex;border-bottom:1px solid var(--line);flex:0 0 auto}
.tab{flex:1;text-align:center;padding:9px 4px;font-size:12.5px;color:var(--mut);cursor:pointer;border-bottom:2px solid transparent}
.tab.active{color:var(--c);border-bottom-color:var(--c);font-weight:600}
.tabbody{flex:1 1 auto;overflow:auto;padding:12px 15px}
.opt{margin-bottom:13px}
.opt label{display:block;font-size:12px;color:var(--mut);margin-bottom:5px}
.opt select,.opt input[type=text]{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:7px;font-size:12.5px;background:#fff}
.opt select:focus,.opt input:focus{outline:none;border-color:var(--c)}
.range{display:flex;align-items:center;gap:9px}
.range input[type=range]{flex:1}
.range output{width:34px;text-align:right;font-size:12.5px;color:var(--c2);font-weight:600}
.check{display:flex;align-items:center;gap:7px;font-size:12.5px;cursor:pointer;margin-bottom:8px}
.check input{width:15px;height:15px;accent-color:var(--c)}
.sep{border-top:1px solid var(--line);margin:11px 0;padding-top:9px}
.sep-t{font-size:11px;font-weight:600;color:var(--c2);margin-bottom:8px;display:block}
.opt .desc{font-size:11px;color:var(--mut);margin-top:3px;line-height:1.5}
.perm-tip{background:#fff8ec;border:1px solid #f0d9a8;color:#8a5a12;border-radius:8px;padding:8px 10px;font-size:11.5px;line-height:1.6}
.perm-tip b{color:#6b4508}
.outrow{display:flex;gap:6px}
.outrow input{flex:1;min-width:0}
.outrow button{flex:0 0 auto}
/* 底部状态栏 */
.statusbar{background:var(--card);border-top:1px solid var(--line);padding:7px 12px;display:flex;align-items:center;gap:12px;flex:0 0 auto;font-size:12px;color:var(--mut)}
.statusbar .txt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.statusbar .txt.err{color:var(--err)}
.bar{width:180px;height:6px;background:#e4ece9;border-radius:4px;overflow:hidden;flex:0 0 auto}
.bar i{display:block;height:100%;width:0;background:var(--c);transition:width .3s}
.link{color:var(--c);cursor:pointer;text-decoration:underline;font-size:11.5px;flex:0 0 auto}
/* 压缩进行中：设置界面变灰锁定，仅取消可点；文件列表保持可选可点击 */
body.locked .toolbar,body.locked .tabs,body.locked .opts,body.locked .tabbody{opacity:.5;pointer-events:none;user-select:none}
body.locked .bar,body.locked #cancelLink{opacity:1;pointer-events:auto}
body.locked #barWrap,body.locked #cancelLink{pointer-events:auto}
/* modal */
.mask{position:fixed;inset:0;background:rgba(10,30,25,.45);display:none;align-items:center;justify-content:center;z-index:50}
.mask.show{display:flex}
.modal{background:#fff;border-radius:12px;width:min(640px,94vw);height:min(540px,86vh);display:flex;flex-direction:column;overflow:hidden;box-shadow:0 18px 50px rgba(0,0,0,.25)}
.mhead{display:flex;align-items:center;gap:10px;padding:11px 14px;border-bottom:1px solid var(--line);flex:0 0 auto}
.mhead h2{font-size:14px;flex:1}
.mhead .x{cursor:pointer;font-size:18px;color:var(--mut);padding:0 6px}
.mhead .x:hover{color:var(--err)}
.mpath{padding:8px 14px;border-bottom:1px solid var(--line);font-size:12px;background:var(--tab);display:flex;align-items:center;gap:7px;flex:0 0 auto}
.mpath input{flex:1;padding:5px 8px;border:1px solid var(--line);border-radius:6px;font-size:12px;min-width:0}
.mpath button{flex:0 0 auto}
.mbody{flex:1 1 auto;overflow:auto;padding:6px 0}
.msec{padding:7px 14px;font-size:11.5px;color:var(--mut);background:#fafcfb;border-bottom:1px solid #eef2f0;display:flex;align-items:center;gap:6px}
.msec .sp{flex:1}
.row{display:flex;align-items:center;gap:9px;padding:6px 14px;cursor:pointer;font-size:12.5px;border-bottom:1px solid #f1f5f3}
.row:hover{background:#f3faf8}
.row .ic{width:16px;text-align:center}
.row .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .sz{color:var(--mut);font-size:11px;white-space:nowrap}
.row .del{color:var(--err);cursor:pointer;font-size:14px;padding:0 4px;visibility:hidden}
.row:hover .del{visibility:visible}
.mfoot{display:flex;justify-content:flex-end;gap:8px;padding:10px 14px;border-top:1px solid var(--line);flex:0 0 auto}
.mbtn{border:1px solid var(--line);background:#fff;border-radius:7px;padding:6px 16px;font-size:12.5px;cursor:pointer}
.mbtn:hover{border-color:var(--c);color:var(--c)}
.mbtn.pri{background:var(--c);border-color:var(--c);color:#fff;font-weight:600}
.mbtn.pri:hover{background:var(--c2);color:#fff}
.mbtn:disabled{opacity:.45;cursor:not-allowed}
.authbanner{display:flex;align-items:center;gap:10px;background:var(--bg2);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:8px 0;font-size:12.5px;color:var(--mut)}
.authbanner .tbtn{padding:5px 14px}
.authbanner .tbtn.pri{background:var(--c);border-color:var(--c);color:#fff;font-weight:600}
.logwrap{background:#0d1b17;color:#bfe6db;font-family:Consolas,Menlo,monospace;font-size:11.5px;padding:10px 12px;white-space:pre-wrap;word-break:break-all;overflow:auto;flex:1}
.hidden{display:none}
</style>
</head>
<body>
<div class="titlebar" style="display:none">
  <img src="images/icon_64.png" alt="Caesium">
  <h1>Caesium - Image Compressor</h1>
  <span class="sp"></span>
  <span class="engine"><span class="dot" id="engineDot"></span><span id="engineTxt">检测中…</span></span>
</div>

<div class="authbanner hidden" id="authBanner">
  <span>尚未授权任何目录，点击「添加文件夹」选择目录即可完成授权（只显示已授权的文件夹）</span>
  <span class="sp"></span>
  <button class="tbtn" id="authDismiss">知道了</button>
  <button class="tbtn pri" id="authBtn">选择目录并授权</button>
</div>

<div class="toolbar">
  <button class="tbtn" id="addFileBtn">＋ 添加文件</button>
  <button class="tbtn" id="addFolderBtn">＋ 添加文件夹</button>
  <button class="tbtn danger" id="removeBtn">移除选中</button>
  <button class="tbtn danger" id="clearBtn">清空</button>
  <span class="sp"></span>
  <button class="tbtn" id="retryFailBtn" disabled>↻ 再压缩失败</button>
  <button class="tbtn" id="recompressAllBtn" disabled>全部重压</button>
  <button class="tbtn primary" id="startBtn" disabled>压缩</button>
</div>

<div class="main">
  <div class="listpane">
    <div class="listhead">
      <b>文件列表</b><span class="count" id="count">0 个项目</span>
      <span class="sp"></span>
      <span id="sumSize" style="font-size:11.5px"></span>
    </div>
    <div class="tblwrap" id="tblwrap">
      <div class="empty" id="empty">
        <div style="margin-bottom:6px">尚未添加文件</div>
        <div>点击「添加文件夹」选择 NAS 中的图片目录，或「添加文件」选择单个图片</div>
      </div>
      <table id="tbl" class="hidden">
        <thead><tr><th style="width:26px"></th><th>名称</th><th>分辨率</th><th class="num">大小</th><th class="num">已节省</th><th>状态</th></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="pager hidden" id="pager">
      <button class="pbtn" id="pageFirst">« 首页</button>
      <button class="pbtn" id="pagePrev">‹ 上一页</button>
      <span class="pginfo" id="pageInfo">第 1 / 1 页</span>
      <button class="pbtn" id="pageNext">下一页 ›</button>
      <button class="pbtn" id="pageLast">末页 »</button>
      <span class="sp"></span>
      <span class="pgrange" id="pageRange"></span>
      <span class="psize">每页
        <select id="pageSize">
          <option value="1000">1000</option>
          <option value="2000">2000</option>
          <option value="5000" selected>5000</option>
          <option value="10000">10000</option>
        </select> 个
      </span>
    </div>
    <div class="prevbar" id="prevbar">
      <div class="prevbox"><img id="prevOrig" alt="原图"><span>原图</span></div>
      <div class="prevmid" id="prevMid"><b>—</b><span>点击文件查看对比</span></div>
      <div class="prevbox"><img id="prevOut" alt="压缩后"><span>压缩后</span></div>
      <div class="prevnote" id="prevNote"></div>
    </div>
  </div>

  <div class="opts">
    <div class="tabs">
      <div class="tab active" data-tab="c">压缩选项</div>
      <div class="tab" data-tab="r">图片尺寸</div>
      <div class="tab" data-tab="o">输出</div>
    </div>

    <div class="tabbody" id="tab-c">
      <div class="opt">
        <label>模式</label>
        <select id="mode">
          <option value="quality">图片质量</option>
          <option value="lossless">无损压缩</option>
          <option value="target">目标大小</option>
        </select>
      </div>
      <div id="qualityPanel">
        <div class="opt">
          <label>JPEG 质量</label>
          <div class="range"><input type="range" id="jpegQ" min="1" max="100" value="90"><output id="jpegQOut">90</output></div>
        </div>
        <div class="opt">
          <label>PNG 质量</label>
          <div class="range"><input type="range" id="pngQ" min="1" max="100" value="90"><output id="pngQOut">90</output></div>
          <div class="desc">100 为最高质量（无损）</div>
        </div>
        <div class="opt">
          <label>PNG 优化级别</label>
          <div class="range"><input type="range" id="pngOpt" min="0" max="6" value="3"><output id="pngOptOut">3</output></div>
          <div class="desc">级别越高压缩率越大</div>
        </div>
        <div class="opt">
          <label>WebP 质量</label>
          <div class="range"><input type="range" id="webpQ" min="1" max="100" value="90"><output id="webpQOut">90</output></div>
        </div>
        <div class="opt">
          <label>TIFF 压缩</label>
          <select id="tiffAlg">
            <option value="deflate">Deflate（默认）</option>
            <option value="lzw">LZW</option>
            <option value="packbits">PackBits</option>
            <option value="uncompressed">无压缩</option>
          </select>
          <div class="desc">TIFF 为无损格式，选择压缩算法</div>
        </div>
        <span class="sep-t">JPEG 高级</span>
        <div class="opt">
          <label>色度二次采样</label>
          <select id="chroma">
            <option value="auto">自动</option>
            <option value="4:4:4">4:4:4（最佳色彩）</option>
            <option value="4:2:2">4:2:2</option>
            <option value="4:2:0">4:2:0（常用）</option>
            <option value="4:1:1">4:1:1（最小体积）</option>
          </select>
        </div>
        <div class="opt check"><input type="checkbox" id="progressive" checked><label for="progressive">渐进式 JPEG（默认开启）</label></div>
        <span class="sep-t">PNG 高级</span>
        <div class="opt check"><input type="checkbox" id="zopfli"><label for="zopfli">Zopfli 优化（更小但显著更慢）</label></div>
        <div class="opt check"><input type="checkbox" id="keepExif" checked><label for="keepExif">保留元数据（EXIF）</label></div>
      </div>
      <div class="opt hidden" id="targetRow">
        <label>目标大小（KB）</label>
        <input type="text" id="targetSize" value="200" inputmode="numeric">
        <div class="desc">压缩到不超过该大小（尽力而为）</div>
      </div>
    </div>

    <div class="tabbody hidden" id="tab-r">
      <div class="opt">
        <label>调整大小</label>
        <select id="resizeMode">
          <option value="none">不调整</option>
          <option value="long_edge">最长边</option>
          <option value="width">指定宽度</option>
          <option value="height">指定高度</option>
          <option value="percent">按百分比</option>
        </select>
      </div>
      <div class="opt hidden" id="resizeValRow">
        <label id="resizeValLabel">最长边像素</label>
        <input type="text" id="resizeVal" value="1920" inputmode="numeric">
      </div>
      <div class="opt check hidden"><input type="checkbox" id="noUpscale"><label for="noUpscale">不放大图片</label></div>
      <div class="opt"><div class="desc">等比缩放，保持原始宽高比</div></div>
    </div>

    <div class="tabbody hidden" id="tab-o">
      <div class="opt">
        <label>输出文件夹</label>
        <div class="outrow">
          <input type="text" id="outPath" placeholder="选择输出目录" readonly>
          <button class="tbtn" id="browseOutBtn">选择</button>
        </div>
      </div>
      <div class="opt check"><input type="checkbox" id="keepStruct" checked><label for="keepStruct">保留目录结构</label></div>
      <div class="opt">
        <label>已存在文件</label>
        <select id="existPolicy">
          <option value="skip">跳过（推荐）</option>
          <option value="overwrite">覆盖</option>
          <option value="rename">保留两份</option>
        </select>
        <div class="desc" id="existPolicyDesc">输出目录里已有同名文件时直接跳过，不重复压缩</div>
      </div>
      <div class="opt">
        <label>并行数</label>
        <select id="workers">
          <option value="0">自动（用满 CPU）</option>
          <option value="2">2</option>
          <option value="4">4</option>
          <option value="8">8</option>
          <option value="16">16</option>
          <option value="1">1（单线程）</option>
        </select>
        <div class="desc">同时压几张图（每多一路就多一个压缩进程）；内存紧张时调小</div>
      </div>
      <div class="opt">
        <label>导出格式</label>
        <select id="outFormat">
          <option value="keep">保持原格式</option>
          <option value="jpeg">JPEG（.jpg）</option>
          <option value="png">PNG</option>
          <option value="webp">WebP</option>
          <option value="tiff">TIFF</option>
        </select>
      </div>
      <div class="opt check"><input type="checkbox" id="keepDates" checked><label for="keepDates">保留文件时间</label></div>
      <div class="opt check"><input type="checkbox" id="sameFolder"><label for="sameFolder">输出到原文件夹（覆盖原图，慎用）</label></div>
      <div class="opt">
        <label>文件后缀（可选）</label>
        <input type="text" id="suffix" placeholder="如 _compressed">
      </div>
      <div class="perm-tip" id="permTip" style="display:none">
        <b>看不到你的文件夹？</b><br>
        1. 飞牛「设置 → 应用 → Caesium → 目录权限」添加文件夹（读写）<br>
        2. 回到本页刷新；或直接在文件夹选择器里粘贴路径
      </div>
    </div>
  </div>
</div>

<div class="statusbar">
  <span class="txt" id="statusTxt">就绪</span>
  <div class="bar" id="barWrap" style="display:none"><i id="bar"></i></div>
  <span class="link hidden" id="scanCancelLink">取消扫描</span>
  <span class="link hidden" id="logLink">查看日志</span>
  <span class="link hidden" id="cancelLink">取消</span>
</div>

<!-- 文件夹/文件浏览器 -->
<div class="mask" id="browserMask">
  <div class="modal">
    <div class="mhead"><h2 id="browserTitle">选择文件夹</h2><span class="x" id="browserClose">×</span></div>
    <div class="mpath">
      <span style="flex:0 0 auto;font-weight:600;color:var(--c2)" id="browserLoc">我的文件夹</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" id="browserPath"></span>
      <span class="link" id="manualLink">手动输入路径</span>
    </div>
    <div class="mpath hidden" id="manualRow">
      <input type="text" id="pathInput" placeholder="/vol1/1000/… 或粘贴路径" spellcheck="false">
      <button class="mbtn" id="pathGo">前往</button>
    </div>
    <div class="mbody" id="browserBody"></div>
    <div class="mfoot">
      <button class="mbtn" id="browserUp">上级目录</button>
      <span style="flex:1"></span>
      <button class="mbtn" id="browserCancel">取消</button>
      <button class="mbtn pri" id="browserOK">选择此文件夹</button>
    </div>
  </div>
</div>

<!-- 日志 -->
<div class="mask" id="logMask">
  <div class="modal">
    <div class="mhead"><h2>任务日志</h2><span class="x" id="logClose">×</span></div>
    <div class="logwrap" id="logBody"></div>
    <div class="mfoot"><button class="mbtn" id="logCloseBtn">关闭</button></div>
  </div>
</div>

<!-- 确认弹层：飞牛 iframe 里原生 confirm() 会被 sandbox 静默拦掉，改自绘 -->
<div class="mask" id="confirmMask">
  <div class="modal" style="height:auto;width:min(420px,90vw)">
    <div class="mhead"><h2 id="confirmTitle">确认</h2><span class="x" id="confirmClose">×</span></div>
    <div id="confirmText" style="padding:18px 16px;font-size:12.5px;line-height:1.7"></div>
    <div class="mfoot">
      <button class="mbtn" id="confirmCancel">取消</button>
      <button class="mbtn pri" id="confirmOK">确定</button>
    </div>
  </div>
</div>

<script>
(function(){
var $=function(id){return document.getElementById(id);};
var files=[]; var taskId=null, pollTimer=null;
/* 去重索引：以前是每来一个文件就 files.some(...) 扫全表，扫 2 万张图是 O(n²)（4 亿次比较，
   界面会像卡死）。改成 Set 之后每条 O(1)。 */
var pathIndex=new Set();
function rebuildPathIndex(){
  pathIndex=new Set();
  for(var i=0;i<files.length;i++)pathIndex.add(files[i].path);
}
var browserMode='folder', browserPick=null, curBrowsePath='';
var myFolders=[];
var ENC=encodeURIComponent;

function fmt(n){if(n==null)return'—';if(n<1024)return n+' B';if(n<1048576)return(n/1024).toFixed(1)+' KB';return(n/1048576).toFixed(2)+' MB';}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
/* 统一的安全 JSON 解析：飞牛网关等不到响应时会返回 **HTML 错误页**
   （<html><head><title>504 Gateway Time-out</title>…），此时不经本函数直接解析
   就会抛 `Unexpected token '<', "<html> <h"...` —— 用户完全看不懂。这里统一兜住，
   给一句人话提示；所有 fetch 都必须走这个函数，不许再直接解析响应体。 */
function jsonSafe(r){
  if(!r||typeof r.text!=='function')return Promise.reject(new Error('服务未响应，请稍后重试'));
  return r.text().then(function(t){
    var s=String(t==null?'':t).replace(/^\uFEFF/,'').trim();
    if(!s)return {};
    var c=s.charAt(0);
    if(c!=='{'&&c!=='['){
      if(/<\s*(!doctype|html|head|body|title)/i.test(s.slice(0,300))){
        throw new Error('服务未响应或超时，请稍后重试');
      }
      throw new Error('服务返回了异常内容，请稍后重试');
    }
    try{return JSON.parse(s);}catch(e){throw new Error('服务返回的内容无法解析，请稍后重试');}
  });
}

fetch('api/status').then(jsonSafe).then(function(j){
  if(j.clt){
    $('engineDot').classList.remove('off');
    var cores=j.cpus||'?', par=j.workers||'?';
    $('engineTxt').textContent='压缩引擎正常（'+cores+' 核 / 并行 '+par+'）';
  }
  else{$('engineDot').classList.add('off');$('engineTxt').textContent='压缩引擎不可用';}
}).catch(function(){$('engineDot').classList.add('off');$('engineTxt').textContent='服务未就绪';});

/* ---------- 标签页 ---------- */
document.querySelectorAll('.tab').forEach(function(t){
  t.onclick=function(){
    document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active');});
    t.classList.add('active');
    ['c','r','o'].forEach(function(k){$('tab-'+k).classList.toggle('hidden',k!==t.dataset.tab);});
  };
});
$('mode').onchange=function(){
  var m=this.value;
  $('qualityPanel').classList.toggle('hidden',m!=='quality');
  $('targetRow').classList.toggle('hidden',m!=='target');
};
function syncResize(){
  var m=$('resizeMode').value;
  $('resizeValRow').classList.toggle('hidden',m==='none');
  $('noUpscale').closest('.opt').classList.toggle('hidden',m==='none');
  var defs={long_edge:1920,width:1920,height:1080,percent:100};
  $('resizeValLabel').textContent={long_edge:'最长边像素',width:'宽度像素',height:'高度像素',percent:'百分比（%）'}[m]||'值';
  var d=defs[m];
  if(d!==undefined){$('resizeVal').value=d;$('resizeVal').placeholder=String(d);}
}
$('resizeMode').onchange=syncResize;
$('jpegQ').oninput=function(){$('jpegQOut').textContent=this.value;};
$('pngQ').oninput=function(){$('pngQOut').textContent=this.value;};
$('pngOpt').oninput=function(){$('pngOptOut').textContent=this.value;};
$('webpQ').oninput=function(){$('webpQOut').textContent=this.value;};

/* ---------- 文件列表 ---------- */
var selIdx=-1;
function countByStatus(){
  var c={ok:0,skip:0,copy:0,err:0,wait:0};
  files.forEach(function(f){
    if(f.status==='ok')c.ok++;
    else if(f.status==='skip')c.skip++;
    else if(f.status==='copied')c.copy++;      /* GIF 直通：已复制，也算"已完成" */
    else if(f.status==='err')c.err++;
    else c.wait++;
  });
  return c;
}
/* 取文件**直接父目录名**（不是用户选的扫描根 root，root 可能隔了好几层）。
   列表只显示文件名时，两个不同目录里的同名图看起来一模一样，用户会误以为
   「添加文件夹把别的文件当重复丢掉了」——把所在目录显示出来即可说清楚。 */
function dirOf(f){
  var parts=String((f&&f.path)||'').split(/[\\/]/);
  parts.pop();
  return parts.length?(parts[parts.length-1]||''):'';
}
/* ---------- 分页（v1.7.5） ----------
   几万个文件一次性铺进 DOM 会直接卡死：每行 6 个单元格，还要给每行挂 2 个事件闭包。
   现在**只渲染当前页**（默认 5000 条/页），翻页才重建，行内事件改成 tbody 上的事件委托。 */
var PAGE_SIZE=5000, curPage=0, LAST_SUM=0;
var PAGE_SIZES=[1000,2000,5000,10000];
function pageCount(){return Math.max(1,Math.ceil(files.length/PAGE_SIZE));}
function clampPage(){
  var pc=pageCount();
  if(curPage>pc-1)curPage=pc-1;
  if(curPage<0)curPage=0;
  return pc;
}
function statusHtml(f){
  if(f.status==='ok')return '<span class="st-ok">成功</span>';
  if(f.status==='skip')return '<span class="st-skip"'+(f.note?' title="'+esc(f.note)+'"':'')+'>已跳过</span>';
  if(f.status==='err')return '<span class="st-err">失败</span>';
  if(f.status==='copied')return '<span class="st-copy">已复制</span>';
  if(f.status==='run')return '<span class="st-run">压缩中</span>';
  return '<span class="st-wait">等待</span>';
}
function saveHtml(f){
  /* GIF 直通复制不产生"节省"，显示 — 而不是误导性的 0% */
  if(f.status==='copied'||f.out_size==null||f.in_size==null)return '—';
  var pct=Math.round(100*(1-f.out_size/f.in_size));
  if(pct<0)return '<span class="bad" style="color:var(--warn)">+'+(-pct)+'%</span>';
  return '<span class="save">−'+pct+'%</span>';
}
function rowHtml(f,i){
  var res=(f.w&&f.h)?(f.w+'×'+f.h):'—';
  var parentDir=dirOf(f);
  return '<tr data-i="'+i+'"'+(i===selIdx?' class="sel"':'')+'>'+
    '<td><input type="checkbox" class="cb"'+(f.sel?' checked':'')+'></td>'+
    '<td><span class="p" title="'+esc(f.path)+'">'+esc(f.name)+'</span>'+
    (parentDir?'<span class="fdir" title="'+esc(f.path)+'">'+esc(parentDir)+'/</span>':'')+'</td>'+
    '<td>'+res+'</td><td class="num">'+fmt(f.size)+'</td>'+
    '<td class="num">'+saveHtml(f)+'</td><td>'+statusHtml(f)+'</td></tr>';
}
function updateSummary(cs,sum){
  var n=files.length;
  $('count').textContent=n+' 个项目';
  $('sumSize').textContent=n?('合计 '+fmt(sum)+' · 成功 '+cs.ok+
    (cs.copy?' · 已复制 '+cs.copy:'')+' · 跳过 '+cs.skip+' · 失败 '+cs.err):'';
  $('empty').classList.toggle('hidden',n>0);
  $('tbl').classList.toggle('hidden',n===0);
}
function updatePager(){
  var n=files.length, pc=clampPage();
  var start=curPage*PAGE_SIZE, end=Math.min(start+PAGE_SIZE,n);
  $('pageInfo').textContent='第 '+(curPage+1)+' / '+pc+' 页';
  $('pageRange').textContent=n?((start+1)+'-'+end+' / '+n):'';
  $('pageFirst').disabled=$('pagePrev').disabled=(curPage<=0);
  $('pageLast').disabled=$('pageNext').disabled=(curPage>=pc-1);
  $('pageSize').value=String(PAGE_SIZE);
  /* 只有真的可能分页时才露这一条（几千条以上），小列表不占地方 */
  $('pager').classList.toggle('hidden',n<=1000);
}
function render(){
  var pc=clampPage();
  var start=curPage*PAGE_SIZE, end=Math.min(start+PAGE_SIZE,files.length);
  var sum=0, i;
  for(i=0;i<files.length;i++)sum+=(files[i].size||0);
  var html=[];
  for(i=start;i<end;i++)html.push(rowHtml(files[i],i));
  $('tbody').innerHTML=html.join('');
  var cs=countByStatus();
  LAST_SUM=sum;
  updateSummary(cs,sum);
  updatePager();
  syncButtons(cs);
}
/* 轮询期间只补更新当前页里"状态刚刚变了"的那几行：
   不重建 5000 行，压缩跑起来列表就不会一跳一跳的。 */
function patchRow(i){
  var start=curPage*PAGE_SIZE;
  if(i<start||i>=start+PAGE_SIZE)return;
  var tr=$('tbody').children[i-start];
  if(!tr||tr.children.length<6)return;
  tr.children[4].innerHTML=saveHtml(files[i]);
  tr.children[5].innerHTML=statusHtml(files[i]);
}
function refreshSummary(){
  var cs=countByStatus();
  updateSummary(cs,LAST_SUM);
  syncButtons(cs);
}
function gotoPage(p){
  curPage=p;
  render();
  var w=$('tblwrap'); if(w)w.scrollTop=0;
}
$('pageFirst').onclick=function(){gotoPage(0);};
$('pagePrev').onclick=function(){gotoPage(curPage-1);};
$('pageNext').onclick=function(){gotoPage(curPage+1);};
$('pageLast').onclick=function(){gotoPage(pageCount()-1);};
$('pageSize').onchange=function(){
  var v=parseInt(this.value,10);
  if(PAGE_SIZES.indexOf(v)<0)v=5000;
  var first=curPage*PAGE_SIZE;          /* 换每页条数时尽量停在原位置附近 */
  PAGE_SIZE=v;
  curPage=Math.floor(first/PAGE_SIZE);
  render();
};

/* 事件委托：行是 innerHTML 批量生成的，不能再给每行挂闭包（几千个闭包就是卡的原因之一） */
$('tbody').addEventListener('click',function(ev){
  var tr=ev.target&&ev.target.closest?ev.target.closest('tr'):null;
  if(!tr||tr.parentNode!==this)return;
  if(ev.target.tagName==='INPUT')return;
  var i=parseInt(tr.getAttribute('data-i'),10);
  if(isNaN(i)||!files[i])return;
  selIdx=i;
  showPreview(files[i]);
});
$('tbody').addEventListener('change',function(ev){
  var t=ev.target;
  if(!t||!t.classList||!t.classList.contains('cb'))return;
  var tr=t.closest?t.closest('tr'):null;
  if(!tr)return;
  var i=parseInt(tr.getAttribute('data-i'),10);
  if(isNaN(i)||!files[i])return;
  files[i].sel=!!t.checked;
  tr.classList.toggle('sel',!!t.checked);
});

/* 按钮可用状态 + 主按钮文案（有已完成项时变成「继续压缩（已完成 N）」） */
function syncButtons(cs){
  /* 「已完成」= 成功 + 已跳过 + 已复制（GIF）——否则重开应用会把 GIF 重压一遍 */
  /* 可能被当成事件回调直接挂上（$('outPath').addEventListener('change',syncButtons)），
     那时第一个参数是 Event，不能当成统计结果用。 */
  if(!cs||typeof cs!=='object'||typeof cs.ok!=='number')cs=countByStatus();
  var doneCount=cs.ok+cs.skip+cs.copy;
  $('startBtn').disabled=files.length===0||(!$('outPath').value&&!$('sameFolder').checked);
  $('startBtn').textContent=doneCount>0?('继续压缩（已完成 '+doneCount+'）'):'压缩';
  $('recompressAllBtn').disabled=!(files.length>0&&doneCount>0);
  $('retryFailBtn').disabled=!files.some(function(f){return f.status==='err';});
  if(!document.body.classList.contains('locked')){
    $('existPolicy').disabled=$('sameFolder').checked;
  }
}

$('addFileBtn').onclick=function(){
  pickNativeFiles().then(function(paths){
    if(paths===null){openBrowser('file',function(p){addPaths([p],false);});return;}
    if(!paths.length)return;
    addPaths(paths,false);
  });
};
$('addFolderBtn').onclick=function(){
  pickNativeDir().then(function(paths){
    if(paths===null){openBrowser('folder',function(p){addPaths([p],true);});return;}
    if(!paths.length)return;
    registerFolders(paths);
    addPaths(paths,true);
  });
};
/* 自绘确认弹层（替代 confirm()：飞牛 iframe 的 sandbox 会把原生弹窗静默拦掉） */
var confirmCb=null, confirmCancelCb=null;
function askConfirm(text,cb,title,onCancel){
  $('confirmTitle').textContent=title||'确认';
  $('confirmText').textContent=text;
  confirmCb=cb||null;
  confirmCancelCb=onCancel||null;
  $('confirmMask').classList.add('show');
}
function closeConfirm(){
  $('confirmMask').classList.remove('show');
  confirmCb=null;confirmCancelCb=null;
}
$('confirmOK').onclick=function(){var cb=confirmCb;closeConfirm();if(cb)cb();};
$('confirmCancel').onclick=function(){var cb=confirmCancelCb;closeConfirm();if(cb)cb();};
$('confirmClose').onclick=function(){var cb=confirmCancelCb;closeConfirm();if(cb)cb();};
$('confirmMask').onclick=function(e){
  if(e.target!==$('confirmMask'))return;
  var cb=confirmCancelCb;closeConfirm();if(cb)cb();
};

$('removeBtn').onclick=function(){
  files=files.filter(function(f){return !f.sel;});
  rebuildPathIndex();selIdx=-1;render();saveQueue();
};
$('clearBtn').onclick=function(){
  if(!files.length){files=[];rebuildPathIndex();curPage=0;selIdx=-1;render();saveQueue();return;}
  askConfirm('清空全部 '+files.length+' 个文件？已压缩的结果记录会保留，可「继续压缩」。',function(){
    files=[];rebuildPathIndex();curPage=0;selIdx=-1;render();saveQueue();
  });
};

/* 文件列表自动持久化：添加/移除/清空/扫描结束时保存，打开页面时恢复。
   为什么要分批：一次 POST 全量时，2000 条 ≈ 400KB、5000 条 ≈ 1MB —— 飞牛网关的
   client_max_body_size 很可能就是 1MB，超了会静默 413，队列根本没存上（前端还把
   异常 catch 掉了，用户完全看不出来）。现在每批 ≤500 条串行发，单批 ~100KB。 */
var QUEUE_BATCH=500, queueSaving=false, queueDirty=false, queueWaiters=[];
/* 与服务端 QUEUE_MAX_FILES 保持一致：队列最多存 5 万条（= 扫描上限），
   超过就不能用「队列引用」提交，否则会悄悄少压。 */
var QUEUE_MAX_FILES=50000;
/* saveQueue() 现在返回 Promise（存完就 resolve）。doCompress 大列表时要先等队列落盘，
   再用「队列引用」方式提交任务 —— 否则 2 万条文件的 POST 体会超网关上限。 */
function queueFlushWaiters(){
  var ws=queueWaiters; queueWaiters=[];
  ws.forEach(function(fn){try{fn();}catch(e){}});
}

function queueSnapshot(){
  return files.map(function(f){return{path:f.path,name:f.name,size:f.size,ext:f.ext,root:f.root,w:f.w,h:f.h};});
}

function saveQueue(){
  if(queueSaving){queueDirty=true;return new Promise(function(res){queueWaiters.push(res);});}
  var p=new Promise(function(res){queueWaiters.push(res);});
  var data=queueSnapshot(), i=0, sent=false;
  queueSaving=true;
  function finish(){
    queueSaving=false;
    if(queueDirty){queueDirty=false;saveQueue();return;}  /* 新一轮会把 waiter 一起解决 */
    queueFlushWaiters();
  }
  function step(){
    var batch=data.slice(i,i+QUEUE_BATCH);
    if(!batch.length&&sent){finish();return;}    /* 空列表也要发一次（把服务端清空） */
    var first=!sent;
    sent=true;
    i+=batch.length;
    fetch('api/queue',{method:'POST',headers:{'Content-Type':'application/json'},
                       body:JSON.stringify({files:batch,append:!first})})
    .then(jsonSafe)
    .then(function(j){
      if(j&&j.truncated){
        setStatus('文件列表过长，服务端仅保存最近 '+(j.kept||0)+' 条（共 '+(j.count||0)+' 条）',true);
      }
      step();
    })
    .catch(function(e){
      /* 半途失败会留下半份队列。保留 queueDirty=true：下一次状态变化触发 saveQueue
         成功收尾时会自动补存一次（QA B7b 要的"自动重试机会"），提示仍然如实给出。 */
      queueSaving=false;queueDirty=true;
      setStatus('文件列表保存失败：'+((e&&e.message)||'未知错误')+'（刷新后可能只恢复部分）',true);
      queueFlushWaiters();          /* 失败也要放行等待者，否则提交按钮会一直挂着 */
    });
  }
  step();
  return p;
}
function loadQueue(){
  fetch('api/queue').then(jsonSafe).then(function(j){
    if(j.files&&j.files.length){
      files=(j.files||[]).map(function(f){
        return{path:f.path,name:f.name,size:f.size,ext:f.ext,root:f.root,w:f.w,h:f.h,sel:false,status:null};
      });
      rebuildPathIndex();
      render();
      setStatus('已恢复上次添加的 '+files.length+' 个文件');
    }
    refreshDoneStates();
  }).catch(function(){refreshDoneStates();});
}

/* ---------- 断点续传：把服务端「当前参数下已完成」的文件标回来 ----------
   分批查：2 万个文件一次性 POST 大概 2MB，会撞网关 1MB 上限（静默失败 → 断点续传
   在大列表上等于失效）。这里按 2000 条一批串行查，逐批合并结果。 */
var RESULTS_CHUNK=2000, refreshToken=0;
function refreshDoneStates(){
  if(taskId||!files.length)return;   // 有任务在跑时由 poll() 负责刷新，避免互相覆盖
  var opts=collectOptions();
  var out=$('sameFolder').checked?'':$('outPath').value.trim();
  var same=$('sameFolder').checked, keep=$('keepStruct').checked;
  var token=++refreshToken, done={}, i=0, hit=0;
  function apply(){
    if(token!==refreshToken)return;      // 期间又发起了一次，旧结果作废
    files.forEach(function(f){
      var r=done[f.path];
      if(r){f.status=(r.status==='skipped'?'skip':(r.status==='copied'?'copied':'ok'));
            f.out=r.out;f.in_size=r.in_size;f.out_size=r.out_size;hit++;}
    });
    render();
    if(hit)setStatus('已有 '+hit+' 个文件在当前参数下压好了，点「继续压缩」只处理剩下的');
  }
  function step(){
    if(token!==refreshToken)return;
    var batch=files.slice(i,i+RESULTS_CHUNK);
    i+=batch.length;
    if(!batch.length){apply();return;}
    var body={
      files:batch.map(function(f){return{path:f.path,root:f.root||''};}),
      options:opts, output:out, same_folder:same, keep_structure:keep
    };
    fetch('api/results/status',{method:'POST',headers:{'Content-Type':'application/json'},
                               body:JSON.stringify(body)})
    .then(jsonSafe)
    .then(function(j){var d=(j&&j.done)||{};for(var k in d)done[k]=d[k];step();})
    .catch(function(){apply();});      // 某批失败就用手上已有的结果，不整段放弃
  }
  step();
}

/* ---------- 大目录扫描：异步任务 + 分页增量拉取 ---------- */
/* 后端 /api/scan 现在**立刻**返回 scan_id，扫描在后台线程跑；结果按页拉，
   几万个文件也不会塞进一个响应（那样又会把网关拖到超时）。 */
var scanId=null, scanTimer=null, scanOffset=0, scanTotal=0, scanAdded=0, scanDup=0;
var scanInFlight=0;                  /* 在飞的 status 请求数（>0 就跳过本轮 tick） */
var SCAN_PAGE=500, SCAN_MAX_PAGES=5, SCAN_TICK=500;

function stopScanPoll(){if(scanTimer){clearInterval(scanTimer);scanTimer=null;}}

/* 扫描中禁用两个「添加」按钮（避免叠加扫描），并把「取消扫描」露出来 */
function setScanBusy(on){
  var locked=document.body.classList.contains('locked');
  $('addFileBtn').disabled=!!on||locked;
  $('addFolderBtn').disabled=!!on||locked;
  $('scanCancelLink').classList.toggle('hidden',!on);
  if(on){$('barWrap').style.display='block';$('bar').style.width='2%';}
  else if(!taskId){$('barWrap').style.display='none';}
}

/* 把一页结果 append 进 files（按 path 去重），返回本页新增条数 */
function appendScanFiles(list){
  var add=0;
  (list||[]).forEach(function(f){
    if(!f||!f.path)return;
    if(pathIndex.has(f.path)){scanDup++;return;}
    files.push({path:f.path,name:f.name,size:f.size,ext:f.ext,root:f.root,sel:false,status:null,
                w:f.w,h:f.h});
    pathIndex.add(f.path);
    add++;
  });
  scanAdded+=add;
  return add;
}

function scanTick(){if(scanId&&scanInFlight===0)pullScanPage(0);}

/* 拉一页；一个 tick 内最多连拉 SCAN_MAX_PAGES 页，避免 5 万文件要等 100 个 tick。
   scanInFlight 做互斥：上一轮还没回来就跳过本轮 —— 否则弱网下两个 tick 会各拉一页，
   同一段 offset 被重复拉（实测重复 2000~4499 条），还把"落后"放大成"丢尾页"。 */
function pullScanPage(depth){
  if(!scanId)return;
  scanInFlight++;
  fetch('api/scan/status?id='+ENC(scanId)+'&offset='+scanOffset+'&limit='+SCAN_PAGE)
  .then(jsonSafe)
  .then(function(j){
    if(j&&j.error){throw new Error(String(j.error)==='scan not found'?'扫描任务已失效，请重新添加':String(j.error));}
    var before=scanOffset;
    var add=appendScanFiles(j.files);
    scanOffset=(j.next_offset!=null)?j.next_offset:(before+((j.files||[]).length));
    var progressed=scanOffset>before;        /* 本页有没有真的往前走 */
    scanTotal=j.total||0;
    if(j.state==='running'){
      setStatus('正在扫描… 已发现 '+scanTotal+' 个'+(j.scanned?('（已读取 '+j.scanned+' 项）'):''));
      $('bar').style.width=Math.min(90,2+Math.ceil(scanTotal/50))+'%';
      if(add>0&&depth===0)render();          /* 渲染节流：每个 tick 最多重绘一次 */
      if(progressed&&scanOffset<scanTotal&&depth<SCAN_MAX_PAGES)pullScanPage(depth+1);
      return;
    }
    /* 收尾帧：服务端说"扫完了"，但客户端可能还落后（弱网/一帧只给 500 条）。
       只按 state 收尾会把尾页永久丢掉（QA P0：5000 张只收到 1500~4545 张）。
       只要还没拉完就继续拉，绝不在中途收尾。
       注意：这里**不能**因为 truncated=true 就跳过 —— truncated 只代表"目录没扫全
       （到 50000 上限）"，不代表"已收录的这几页可以少拉"（QA S10b：500/1500）。
       是否该告警「结果可能不完整」由 finishScan 单独判断（那里才看 truncated）。
       progressed 是防死循环闸：服务端（或桩）返回没有推进 offset 的重复页时，
       不能无限轮询下去，直接收尾由 finishScan 如实说明。 */
    if(progressed&&scanOffset<scanTotal){
      setStatus('正在整理扫描结果…（已收 '+scanOffset+'/'+scanTotal+'）');
      if(depth<SCAN_MAX_PAGES)pullScanPage(depth+1);   /* 同一 tick 内接着拉 */
      return;                                          /* 剩下的下个 tick 继续 */
    }
    finishScan(j);
  })
  .catch(function(e){failScan(e);})
  .then(function(){if(scanInFlight>0)scanInFlight--;});   /* 成功/失败都要放行下一轮 */
}

function finishScan(j){
  stopScanPoll();
  var state=j.state||'done', trunc=!!j.truncated;
  var total=j.total||0, consumed=scanAdded+scanDup;
  scanId=null; scanOffset=0;
  setScanBusy(false);
  render();
  saveQueue();
  if(state==='canceled'){setStatus('已取消扫描（列表里已有 '+files.length+' 个文件）');return;}
  if(state==='error'){setStatus('扫描失败：'+(j.error||'未知错误'),true);return;}
  var msg='添加 '+scanAdded+' 个文件';
  /* 说清楚是"同一路径已在列表中"，不是把不同文件夹的东西随便丢掉。
     想全部重加就先「清空」——scanDup 不是错误，isErr 不因它置 true。 */
  if(scanDup)msg+='，'+scanDup+' 个文件已在列表中（同一路径），未重复添加；如需全部重加请先点「清空」';
  if(trunc)msg+='（目录太大，已到上限 '+(total||files.length)+' 个，其余未加入）';
  /* 自检：拉到的条数对不上 total 时如实说明，不许闷着当成功（宁可让用户看见） */
  if(!trunc&&total&&consumed<total)msg+='；结果可能不完整（已收 '+consumed+'/'+total+'）';
  setStatus(msg,trunc||(!trunc&&total&&consumed<total));
}

function failScan(e){
  stopScanPoll();
  scanId=null; scanOffset=0;
  setScanBusy(false);
  if(!files.length&&!taskId){$('barWrap').style.display='none';}
  setStatus('扫描失败：'+((e&&e.message)||'未知错误'),true);
}

function addPaths(paths,isDir){
  if(scanId){setStatus('正在扫描，请稍候…',true);return;}
  scanAdded=0;scanDup=0;scanOffset=0;scanTotal=0;
  setStatus('正在扫描图片…');
  setScanBusy(true);
  fetch('api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:paths})})
  .then(jsonSafe)
  .then(function(j){
    if(!j||!j.scan_id)throw new Error('服务未返回扫描任务编号');
    scanId=j.scan_id;
    scanTimer=setInterval(scanTick,SCAN_TICK);
    scanTick();                       /* 立刻先拉一页，别等 500ms */
  })
  .catch(function(e){failScan(e);});
}

$('scanCancelLink').onclick=function(){
  if(!scanId)return;
  setStatus('正在取消扫描…');
  fetch('api/scan/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scan_id:scanId})})
  .then(jsonSafe)
  .catch(function(){});               /* 就算这次请求失败，轮询也会收到 canceled 并收尾 */
};

/* ---------- 预览 ---------- */
function showPreview(f){
  var o=$('prevOrig'), c=$('prevOut');
  // 加载失败/无法渲染的图片 → 移除 src 隐藏（不显示坏图）
  o.onerror=function(){o.removeAttribute('src');};
  c.onerror=function(){c.removeAttribute('src');};
  o.src='api/img?path='+ENC(f.path);
  $('prevMid').innerHTML='<b>—</b><span>未压缩</span>';
  c.removeAttribute('src');
  $('prevNote').textContent=f.path;
  if(f.out&&(f.status==='ok'||f.status==='skip')){
    c.src='api/img?path='+ENC(f.out);
    var pct=f.in_size?Math.round(100*(1-f.out_size/f.in_size)):null;
    if(pct!=null){
      var cls=pct>=0?'':'bad';
      $('prevMid').innerHTML='<b class="'+cls+'">'+(pct>=0?'−':'+')+Math.abs(pct)+'%</b><span>'+(pct>=0?'已节省':'变大')+'</span>';
    }else $('prevMid').innerHTML='<b>✓</b><span>压缩完成</span>';
  }
}

/* ---------- 飞牛官方授权选择器（100zip 同款方案：系统原生目录选择） ---------- */
var fnosSdk=null, fnosSdkReady=null;
function loadFnosSdk(){
  if(fnosSdkReady)return fnosSdkReady;
  fnosSdkReady=import('./sdk/trimjs-web-app.js').then(function(m){
    var C=(m&&(m.TrimApp||m.default))||null;
    if(!C)return null;
    try{fnosSdk=new C();}catch(e){fnosSdk=null;}
    return fnosSdk;
  }).catch(function(){fnosSdk=null;return null;});
  return fnosSdkReady;
}
function fnosStandalone(){
  if(fnosSdk&&typeof fnosSdk.isStandaloneWeb==='boolean')return fnosSdk.isStandaloneWeb;
  return window===window.top;
}
function fnosState(){return Math.random().toString(36).slice(2)+Date.now().toString(36);}
function fnosOpenAuth(params){
  var st=fnosState();
  try{sessionStorage.setItem('caesium.authState',st);}catch(e){}
  return fnosSdk.openAppAuth('pickUserFile',
    Object.assign({appName:'caesium',redirectUri:'/app/caesium/callback.html',state:st},params),
    {target:'_self'}).then(function(){return [];});
}
/* 打开系统原生目录授权选择器：解析为路径数组；SDK 不可用返回 null（回退自建弹窗），
   独立浏览器跳转授权/用户取消返回 []（静默） */
function pickNativeDir(){
  return loadFnosSdk().then(function(){
    if(!fnosSdk||typeof fnosSdk.pickUserFile!=='function')return null;
    var params={directory:true,title:'选择要授权给 Caesium 的目录',okText:'确认授权'};
    if(fnosStandalone()&&typeof fnosSdk.openAppAuth==='function')return fnosOpenAuth(params);
    return fnosSdk.pickUserFile(params).then(function(res){return (res&&res.data)||[];});
  });
}
function pickNativeFiles(){
  return loadFnosSdk().then(function(){
    if(!fnosSdk||typeof fnosSdk.pickUserFile!=='function')return null;
    var params={directory:false,multiple:true,title:'选择图片文件',okText:'确认',
                accept:['.jpg','.jpeg','.png','.webp','.gif','.tif','.tiff','.bmp']};
    if(fnosStandalone()&&typeof fnosSdk.openAppAuth==='function')return fnosOpenAuth(params);
    return fnosSdk.pickUserFile(params).then(function(res){return (res&&res.data)||[];});
  });
}
function registerFolders(paths){
  (paths||[]).forEach(function(p){
    if(!p)return;
    fetch('api/folders/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})}).catch(function(){});
  });
}
/* 授权回调带回的目录（独立浏览器 openAppAuth → callback.html → sessionStorage） */
(function(){
  var raw=null;
  try{raw=sessionStorage.getItem('caesium.pendingPaths');sessionStorage.removeItem('caesium.pendingPaths');}catch(e){}
  if(raw){
    var pend=[];try{pend=JSON.parse(raw);}catch(e){}
    pend=pend.filter(function(p){return typeof p==='string'&&p;});
    if(pend.length){registerFolders(pend);addPaths(pend,true);}
  }
})();

/* 首次授权引导：无已授权目录时显示横幅（对标 100zip 授权引导） */
(function(){
  fetch('api/fs/roots').then(jsonSafe).then(function(j){
    if(!(j.roots||[]).length){$('authBanner').classList.remove('hidden');}
  }).catch(function(){});
})();
$('authBtn').onclick=function(){
  pickNativeDir().then(function(paths){
    if(paths===null){openBrowser('folder',function(p){registerFolders([p]);addPaths([p],true);});return;}
    if(!paths.length)return;
    registerFolders(paths);addPaths(paths,true);
    $('authBanner').classList.add('hidden');
  });
};
$('authDismiss').onclick=function(){$('authBanner').classList.add('hidden');};

/* ---------- 文件浏览器 ---------- */
function openBrowser(mode,cb){
  browserMode=mode; browserPick=cb;
  $('browserTitle').textContent=mode==='folder'?'选择文件夹':'选择图片文件';
  $('browserOK').textContent=mode==='folder'?'选择此文件夹':'选择此文件';
  $('browserOK').classList.toggle('hidden',mode!=='folder');
  curBrowsePath='';
  loadMyFolders();
  $('browserMask').classList.add('show');
}
function closeBrowser(){$('browserMask').classList.remove('show');}

function loadMyFolders(){
  curBrowsePath=''; $('browserLoc').textContent='我的文件夹';
  $('browserPath').textContent='';
  $('manualRow').classList.add('hidden');
  var body=$('browserBody');
  body.innerHTML='<div class="row" style="color:var(--mut)">加载中…</div>';
  fetch('api/fs/roots').then(jsonSafe).then(function(j){
    myFolders=j.roots||[];
    body.innerHTML='';
    var sec=document.createElement('div');sec.className='msec';
    sec.innerHTML='<b>已授权文件夹（点击选择）</b><span class="sp"></span><span>'+myFolders.length+' 个</span>';
    body.appendChild(sec);
    if(!myFolders.length){
      $('manualLink').classList.remove('hidden');
      var e=document.createElement('div');e.className='empty';
      e.innerHTML='还没有发现可访问的文件夹<br>请先在飞牛「设置 → 应用 → Caesium → 目录权限」添加文件夹（读写），<br>授权后点下方「重新加载」';
      body.appendChild(e);
      var reload=document.createElement('div');
      reload.className='mfoot';reload.style.borderTop='none';reload.style.justifyContent='center';
      reload.innerHTML='<button class="mbtn pri" id="reloadBtn">重新加载</button>';
      body.appendChild(reload);
      $('reloadBtn').onclick=loadMyFolders;
      return;
    }
    myFolders.forEach(function(f){
      var row=document.createElement('div');row.className='row';
      row.innerHTML='<span class="ic">📁</span><span class="nm">'+esc(f.path)+'</span><span class="sz">选择 ›</span><span class="del" title="从列表移除">×</span>';
      row.onclick=function(){loadList(f.path);};
      row.querySelector('.del').onclick=function(ev){
        ev.stopPropagation();
        fetch('api/folders/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:f.path})});
        loadMyFolders();
      };
      body.appendChild(row);
    });
  }).catch(function(e){body.innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>';});
}
function loadList(path){
  curBrowsePath=path;
  $('browserLoc').textContent='当前路径';
  $('browserPath').textContent=path;
  $('manualRow').classList.add('hidden');
  var body=$('browserBody');
  body.innerHTML='<div class="row" style="color:var(--mut)">加载中…</div>';
  fetch('api/fs/list?path='+ENC(path)).then(jsonSafe).then(function(j){
    body.innerHTML='';
    var sec=document.createElement('div');sec.className='msec';
    sec.innerHTML='<b>'+esc(path)+'</b><span class="sp"></span>';
    body.appendChild(sec);
    if(j.error){body.innerHTML='<div class="empty">'+esc(j.error)+'</div>';return;}
    j.dirs.forEach(function(d){
      var row=document.createElement('div');row.className='row';
      row.innerHTML='<span class="ic">📁</span><span class="nm">'+esc(d.name)+'</span><span class="sz"></span>';
      row.onclick=function(){loadList(d.path);};
      body.appendChild(row);
    });
    if(browserMode==='file'){
      j.files.forEach(function(f){
        var row=document.createElement('div');row.className='row';
        row.innerHTML='<span class="ic">🖼</span><span class="nm">'+esc(f.name)+'</span><span class="sz">'+fmt(f.size)+(f.w?' · '+f.w+'×'+f.h:'')+'</span>';
        row.onclick=function(){browserPick(f.path);closeBrowser();};
        body.appendChild(row);
      });
    }
    if(!j.dirs.length&&!(browserMode==='file'&&j.files.length)){
      var e=document.createElement('div');e.className='empty';
      e.textContent='此目录下没有可访问的子文件夹'+(browserMode==='file'?'或图片文件':'');
      body.appendChild(e);
    }
  }).catch(function(e){body.innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>';});
}

$('manualLink').onclick=function(){
  $('manualRow').classList.toggle('hidden');
  if(!$('manualRow').classList.contains('hidden'))$('pathInput').focus();
};

$('pathGo').onclick=function(){
  var p=$('pathInput').value.trim();
  if(!p)return;
  fetch('api/folders/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})})
  .then(function(){}).catch(function(){});
  loadList(p);
};
$('pathInput').onkeydown=function(e){if(e.key==='Enter')$('pathGo').click();};
$('browserClose').onclick=closeBrowser;
$('browserCancel').onclick=closeBrowser;
$('browserUp').onclick=function(){
  if(!curBrowsePath){loadMyFolders();return;}
  var p=curBrowsePath.replace(/\/+$/,'');
  var i=p.lastIndexOf('/');
  var parent=i>0?p.slice(0,i):'';
  if(parent)loadList(parent);else loadMyFolders();
};
$('browserOK').onclick=function(){
  if(browserMode==='folder'&&curBrowsePath){
    fetch('api/folders/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:curBrowsePath})});
    browserPick(curBrowsePath);closeBrowser();
  }
};
$('browserMask').onclick=function(e){if(e.target===$('browserMask'))closeBrowser();};

/* ---------- 输出 ---------- */
$('browseOutBtn').onclick=function(){
  pickNativeDir().then(function(paths){
    if(paths===null){
      openBrowser('folder',function(p){$('outPath').value=p;$('startBtn').disabled=files.length===0;});
      return;
    }
    if(!paths.length)return;
    registerFolders(paths);
    $('outPath').value=paths[0];
    $('startBtn').disabled=files.length===0;
  });
};
/* 已存在文件策略：文案与可用性随「输出到原文件夹」联动 */
function updateExistDesc(){
  var same=$('sameFolder').checked;
  var el=$('existPolicy'), desc=$('existPolicyDesc');
  el.disabled=same||document.body.classList.contains('locked');
  if(same){desc.textContent='输出到原文件夹时目标就是原图，只能覆盖原图';return;}
  var v=el.value;
  if(v==='overwrite')desc.textContent='输出目录里已有同名文件时直接覆盖重压';
  else if(v==='rename')desc.textContent='输出目录里已有同名文件时保留两份，新的存为「名称 (2)」';
  else desc.textContent='输出目录里已有同名文件时直接跳过，不重复压缩';
}
$('existPolicy').onchange=updateExistDesc;

/* 「输出到原文件夹」= 直接覆盖原图，不可恢复：勾选时必须给一次明确确认。
   注意不能在飞牛 iframe 里用原生 alert()/confirm()（sandbox 会静默拦掉，
   等于没有任何提示），所以走自绘确认层；点「取消」就把勾选回退掉。 */
function revertSameFolder(){
  var el=$('sameFolder');
  if(!el||!el.checked)return;
  el.checked=false;
  $('browseOutBtn').disabled=false;
  updateExistDesc();
  syncButtons();
  setStatus('已取消「输出到原文件夹」：原图不会被覆盖');
}
$('sameFolder').onchange=function(){
  if(this.checked){
    $('outPath').value='';
    $('browseOutBtn').disabled=true;
    updateExistDesc();
    syncButtons();
    askConfirm('输出到原文件夹会覆盖原图，请谨慎使用。确认继续？',function(){
      setStatus('已启用「输出到原文件夹」：压缩结果会直接覆盖原图，请确认已有备份');
    },'确认覆盖原图',revertSameFolder);
    return;
  }
  $('browseOutBtn').disabled=false;
  updateExistDesc();
  syncButtons();
};
/* 输出目录变化后同步按钮可用态（选择器/手动输入/程序化赋值都走这里兜底） */
$('outPath').addEventListener('change',syncButtons);

/* 任何影响压缩结果的参数一改，之前的「已完成」状态就作废（sig 变了必须重压），
   否则用户改了质量却被跳过，会以为功能坏了。 */
var SIG_OPT_IDS=['mode','jpegQ','pngQ','pngOpt','webpQ','tiffAlg','chroma','progressive','zopfli',
                 'keepExif','targetSize','resizeMode','resizeVal','noUpscale','outFormat','keepDates','suffix'];
SIG_OPT_IDS.forEach(function(id){
  var el=$(id);
  if(!el)return;
  var handler=function(){
    var had=false;
    files.forEach(function(f){if(f.status)had=true;f.status=null;f.out=null;f.out_size=null;f.in_size=null;f.note='';});
    if(had)setStatus('压缩参数已改动，之前的完成状态已重置');
    render();
  };
  el.addEventListener('change',handler);
  el.addEventListener('input',handler);
});

/* ---------- 任务 ---------- */
function setStatus(t,isErr){var s=$('statusTxt');s.textContent=t;s.classList.toggle('err',!!isErr);}
function stopPoll(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}

/* 收集当前所有压缩参数（同时用于提交任务与查询已完成状态） */
function collectOptions(){
  var opts={mode:'quality',lossless:false,jpeg_quality:90,png_quality:90,png_opt_level:3,webp_quality:90,
            tiff_algorithm:'deflate',chroma:'auto',progressive:true,zopfli:false,keep_exif:true,suffix:'',
            format:'keep',keep_dates:true,long_edge:0,width:0,height:0,percent:0,no_upscale:false,max_size_kb:200};
  var m=$('mode').value;
  if(m==='lossless')opts.mode='lossless';
  else if(m==='target'){opts.mode='target';opts.max_size_kb=parseInt($('targetSize').value,10)||200;}
  else{
    opts.mode='quality';
    opts.jpeg_quality=parseInt($('jpegQ').value,10)||90;
    opts.png_quality=parseInt($('pngQ').value,10)||90;
    opts.png_opt_level=Math.max(0,Math.min(6,parseInt($('pngOpt').value,10)||3));
    opts.webp_quality=parseInt($('webpQ').value,10)||90;
    opts.tiff_algorithm=$('tiffAlg').value;
    opts.chroma=$('chroma').value;
    opts.progressive=$('progressive').checked;
    opts.zopfli=$('zopfli').checked;
  }
  opts.keep_exif=$('keepExif').checked;
  opts.keep_dates=$('keepDates').checked;
  opts.format=$('outFormat').value;
  opts.suffix=$('suffix').value.trim();
  var rm=$('resizeMode').value, rv=parseInt($('resizeVal').value,10)||0;
  if(rm==='long_edge')opts.long_edge=rv;
  else if(rm==='width')opts.width=rv;
  else if(rm==='height')opts.height=rv;
  else if(rm==='percent')opts.percent=Math.max(1,Math.min(400,rv));
  opts.no_upscale=$('noUpscale').checked;
  opts.workers=parseInt(($('workers')||{}).value,10)||0;   /* 0=自动；不参与参数指纹 */
  return opts;
}

/* 提交任务。列表很大时（默认 >1200 条）改用「服务端队列引用」：
   把整个列表塞进 POST 体在 1 万条以上约 1MB+，会撞飞牛网关的 client_max_body_size，
   超了直接 413（前端只看到一段 HTML）。改用 use_queue 后请求体只有几百字节，
   服务端从已落盘的队列里取 path/root —— 队列内容与列表同序，是刚才 saveQueue 存的。
   队列没存上（服务端返回 409 queue_empty）时自动退回逐条提交，不让用户卡死。 */
var QUEUE_REF_MIN=1200;
function doCompress(sel,force){
  var out=$('outPath').value.trim();
  var sameFolder=$('sameFolder').checked;
  if(!out&&!sameFolder){setStatus('请先选择输出文件夹',true);return;}
  var opts=collectOptions();
  /* 已存在文件策略：输出到原文件夹时目标就是原图，只能覆盖 */
  opts.exist_policy=sameFolder?'overwrite':($('existPolicy').value||'skip');
  if(sameFolder)opts.overwrite=true;
  var wholeList=(sel===files);
  var useQueue=wholeList&&files.length>=QUEUE_REF_MIN&&files.length<=QUEUE_MAX_FILES;
  var body={
    output:sameFolder?'':out,
    same_folder:sameFolder,
    keep_structure:$('keepStruct').checked,
    force_recompress:!!force,
    options:opts
  };
  function post(payload){
    return fetch('api/compress',{method:'POST',headers:{'Content-Type':'application/json'},
                                 body:JSON.stringify(payload)}).then(jsonSafe);
  }
  function withFiles(){
    body.use_queue=false;
    body.files=sel.map(function(f){return{path:f.path,root:f.root};});
    return post(body);
  }
  /* 队列引用必须先确保队列已落盘；小列表直接发 */
  var pre=useQueue?saveQueue():Promise.resolve();
  pre.then(function(){
    if(!useQueue)return withFiles();
    body.use_queue=true;
    return post(body).then(function(j){
      if(j&&j.task_id)return j;
      return withFiles();          /* 服务端说队列取不到 → 退回逐条提交 */
    });
  })
  .then(function(j){
    if(!j.task_id){setStatus('任务创建失败：'+(j.error||'未知错误'),true);return;}
    taskId=j.task_id;
    sel.forEach(function(f){f.status=null;f.out=null;f.out_size=null;f.note='';});
    setLocked(true);
    render();
    $('barWrap').style.display='block';$('bar').style.width='2%';
    $('cancelLink').classList.remove('hidden');
    setStatus('任务已创建，正在压缩…');
    pollTimer=setInterval(poll,1200);
  })
  .catch(function(e){setStatus('创建任务失败：'+((e&&e.message)||'未知错误'),true);});
}
$('startBtn').onclick=function(){doCompress(files);};
$('retryFailBtn').onclick=function(){
  var fails=files.filter(function(f){return f.status==='err';});
  if(!fails.length){setStatus('没有失败的文件',true);return;}
  setStatus('正在重新压缩 '+fails.length+' 个失败文件…');
  doCompress(fails);
};
/* 全部重压：先清空服务端已完成记录，再全量提交 */
$('recompressAllBtn').onclick=function(){
  if(!files.length)return;
  setStatus('正在清空压缩记录…');
  fetch('api/results/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
  .then(jsonSafe)
  .then(function(){
    files.forEach(function(f){f.status=null;f.out=null;f.out_size=null;f.in_size=null;});
    render();
    doCompress(files,true);
  })
  .catch(function(e){setStatus('清空记录失败：'+e.message,true);});
};

/* 压缩进行中：锁定全部设置（灰），仅「取消」可点 */
function setLocked(on){
  document.body.classList.toggle('locked',on);
  ['addFileBtn','addFolderBtn','removeBtn','clearBtn','retryFailBtn','recompressAllBtn','startBtn',
   'mode','jpegQ','pngQ','pngOpt','webpQ','tiffAlg','chroma','progressive','zopfli','keepExif','targetSize',
   'resizeMode','resizeVal','noUpscale','outPath','browseOutBtn','keepStruct','existPolicy','outFormat',
   'keepDates','sameFolder','suffix','workers']
   .forEach(function(id){var el=$(id);if(el)el.disabled=on;});
  document.querySelectorAll('.tab').forEach(function(t){t.style.pointerEvents=on?'none':'';t.style.opacity=on?'.5':'';});
  if(!on)syncButtons();   // 解锁后按真实状态恢复按钮（含「继续压缩（已完成 N）」文案）
  if(scanId){$('addFileBtn').disabled=true;$('addFolderBtn').disabled=true;}  // 扫描中仍禁用
}

/* 已完成 / 跳过 / 失败的统一口径 */
function effectiveState(s){
  if(s==='ok'||s==='skipped')return 'ok';
  if(s==='copied')return 'copied';
  if(s==='error')return 'err';
  return null;
}

function poll(){
  if(!taskId)return;
  fetch('api/tasks/'+taskId).then(jsonSafe).then(function(j){
    if(j.error){stopPoll();setStatus('查询任务失败：'+j.error,true);return;}
    var pct=j.total?Math.round(100*j.done/j.total):0;
    $('bar').style.width=Math.max(2,pct)+'%';
    var skipped=j.skipped||0, copied=j.copied||0;
    setStatus('进度 '+j.done+'/'+j.total+' · 成功 '+j.ok+
      (copied?' · 已复制 '+copied+'':'')+' · 跳过 '+skipped+' · 失败 '+j.fail);
    var byPath={};
    (j.results||[]).forEach(function(r){byPath[r.path]=r;});
    var changed=[];
    files.forEach(function(f,i){
      var r=byPath[f.path];
      if(r){
        var ns=(r.status==='skipped'?'skip':effectiveState(r.status));
        if(f.status!==ns||f.out!==r.out||f.out_size!==r.out_size){
          f.status=ns;f.out=r.out;f.error=r.error;f.out_size=r.out_size;f.in_size=r.in_size;
          f.note=r.note||'';
          changed.push(i);
        }
      }
    });
    /* 状态变化落在当前页 → 只补那几行；否则整页重建。这样 5000 行的页不会每 1.2s 抖一次 */
    if(changed.length>400)render();
    else if(changed.length){changed.forEach(patchRow);refreshSummary();}
    if(j.state==='done'||j.state==='canceled'||j.state==='error'){
      stopPoll();
      setLocked(false);
      $('cancelLink').classList.add('hidden');
      if(j.state==='canceled')setStatus('任务已取消'+(j.done?'（已完成 '+j.done+' 个，再点「继续压缩」接着压）':''),true);
      else if(j.state==='error')setStatus('任务异常中断：'+(j.error||'未知错误')+'（再点「继续压缩」接着压）',true);
      else{
        var fails=(j.results||[]).filter(function(r){return r.status==='error';});
        var skips=(j.results||[]).filter(function(r){return r.status==='skipped';});
        var copies=(j.results||[]).filter(function(r){return r.status==='copied';});
        var copiedPart=copies.length?('，已复制 '+copies.length+' 个（GIF 不压缩）'):'';
        if(fails.length)setStatus('压缩完成：成功 '+j.ok+copiedPart+'，跳过 '+skips.length+'，失败 '+fails.length+' 个',true);
        else if(skips.length){
          var hasNote=skips.some(function(r){return !!r.note;});
          setStatus('全部处理完成 ✓ 成功 '+j.ok+' 个'+copiedPart+'，跳过 '+(skips.length)+' 个（已存在）'+
            (hasNote?'；想再存一份请点「全部重压」':'')+' 输出目录：'+(j.output||'原文件夹'));
        }
        else if(copies.length)
          setStatus('全部处理完成 ✓ 成功 '+j.ok+' 个'+copiedPart+' ✓ 输出目录：'+(j.output||'原文件夹'));
        else setStatus('全部 '+j.ok+' 个文件压缩完成 ✓ 输出目录：'+(j.output||'原文件夹'));
      }
      if((j.results||[]).length)buildLog(j);
    }
  }).catch(function(e){stopPoll();setStatus('查询任务失败：'+e.message,true);});
}

var lastLog='';
function buildLog(j){
  var lines=['任务 '+j.id+'  ·  输出：'+(j.output||'原文件夹')+'  ·  保留目录结构：'+(j.keep_structure?'是':'否'),
             '完成 '+j.done+'/'+j.total+'  ·  成功 '+j.ok+
             ((j.copied||0)?('  ·  已复制 '+(j.copied||0)):'')+
             '  ·  跳过 '+(j.skipped||0)+'  ·  失败 '+j.fail+'  ·  耗时 '+(j.elapsed||0)+' 秒',''];
  (j.results||[]).forEach(function(r){
    if(r.status==='ok'){
      var rate='';
      if(r.in_size&&r.out_size)rate='  ('+Math.round(100*(1-r.out_size/r.in_size))+'%)';
      lines.push('✓ '+r.path+'  →  '+(r.out||'')+rate);
    }else if(r.status==='skipped'){
      lines.push('⤼ '+r.path+'  →  '+(r.out||'')+(r.note?'  (已跳过：'+r.note+')':'  (已跳过)'));
    }else if(r.status==='copied'){
      lines.push('⤼ '+r.path+'  →  '+(r.out||'')+'  (已复制：GIF 不压缩)');
    }else lines.push('✗ '+r.path+'  '+(r.error||''));
  });
  lastLog=lines.join('\n');
  $('logBody').textContent=lastLog;
  $('logLink').classList.remove('hidden');
}
$('logLink').onclick=function(){$('logMask').classList.add('show');};
$('logClose').onclick=function(){$('logMask').classList.remove('show');};
$('logCloseBtn').onclick=function(){$('logMask').classList.remove('show');};
$('logMask').onclick=function(e){if(e.target===$('logMask'))$('logMask').classList.remove('show');};

$('cancelLink').onclick=function(){
  if(!taskId)return;
  fetch('api/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId})})
  .then(jsonSafe).then(function(j){if(j.ok)setStatus('正在取消…');}).catch(function(){});
};

/* 页面加载时接回仍在跑的任务（闪退/刷新页面后进度不丢，也不再重复压） */
function checkActiveTask(){
  fetch('api/tasks/active').then(jsonSafe).then(function(j){
    var t=j&&j.task;
    if(!t||!t.id)return;
    taskId=t.id;
    var byPath={};
    (t.results||[]).forEach(function(r){byPath[r.path]=r;});
    files.forEach(function(f){
      var r=byPath[f.path];
      if(r){f.status=(r.status==='skipped'?'skip':effectiveState(r.status));f.out=r.out;f.out_size=r.out_size;f.in_size=r.in_size;f.note=r.note||'';}
    });
    setLocked(true);
    render();
    $('barWrap').style.display='block';
    var pct=t.total?Math.round(100*(t.done||0)/t.total):0;
    $('bar').style.width=Math.max(2,pct)+'%';
    $('cancelLink').classList.remove('hidden');
    setStatus('接回进行中的任务：'+(t.done||0)+'/'+t.total);
    stopPoll();
    pollTimer=setInterval(poll,1200);
  }).catch(function(){});
}

fetch('api/fs/roots').then(jsonSafe).then(function(j){
  if(j.roots&&j.roots.length===0)$('permTip').style.display='block';
}).catch(function(){});
syncResize();
updateExistDesc();
loadQueue();
checkActiveTask();
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CaesiumWeb/1.8.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, ctype, max_bytes=0):
        """流式分块发送文件。整文件 f.read() 一次写出的方式会在大图（上百 MB TIFF）
        预览时把进程内存打爆而被 OOM 杀掉 —— 这就是用户说的"闪退"。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            self._send(404, "not found")
            return
        if max_bytes and size > max_bytes:
            self._send(413, "file too large for preview", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(STREAM_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            try:
                self.wfile.flush()
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass  # 响应头已发出，无法再改状态码，只能放弃这次响应

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_json(self, body):
        """读请求体 JSON。只接受顶层是对象的请求：`[]` / `"str"` / `123` 一律当空对象处理，
        否则后续 `req.get(...)` 会抛 AttributeError，客户端看到的是连接被重置（QA C09）。"""
        try:
            obj = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _strip_path(self, raw_path):
        """剥离飞牛网关前缀：/app/caesium/api/xxx → /api/xxx（网关保留前缀，server 自行剥）"""
        if raw_path == GW_PREFIX:
            return "/"
        if raw_path.startswith(GW_PREFIX + "/"):
            return raw_path[len(GW_PREFIX):]
        return raw_path

    def address_string(self):
        # unix socket 无 peer 地址，避免 getfqdn('') 卡住
        return "unix"

    def do_GET(self):
        parsed = urlparse(self.path)
        p = self._strip_path(parsed.path)
        if p in ("/", "/index.html"):
            self._send(200, PAGE)
        elif p.startswith("/images/"):
            img = os.path.basename(p)
            ipath = os.path.join(APP_DIR, "ui", "images", img)
            if os.path.isfile(ipath):
                self._send_file(ipath, "image/png")
            else:
                self._send(404, "not found")
        elif p == "/sdk/trimjs-web-app.js":
            if os.path.isfile(SDK_FILE):
                self._send_file(SDK_FILE, "application/javascript")
            else:
                self._send(404, "sdk not found")
        elif p == "/callback.html":
            self._send(200, CALLBACK_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif p == "/api/status":
            self._json({"ok": True, "clt": os.path.isfile(CLT), "version": "1.8.1",
                        "cpus": os.cpu_count() or 1,
                        "workers": resolve_workers({}, 999999),
                        "platform": "fnOS"})
        elif p == "/api/fs/roots":
            self._json({"roots": fs_roots()})
        elif p == "/api/fs/list":
            qs = parse_qs(parsed.query)
            path = qs.get("path", ["/"])[0] or "/"
            self._json(fs_list(path))
        elif p == "/api/queue":
            # 恢复上次添加的文件列表（持久化到 workdir/queue.json）
            q = load_queue()
            q = [x for x in q if isinstance(x, dict) and x.get("path")]
            self._json({"files": q, "count": len(q)})
        elif p == "/api/results":
            # 已完成压缩记录（断点续传依据）
            recs = get_results()
            self._json({"version": 1, "count": len(recs), "records": recs})
        elif p == "/api/scan/status":
            # 扫描进度 + 分页结果：只返回 [offset, offset+limit) 这一段
            qs = parse_qs(parsed.query)
            scan_id = (qs.get("id", [""])[0] or "").strip()
            with SCAN_LOCK:
                s = SCANS.get(scan_id)
            if not s:
                self._json({"error": "scan not found", "state": "error"}, 404)
                return
            self._json(scan_snapshot(s, qs.get("offset", [0])[0],
                                     qs.get("limit", [SCAN_PAGE_DEFAULT])[0]))
        elif p == "/api/tasks/active":
            # 页面刷新/闪退重进后接回仍在跑的任务
            active = None
            with TASKS_LOCK:
                for t in TASKS.values():
                    if t.get("state") in ("pending", "running"):
                        active = t
                        break
            self._json({"task": task_snapshot(active) if active else None})
        elif p == "/api/img":
            qs = parse_qs(parsed.query)
            path = qs.get("path", [""])[0]
            ext = os.path.splitext(path)[1].lower()
            if ext not in IMAGE_EXTS or not os.path.isfile(path):
                self._send(404, "not found")
                return
            ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                     "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
                     "tif": "image/tiff", "tiff": "image/tiff"}.get(ext.lstrip("."), "application/octet-stream")
            self._send_file(path, ctype, MAX_IMAGE_BYTES)
        elif p.startswith("/api/tasks/"):
            tid = p[len("/api/tasks/"):].strip("/")
            with TASKS_LOCK:
                t = TASKS.get(tid)
            if not t:
                self._json({"error": "task not found"}, 404)
                return
            self._json(task_snapshot(t))
        else:
            self._send(404, "not found")

    def do_POST(self):
        """统一入口：任何未预料的异常都回 400 JSON，而不是把连接直接掐断。

        客户端看到「连接被重置」时只能提示"创建任务失败"，真正的原因（服务端
        traceback）用户根本看不到 —— 所以这里必须兜住（QA C09）。
        """
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:
            try:
                self._json({"error": "bad request: %s: %s" % (type(e).__name__, e)}, 400)
            except Exception:
                self.close_connection = True

    def _handle_post(self):
        parsed = urlparse(self.path)
        p = self._strip_path(parsed.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > MAX_JSON:
            self._json({"error": "payload too large"}, 413)
            return
        body = self.rfile.read(length) if length else b""

        if p == "/api/scan":
            req = self._read_json(body)
            raw_paths = req.get("paths")
            if isinstance(raw_paths, str):
                raw_paths = [raw_paths]        # 单个路径写成字符串：容忍
            elif not isinstance(raw_paths, list):
                raw_paths = []
            paths = [str(x).strip() for x in raw_paths if str(x).strip()]
            errors = [x for x in paths if not os.path.isdir(x) and not os.path.isfile(x)]
            # 立刻返回：扫描在后台线程跑，结果用 GET /api/scan/status 分页取
            # （同步扫盘 → 网关 504 HTML → 前端 "Unexpected token '<'" 的老 bug）
            scan_id = new_scan(paths)
            self._json({"scan_id": scan_id, "state": "running", "errors": errors})
        elif p == "/api/scan/cancel":
            req = self._read_json(body)
            scan_id = str(req.get("scan_id") or "").strip()
            hit = cancel_scan(scan_id)
            self._json({"ok": hit, "scan_id": scan_id,
                        "state": "canceled" if hit else "unknown"})
        elif p == "/api/queue":
            # 保存文件列表（自动持久化，防关闭丢失）。
            # 前端**分批** POST（每批 ≤500 条）：append=true 表示"接着上一批往后拼"，
            # 否则整份替换。这样请求体不会随文件数线性长到撞上网关的
            # client_max_body_size（1MB 左右会被静默 413，队列根本没存上）。
            req = self._read_json(body)
            files = req.get("files")
            if not isinstance(files, list):
                files = []
            clean = []
            for f in files:
                if isinstance(f, dict) and f.get("path"):
                    clean.append({
                        "path": str(f.get("path", "")),
                        "name": str(f.get("name", os.path.basename(str(f.get("path", ""))))),
                        "size": _as_int(f.get("size", 0), 0),
                        "ext": str(f.get("ext", os.path.splitext(str(f.get("path", "")))[1].lower())),
                        "root": str(f.get("root", "") or ""),
                        "w": f.get("w"), "h": f.get("h"),
                    })
            if req.get("append"):
                merged = [x for x in load_queue() if isinstance(x, dict) and x.get("path")]
            else:
                merged = []
            ordered = {}
            for item in merged + clean:
                ordered[item["path"]] = item         # 同一 path 只留最后一次（幂等）
            all_files = list(ordered.values())
            total = len(all_files)
            kept = all_files[-QUEUE_MAX_FILES:]
            saved = save_queue(kept)
            self._json({"ok": saved, "count": total, "kept": len(kept),
                        "truncated": total > len(kept)})
        elif p == "/api/folders/add":
            req = self._read_json(body)
            path = str(req.get("path", "")).strip()
            if not path:
                self._json({"error": "path required"}, 400)
                return
            # 重新添加：从排除列表移除
            save_excluded([x for x in load_excluded() if x != path])
            folders = load_folders()
            if path not in folders and os.path.isdir(path):
                folders.append(path)
                save_folders(folders[-200:])
            self._json({"ok": True})
        elif p == "/api/folders/remove":
            req = self._read_json(body)
            path = str(req.get("path", "")).strip()
            # 加入排除列表（防止探测/官方授权列表再次把它带回来），并从历史删除
            if path:
                excl = load_excluded()
                if path not in excl:
                    excl.append(path)
                    save_excluded(excl[-500:])
            save_folders([x for x in load_folders() if x != path])
            self._json({"ok": True})
        elif p == "/api/compress":
            req = self._read_json(body)
            files = req.get("files")
            if not isinstance(files, list):
                files = []          # files:"abc" / files:3 之类一律按空列表处理
            output = str(req.get("output", "")).strip()
            same_folder = bool(req.get("same_folder", False))
            use_queue = bool(req.get("use_queue", False))
            if use_queue:
                # 大列表（>1 万条）走"队列引用"：请求体只有几百字节，不会撞网关 1MB 上限。
                # 队列内容就是前端 saveQueue 刚存的那份（同序、同 path/root）。
                items = []
                for x in load_queue():
                    if isinstance(x, dict):
                        fp = str(x.get("path", "") or "")
                        if fp:
                            items.append({"path": fp, "root": str(x.get("root", "") or "")})
                if not items:
                    self._json({"error": "queue empty", "code": "queue_empty"}, 409)
                    return
            else:
                if not files or (not output and not same_folder):
                    self._json({"error": "files and output required"}, 400)
                    return
                items = []
                for f in files:
                    if not isinstance(f, dict):
                        continue        # ["a.jpg"] / [123] 这种跳过，不要 AttributeError
                    fp = str(f.get("path", ""))
                    if fp:
                        items.append({"path": fp, "root": str(f.get("root", "") or "")})
                if not items:
                    self._json({"error": "no valid files"}, 400)
                    return
            if not output and not same_folder:
                self._json({"error": "files and output required"}, 400)
                return
            raw_opts = req.get("options")
            opts = dict(raw_opts) if isinstance(raw_opts, dict) else {}
            keep = bool(req.get("keep_structure", True))
            opts["exist_policy"] = normalize_exist_policy(opts)
            force_recompress = bool(req.get("force_recompress", False))
            if same_folder:
                # 输出到原文件夹：目标就是原图，永远"已存在"，只能按覆盖处理
                opts["exist_policy"] = "overwrite"
                opts["overwrite"] = True
                outs = [{"path": it["path"], "out_dir": os.path.dirname(it["path"]), "root": it["root"]} for it in items]
                t = new_task(outs, "", opts, False, force_recompress)
            else:
                t = new_task(items, output, opts, keep, force_recompress)
            threading.Thread(target=run_task, args=(t,), daemon=True).start()
            self._json({"task_id": t["id"], "sig": results_signature(opts),
                        "force_recompress": force_recompress})
        elif p == "/api/results/clear":
            clear_results()
            self._json({"ok": True, "count": 0})
        elif p == "/api/results/status":
            # 按当前参数批量查询哪些文件已经压好了（前端「继续压缩（已完成 N）」用）
            req = self._read_json(body)
            raw_opts = req.get("options")
            opts = raw_opts if isinstance(raw_opts, dict) else {}
            sig = results_signature(opts)
            output = str(req.get("output", "") or "")
            same_folder = bool(req.get("same_folder", False))
            keep = bool(req.get("keep_structure", True))
            req_files = req.get("files")
            if not isinstance(req_files, list):
                req_files = []
            done = {}
            for f in req_files:
                if isinstance(f, dict):
                    fp = str(f.get("path", "") or "")
                    root = str(f.get("root", "") or "")
                else:
                    fp, root = str(f or ""), ""
                if not fp:
                    continue
                item = {"path": fp, "root": root}
                if same_folder:
                    item["out_dir"] = os.path.dirname(fp)
                dst_dir = resolve_dst_dir(item, {"keep_structure": keep}, output)
                rec = result_is_done(fp, sig, dst_dir)
                if rec:
                    done[fp] = rec
            self._json({"ok": True, "sig": sig, "count": len(done), "done": done})
        elif p == "/api/cancel":
            req = self._read_json(body)
            tid = str(req.get("task_id", ""))
            with TASKS_LOCK:
                t = TASKS.get(tid)
                if t and t["state"] in ("pending", "running"):
                    t["cancel"] = True
                    killed = cancel_task_processes(tid)   # P1-2：立即终止该任务在途引擎
                    self._json({"ok": True, "killed": killed})
                    return
            self._json({"ok": False, "error": "task not found or finished"})
        else:
            self._send(404, "not found")


def main():
    global FOLDERS_FILE, PREVIEW_DIR, QUEUE_FILE, EXCLUDED_FILE, RESULTS_FILE
    ap = argparse.ArgumentParser(description="Caesium for fnOS")
    ap.add_argument("--port", type=int, default=8390)
    ap.add_argument("--socket", default="", help="unix socket 路径（飞牛网关 micro_app 模式，优先于 --port）")
    ap.add_argument("--workdir", default=os.path.join(APP_DIR, "var"))
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    FOLDERS_FILE = os.path.join(args.workdir, "folders.json")
    QUEUE_FILE = os.path.join(args.workdir, "queue.json")
    EXCLUDED_FILE = os.path.join(args.workdir, "excluded.json")
    RESULTS_FILE = os.path.join(args.workdir, "results.json")
    PREVIEW_DIR = os.path.join(args.workdir, "preview")
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    load_results()  # 断点续传：启动时恢复逐文件压缩记录
    if args.socket:
        if hasattr(socket, "AF_UNIX"):
            if os.path.exists(args.socket):
                os.unlink(args.socket)
            os.makedirs(os.path.dirname(args.socket) or ".", exist_ok=True)

            class UnixServer(ThreadingHTTPServer):
                address_family = socket.AF_UNIX

                def server_bind(self):
                    self.socket = socket.socket(self.address_family, socket.SOCK_STREAM)
                    self.socket.bind(self.server_address)
                    self.socket.listen(self.request_queue_size)
                    self.server_name = "unix"
                    self.server_port = 0

            srv = UnixServer(args.socket, Handler)
        else:
            srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
            args.socket = ""  # 平台不支持时回退 TCP
    else:
        srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    srv.daemon_threads = True
    listen_desc = "unix:%s" % args.socket if args.socket else "0.0.0.0:%d" % args.port
    print("Caesium for fnOS 1.8.1 (multi-thread) listening on %s (engine=%s)" % (listen_desc, CLT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
