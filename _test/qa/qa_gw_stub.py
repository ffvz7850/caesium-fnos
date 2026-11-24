#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「飞牛统一网关」桩：在 server.py 前面加一层反向代理，复现线上真实故障。

真实行为（用户报的）：网关等不到上游响应 → 回一张 **nginx 504 HTML 错误页**，
前端 `r.json()` 抛 `Unexpected token '<', "<html> <h"...`；而服务端那个线程
并不会因为网关超时而停下，继续走盘。

本桩：
  * 把请求转发给上游 server.py；
  * 对超时名单里的路径（默认 /api/scan）只等 `timeout` 秒，超时立刻回 504 HTML，
    **不再等上游**（上游继续跑，与真实网关一致）；
  * 记录每条请求的 (路径, 状态, 耗时)，供断言使用。

单独跑：python qa_gw_stub.py --listen 9100 --upstream 8390 --timeout 1.0
"""
import argparse
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 真实 nginx 的 504 页面（与线上返回的 "<html> <h..." 前缀一致）
NGINX_504 = (
    "<html>\r\n"
    "<head><title>504 Gateway Time-out</title></head>\r\n"
    "<body>\r\n"
    "<center><h1>504 Gateway Time-out</h1></center>\r\n"
    "<hr><center>nginx</center>\r\n"
    "</body>\r\n"
    "</html>\r\n"
)

STATE = {"upstream": "127.0.0.1:8390", "timeout": 1.0,
         "slow_paths": ("/api/scan",), "force_paths": (), "log": [], "lock": threading.Lock()}


def log(entry):
    with STATE["lock"]:
        STATE["log"].append(entry)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _proxy(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = b""
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        if n:
            body = self.rfile.read(n)
        target = "http://%s%s" % (STATE["upstream"], self.path)
        if any(p in path for p in STATE["force_paths"]):
            # 强制网关超时（不转发）：用于稳定复现"上游其实跑成功了，但前端只收到 504 HTML"
            log({"path": path, "method": self.command, "status": 504, "elapsed": 0.0,
                 "timeout": True, "note": "forced"})
            self._send(504, NGINX_504.encode("utf-8"), "text/html")
            return
        slow = any(path.endswith(p) or p in path for p in STATE["slow_paths"])
        limit = STATE["timeout"] if slow else 30.0
        started = time.time()
        try:
            req = urllib.request.Request(target, data=body if body or self.command != "GET" else None,
                                         method=self.command)
            ctype = self.headers.get("Content-Type")
            if ctype:
                req.add_header("Content-Type", ctype)
            with urllib.request.urlopen(req, timeout=limit) as r:
                payload = r.read()
                code = r.status
                rct = r.headers.get("Content-Type", "application/json; charset=utf-8")
        except urllib.error.HTTPError as e:
            payload, code = e.read(), e.code
            rct = e.headers.get("Content-Type", "text/html")
        except (socket.timeout, TimeoutError, urllib.error.URLError) as e:
            # 网关等不到：立刻回 HTML 504（上游仍在继续）
            payload = NGINX_504.encode("utf-8")
            code = 504
            rct = "text/html"
            log({"path": path, "method": self.command, "status": 504,
                 "elapsed": round(time.time() - started, 3), "timeout": True,
                 "note": str(e)[:80]})
            self._send(code, payload, rct)
            return
        log({"path": path, "method": self.command, "status": code,
             "elapsed": round(time.time() - started, 3), "timeout": False})
        self._send(code, payload, rct)

    def _send(self, code, payload, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:
            pass

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()


def start(upstream, timeout=1.0, slow_paths=("/api/scan",), port=0, force_paths=()):
    """在后台起网关，返回 (port, srv)。port=0 自动选空闲端口。"""
    STATE["upstream"] = upstream
    STATE["timeout"] = timeout
    STATE["slow_paths"] = tuple(slow_paths)
    STATE["force_paths"] = tuple(force_paths)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv.server_address[1], srv


def requests():
    with STATE["lock"]:
        return list(STATE["log"])


def clear():
    with STATE["lock"]:
        STATE["log"] = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=9100)
    ap.add_argument("--upstream", default="127.0.0.1:8390")
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--force-504", default="", help="这些路径一律立刻回 504 HTML（不转发）")
    a = ap.parse_args()
    force = tuple(x for x in a.force_504.split(",") if x)
    port, srv = start(a.upstream, a.timeout, port=a.listen, force_paths=force)
    print("gateway on 127.0.0.1:%d -> %s (timeout=%.1fs)" % (port, a.upstream, a.timeout), flush=True)
    try:
        while True:
            time.sleep(1)
            for r in requests():
                print(json.dumps(r, ensure_ascii=False), flush=True)
            clear()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
