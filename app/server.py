#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Caesium for fnOS — 一比一复刻原版界面的图片压缩工具（v1.7.0）
左侧文件列表(名称/分辨率/大小/已节省/状态) + 右侧三标签(压缩选项/图片尺寸/输出)
压缩选项：JPEG质量 / PNG优化级别 / WebP质量 / TIFF压缩算法，JPEG色度二次采样+渐进式，PNG Zopfli
"""
import argparse
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CLT = os.environ.get("CAESIUM_CLT") or os.path.join(APP_DIR, "caesiumclt")
MAX_JSON = 8 * 1024 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
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
def get_image_size(path):
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
            with open(path, "rb") as f:
                data = f.read()
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


# ---------------------------------------------------------------- 任务管理
TASKS = {}
TASKS_LOCK = threading.Lock()


def new_task(files, output, options, keep_structure):
    tid = uuid.uuid4().hex[:12]
    t = {"id": tid, "state": "pending", "files": files, "output": output,
         "options": options, "keep_structure": keep_structure,
         "total": len(files), "done": 0, "ok": 0, "fail": 0,
         "current": None, "started": None, "finished": None, "cancel": False, "results": []}
    with TASKS_LOCK:
        TASKS[tid] = t
    return t


def build_clt_cmd(src, dst_dir, options, out_format):
    """按最终输出格式组装 caesiumclt 命令（每个格式独立质量设置）。返回 (cmd, out_ext)"""
    cmd = [CLT]
    mode = str(options.get("mode", "quality"))
    lossless = bool(options.get("lossless")) or mode == "lossless"
    q_jpeg = max(1, min(100, int(options.get("jpeg_quality", 80) or 80)))
    q_webp = max(1, min(100, int(options.get("webp_quality", 80) or 80)))
    q_png = max(1, min(100, int(options.get("png_quality", 90) or 90)))
    opt_png = max(0, min(6, int(options.get("png_opt_level", 3) or 3)))

    # 目标格式
    fmt = str(options.get("format", "keep")).lower()
    if fmt not in ("keep", "jpeg", "png", "webp", "tiff"):
        fmt = "keep"
    if fmt == "keep":
        fmt2 = out_format.lstrip(".").lower()  # 按源文件扩展名决定
        if fmt2 not in ("jpeg", "png", "webp", "tiff", "gif"):
            fmt2 = "jpeg"  # bmp/avif 等未覆盖格式按 JPEG 处理
    else:
        fmt2 = fmt
    out_ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "tiff": ".tiff"}.get(fmt2, out_format)

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
    cmd += ["-o", dst_dir, "-O", "all" if options.get("overwrite") else "never", src]
    return cmd, out_ext


def compress_file(src, dst_dir, options, out_format):
    os.makedirs(dst_dir, exist_ok=True)
    try:
        before = set(os.listdir(dst_dir))
    except Exception:
        before = set()
    cmd, out_ext = build_clt_cmd(src, dst_dir, options, out_format)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except Exception as e:
        return False, None, "caesiumclt error: %s" % e
    base = os.path.splitext(os.path.basename(src))[0]
    suffix = str(options.get("suffix", "") or "")
    out_path = os.path.join(dst_dir, base + suffix + out_ext)
    if os.path.isfile(out_path):
        return True, out_path, ""
    try:
        after = set(os.listdir(dst_dir))
        new = sorted(after - before)
        if new:
            return True, os.path.join(dst_dir, new[0]), ""
    except Exception:
        pass
    err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return False, None, (err[:500] if err else "no output file")


def run_task(t):
    t["state"] = "running"
    t["started"] = time.time()
    output = t["output"]
    if output:
        os.makedirs(output, exist_ok=True)
    for item in t["files"]:
        if t["cancel"]:
            t["state"] = "canceled"
            t["finished"] = time.time()
            return
        src = item.get("path", "")
        if not src or not os.path.isfile(src):
            t["done"] += 1
            t["fail"] += 1
            t["results"].append({"path": src, "status": "error", "error": "file not found"})
            continue
        t["current"] = src
        out_dir = item.get("out_dir")
        if out_dir:
            dst_dir = out_dir
        elif t["keep_structure"]:
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
            dst_dir = os.path.join(output, rel)
            dst_dir = os.path.dirname(dst_dir)
        else:
            dst_dir = output
        out_format = os.path.splitext(src)[1].lower()
        ok, out_path, err = compress_file(src, dst_dir, t["options"], out_format)
        t["done"] += 1
        if ok and out_path:
            t["ok"] += 1
            try:
                t["results"].append({"path": src, "out": out_path, "status": "ok",
                                     "in_size": os.path.getsize(src), "out_size": os.path.getsize(out_path)})
            except Exception:
                t["results"].append({"path": src, "out": out_path, "status": "ok"})
        else:
            t["fail"] += 1
            t["results"].append({"path": src, "status": "error", "error": err or "compress failed"})
    t["state"] = "done"
    t["finished"] = time.time()
    t["current"] = None


# ---------------------------------------------------------------- 文件夹管理
FOLDERS_FILE = None
QUEUE_FILE = None
EXCLUDED_FILE = None


def load_queue():
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_queue(files):
    try:
        os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(files, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


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


def scan_images(paths):
    out, seen = [], set()

    def walk(root, d, depth):
        if depth > 12:
            return
        try:
            names = os.listdir(d)
        except Exception:
            return
        for n in sorted(names, key=lambda x: x.lower()):
            p = os.path.join(d, n)
            try:
                if os.path.isdir(p):
                    walk(root, p, depth + 1)
                else:
                    ext = os.path.splitext(n)[1].lower()
                    if ext in IMAGE_EXTS and p not in seen:
                        seen.add(p)
                        size = os.path.getsize(p)
                        wh = get_image_size(p)
                        out.append({"path": p, "name": n, "size": size, "ext": ext,
                                    "root": root, "w": wh[0] if wh else None, "h": wh[1] if wh else None})
            except Exception:
                pass

    for p in paths:
        if not p:
            continue
        if os.path.isdir(p):
            walk(p, p, 0)
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in IMAGE_EXTS:
            if p not in seen:
                seen.add(p)
                size = os.path.getsize(p)
                wh = get_image_size(p)
                out.append({"path": p, "name": os.path.basename(p), "size": size,
                            "ext": os.path.splitext(p)[1].lower(), "root": os.path.dirname(p),
                            "w": wh[0] if wh else None, "h": wh[1] if wh else None})
    return out


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
.cb{width:15px;height:15px;accent-color:var(--c)}
.st-ok{color:var(--ok);font-weight:600}
.st-err{color:var(--err)}
.st-wait{color:var(--mut)}
.st-run{color:var(--warn)}
.save{color:var(--ok)}
.empty{padding:30px 20px;text-align:center;color:var(--mut);font-size:12.5px}
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

<script>
(function(){
var $=function(id){return document.getElementById(id);};
var files=[]; var taskId=null, pollTimer=null;
var browserMode='folder', browserPick=null, curBrowsePath='';
var myFolders=[];
var ENC=encodeURIComponent;

function fmt(n){if(n==null)return'—';if(n<1024)return n+' B';if(n<1048576)return(n/1024).toFixed(1)+' KB';return(n/1048576).toFixed(2)+' MB';}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

fetch('api/status').then(function(r){return r.json();}).then(function(j){
  if(j.clt){$('engineDot').classList.remove('off');$('engineTxt').textContent='压缩引擎正常';}
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
function render(){
  var tb=$('tbody'); tb.innerHTML='';
  var n=files.length, sum=0;
  files.forEach(function(f,i){
    sum+=f.size;
    var tr=document.createElement('tr');
    var st='<span class="st-wait">等待</span>';
    if(f.status==='ok')st='<span class="st-ok">成功</span>';
    else if(f.status==='err')st='<span class="st-err">失败</span>';
    else if(f.status==='run')st='<span class="st-run">压缩中</span>';
    var res=(f.w&&f.h)?(f.w+'×'+f.h):'—';
    var save='—';
    if(f.out_size!=null&&f.in_size!=null){
      var pct=Math.round(100*(1-f.out_size/f.in_size));
      save='<span class="save">−'+pct+'%</span>';
      if(pct<0)save='<span class="bad" style="color:var(--warn)">+'+(-pct)+'%</span>';
    }
    tr.innerHTML='<td><input type="checkbox" class="cb"></td>'+
      '<td><span class="p" title="'+esc(f.path)+'">'+esc(f.name)+'</span></td>'+
      '<td>'+res+'</td><td class="num">'+fmt(f.size)+'</td>'+
      '<td class="num">'+save+'</td><td>'+st+'</td>';
    var cb=tr.querySelector('.cb');
    cb.checked=f.sel;
    cb.onchange=function(){f.sel=this.checked;tr.classList.toggle('sel',this.checked);};
    tr.onclick=function(ev){if(ev.target.tagName==='INPUT')return;selIdx=i;showPreview(f);};
    if(i===selIdx)tr.classList.add('sel');
    tb.appendChild(tr);
  });
  $('count').textContent=n+' 个项目';
  $('sumSize').textContent=n?('合计 '+fmt(sum)):'';
  $('empty').classList.toggle('hidden',n>0);
  $('tbl').classList.toggle('hidden',n===0);
  $('startBtn').disabled=n===0||!$('outPath').value;
  $('retryFailBtn').disabled=!files.some(function(f){return f.status==='err';});
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
$('removeBtn').onclick=function(){files=files.filter(function(f){return !f.sel;});selIdx=-1;render();saveQueue();};
$('clearBtn').onclick=function(){if(files.length&&!confirm('清空全部 '+files.length+' 个文件？'))return;files=[];selIdx=-1;render();saveQueue();};

/* 文件列表自动持久化：添加/移除/清空时保存，打开页面时恢复 */
function saveQueue(){
  var data=files.map(function(f){return{path:f.path,name:f.name,size:f.size,ext:f.ext,root:f.root,w:f.w,h:f.h};});
  fetch('api/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:data})})
  .catch(function(){});
}
function loadQueue(){
  fetch('api/queue').then(function(r){return r.json();}).then(function(j){
    if(j.files&&j.files.length){
      files=(j.files||[]).map(function(f){
        return{path:f.path,name:f.name,size:f.size,ext:f.ext,root:f.root,w:f.w,h:f.h,sel:false,status:null};
      });
      render();
      setStatus('已恢复上次添加的 '+files.length+' 个文件');
    }
  }).catch(function(){});
}

function addPaths(paths,isDir){
  setStatus('正在扫描图片…');
  fetch('api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths:paths})})
  .then(function(r){return r.json();})
  .then(function(j){
    var add=0,dup=0;
    (j.files||[]).forEach(function(f){
      if(files.some(function(x){return x.path===f.path;})){dup++;return;}
      files.push({path:f.path,name:f.name,size:f.size,ext:f.ext,root:f.root,sel:false,status:null,w:f.w,h:f.h});
      add++;
    });
    render();
    saveQueue();
    setStatus('添加 '+add+' 个文件'+(dup?'，跳过 '+dup+' 个重复':''));
  })
  .catch(function(e){setStatus('扫描失败：'+e.message,true);});
}

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
  if(f.out&&f.status==='ok'){
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
  fetch('api/fs/roots').then(function(r){return r.json();}).then(function(j){
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
  fetch('api/fs/roots').then(function(r){return r.json();}).then(function(j){
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
  fetch('api/fs/list?path='+ENC(path)).then(function(r){return r.json();}).then(function(j){
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
$('sameFolder').onchange=function(){
  if(this.checked){$('outPath').value='';$('browseOutBtn').disabled=true;alert('输出到原文件夹会覆盖原图，请谨慎使用');}
  else{$('browseOutBtn').disabled=false;$('startBtn').disabled=files.length===0;}
};

/* ---------- 任务 ---------- */
function setStatus(t,isErr){var s=$('statusTxt');s.textContent=t;s.classList.toggle('err',!!isErr);}
function stopPoll(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}

function doCompress(sel){
  var out=$('outPath').value.trim();
  var sameFolder=$('sameFolder').checked;
  if(!out&&!sameFolder){setStatus('请先选择输出文件夹',true);return;}
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
  if(sameFolder)opts.overwrite=true;
  var body={
    files:sel.map(function(f){return{path:f.path,root:f.root};}),
    output:sameFolder?'':out,
    same_folder:sameFolder,
    keep_structure:$('keepStruct').checked,
    options:opts
  };
  fetch('api/compress',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(function(r){return r.json();})
  .then(function(j){
    if(!j.task_id){setStatus('任务创建失败：'+(j.error||'未知错误'),true);return;}
    taskId=j.task_id;
    sel.forEach(function(f){f.status=null;f.out=null;f.out_size=null;});
    setLocked(true);
    render();
    $('barWrap').style.display='block';$('bar').style.width='2%';
    $('cancelLink').classList.remove('hidden');
    setStatus('任务已创建，正在压缩…');
    pollTimer=setInterval(poll,1200);
  })
  .catch(function(e){setStatus('创建任务失败：'+e.message,true);});
}
$('startBtn').onclick=function(){doCompress(files);};
$('retryFailBtn').onclick=function(){
  var fails=files.filter(function(f){return f.status==='err';});
  if(!fails.length){setStatus('没有失败的文件',true);return;}
  setStatus('正在重新压缩 '+fails.length+' 个失败文件…');
  doCompress(fails);
};

/* 压缩进行中：锁定全部设置（灰），仅「取消」可点 */
function setLocked(on){
  document.body.classList.toggle('locked',on);
  ['addFileBtn','addFolderBtn','removeBtn','clearBtn','retryFailBtn','startBtn','mode','jpegQ','pngQ','pngOpt','webpQ',
   'tiffAlg','chroma','progressive','zopfli','keepExif','targetSize','resizeMode','resizeVal','noUpscale',
   'outPath','browseOutBtn','keepStruct','outFormat','keepDates','sameFolder','suffix']
   .forEach(function(id){var el=$(id);if(el)el.disabled=on;});
  document.querySelectorAll('.tab').forEach(function(t){t.style.pointerEvents=on?'none':'';t.style.opacity=on?'.5':'';});
}

function poll(){
  if(!taskId)return;
  fetch('api/tasks/'+taskId).then(function(r){return r.json();}).then(function(j){
    if(j.error){stopPoll();setStatus('查询任务失败：'+j.error,true);return;}
    var pct=j.total?Math.round(100*j.done/j.total):0;
    $('bar').style.width=Math.max(2,pct)+'%';
    setStatus('进度 '+j.done+'/'+j.total+' · 成功 '+j.ok+' · 失败 '+j.fail);
    var byPath={};
    (j.results||[]).forEach(function(r){byPath[r.path]=r;});
    files.forEach(function(f){
      var r=byPath[f.path];
      if(r){f.status=r.status==='ok'?'ok':'err';f.out=r.out;f.error=r.error;f.out_size=r.out_size;f.in_size=r.in_size;}
    });
    render();
    if(j.state==='done'||j.state==='canceled'){
      stopPoll();
      setLocked(false);
      $('cancelLink').classList.add('hidden');
      if(j.state==='canceled')setStatus('任务已取消',true);
      else{
        var fails=(j.results||[]).filter(function(r){return r.status!=='ok';});
        if(fails.length)setStatus('压缩完成：成功 '+j.ok+'，失败 '+fails.length+' 个',true);
        else setStatus('全部 '+j.ok+' 个文件压缩完成 ✓ 输出目录：'+(j.output||'原文件夹'));
      }
      if((j.results||[]).length)buildLog(j);
    }
  }).catch(function(e){stopPoll();setStatus('查询任务失败：'+e.message,true);});
}

var lastLog='';
function buildLog(j){
  var lines=['任务 '+j.id+'  ·  输出：'+(j.output||'原文件夹')+'  ·  保留目录结构：'+(j.keep_structure?'是':'否'),
             '完成 '+j.done+'/'+j.total+'  ·  成功 '+j.ok+'  ·  失败 '+j.fail+'  ·  耗时 '+(j.elapsed||0)+' 秒',''];
  (j.results||[]).forEach(function(r){
    if(r.status==='ok'){
      var rate='';
      if(r.in_size&&r.out_size)rate='  ('+Math.round(100*(1-r.out_size/r.in_size))+'%)';
      lines.push('✓ '+r.path+'  →  '+(r.out||'')+rate);
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
  .then(function(r){return r.json();}).then(function(j){if(j.ok)setStatus('正在取消…');}).catch(function(){});
};

fetch('api/fs/roots').then(function(r){return r.json();}).then(function(j){
  if(j.roots&&j.roots.length===0)$('permTip').style.display='block';
}).catch(function(){});
syncResize();
loadQueue();
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "CaesiumWeb/1.7.0"

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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_json(self, body):
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return {}

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
                with open(ipath, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, "not found")
        elif p == "/sdk/trimjs-web-app.js":
            if os.path.isfile(SDK_FILE):
                with open(SDK_FILE, "rb") as f:
                    self._send(200, f.read(), "application/javascript")
            else:
                self._send(404, "sdk not found")
        elif p == "/callback.html":
            self._send(200, CALLBACK_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif p == "/api/status":
            self._json({"ok": True, "clt": os.path.isfile(CLT), "version": "1.7.0"})
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
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        elif p.startswith("/api/tasks/"):
            tid = p[len("/api/tasks/"):].strip("/")
            with TASKS_LOCK:
                t = TASKS.get(tid)
            if not t:
                self._json({"error": "task not found"}, 404)
                return
            elapsed = None
            if t["started"]:
                elapsed = int((t["finished"] or time.time()) - t["started"])
            self._json({"id": t["id"], "state": t["state"], "total": t["total"],
                        "done": t["done"], "ok": t["ok"], "fail": t["fail"],
                        "current": t["current"], "output": t["output"],
                        "keep_structure": t["keep_structure"],
                        "elapsed": elapsed, "results": t["results"]})
        else:
            self._send(404, "not found")

    def do_POST(self):
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
            paths = [str(x).strip() for x in req.get("paths", []) if str(x).strip()]
            found = scan_images(paths)
            errors = [x for x in paths if not os.path.isdir(x)]
            self._json({"files": found, "count": len(found), "errors": errors})
        elif p == "/api/queue":
            # 保存文件列表（自动持久化，防关闭丢失）
            req = self._read_json(body)
            files = req.get("files", [])
            clean = []
            for f in files:
                if isinstance(f, dict) and f.get("path"):
                    clean.append({
                        "path": str(f.get("path", "")),
                        "name": str(f.get("name", os.path.basename(str(f.get("path", ""))))),
                        "size": int(f.get("size", 0) or 0),
                        "ext": str(f.get("ext", os.path.splitext(str(f.get("path", "")))[1].lower())),
                        "root": str(f.get("root", "") or ""),
                        "w": f.get("w"), "h": f.get("h"),
                    })
            save_queue(clean[-2000:])
            self._json({"ok": True, "count": len(clean)})
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
            files = req.get("files", [])
            output = str(req.get("output", "")).strip()
            same_folder = bool(req.get("same_folder", False))
            if not files or (not output and not same_folder):
                self._json({"error": "files and output required"}, 400)
                return
            opts = req.get("options", {}) or {}
            keep = bool(req.get("keep_structure", True))
            items = []
            for f in files:
                fp = str(f.get("path", ""))
                if fp:
                    items.append({"path": fp, "root": str(f.get("root", "") or "")})
            if not items:
                self._json({"error": "no valid files"}, 400)
                return
            if same_folder:
                outs = [{"path": it["path"], "out_dir": os.path.dirname(it["path"]), "root": it["root"]} for it in items]
                t = new_task(outs, "", opts, False)
            else:
                t = new_task(items, output, opts, keep)
            threading.Thread(target=run_task, args=(t,), daemon=True).start()
            self._json({"task_id": t["id"]})
        elif p == "/api/cancel":
            req = self._read_json(body)
            tid = str(req.get("task_id", ""))
            with TASKS_LOCK:
                t = TASKS.get(tid)
                if t and t["state"] in ("pending", "running"):
                    t["cancel"] = True
                    self._json({"ok": True})
                    return
            self._json({"ok": False, "error": "task not found or finished"})
        else:
            self._send(404, "not found")


def main():
    global FOLDERS_FILE, PREVIEW_DIR, QUEUE_FILE, EXCLUDED_FILE
    ap = argparse.ArgumentParser(description="Caesium for fnOS")
    ap.add_argument("--port", type=int, default=8390)
    ap.add_argument("--socket", default="", help="unix socket 路径（飞牛网关 micro_app 模式，优先于 --port）")
    ap.add_argument("--workdir", default=os.path.join(APP_DIR, "var"))
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    FOLDERS_FILE = os.path.join(args.workdir, "folders.json")
    QUEUE_FILE = os.path.join(args.workdir, "queue.json")
    EXCLUDED_FILE = os.path.join(args.workdir, "excluded.json")
    PREVIEW_DIR = os.path.join(args.workdir, "preview")
    os.makedirs(PREVIEW_DIR, exist_ok=True)
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
    print("Caesium for fnOS 1.7.0 listening on %s (engine=%s)" % (listen_desc, CLT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
