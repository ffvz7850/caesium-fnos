"""Regression tests for the fixed PC 1.9.1 server (isolated temp dirs)."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer

SERVER = Path(__file__).resolve().parent / "app" / "server.py"
spec = importlib.util.spec_from_file_location("caesium_server_fix", SERVER)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

TOKEN = "testtoken123"
m.AUTH_TOKEN = TOKEN
tmp = Path(tempfile.mkdtemp(prefix="caesium-191-test-"))
m.RESULTS_FILE = str(tmp / "results.json")
m.FOLDERS_FILE = str(tmp / "folders.json")
m.QUEUE_FILE = str(tmp / "queue.json")
m.EXCLUDED_FILE = str(tmp / "excluded.json")
m.PREVIEW_DIR = str(tmp / "preview")
os.makedirs(m.PREVIEW_DIR, exist_ok=True)

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("[PASS]", name, detail)
    else:
        fail += 1
        print("[FAIL]", name, detail)


srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = dict(headers or {})
    if method == "POST" and body is not None:
        h.setdefault("Content-Type", "application/json")
        h.setdefault("Content-Length", str(len(body)))
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


base = {"X-Caesium-Token": TOKEN}

# --- auth ---
st, _ = req("GET", "/api/status")
check("GET without token rejected", st == 403, str(st))
st, data = req("GET", "/api/status", headers=base)
check("GET with token accepted", st == 200 and json.loads(data)["version"] == "1.9.1",
      data.decode()[:100])
st, _ = req("GET", "/api/status", headers={**base, "Host": "evil.example"})
check("bad Host rejected", st == 403, str(st))
st, _ = req("GET", "/api/status", headers={**base, "Origin": "https://evil.example"})
check("bad Origin rejected", st == 403, str(st))
st, _ = req("POST", "/api/results/clear", b"{}",
            headers={**base, "Content-Type": "text/plain"})
check("CSRF text/plain rejected", st == 415, str(st))
st, data = req("GET", "/")
check("page served and token injected", st == 200 and TOKEN.encode() in data and b"__CAESIUM_TOKEN__" not in data)

# --- /api/open ---
f = tmp / "payload.exe"
f.write_bytes(b"MZ")
st, _ = req("POST", "/api/open", json.dumps({"path": str(f)}).encode(), headers=base)
check("non-directory open rejected", st == 400, str(st))
outside = tmp / "outside"
outside.mkdir()
st, _ = req("POST", "/api/open", json.dumps({"path": str(outside)}).encode(), headers=base)
check("unwhitelisted directory rejected", st == 403, str(st))

# --- workers fallback ---
os.environ["CAESIUM_WORKERS"] = "3"
check("workers=0 follows env", m.resolve_workers({"workers": 0}, 100) == 3,
      str(m.resolve_workers({"workers": 0}, 100)))
check("workers='0' follows env", m.resolve_workers({"workers": "0"}, 100) == 3)
del os.environ["CAESIUM_WORKERS"]

# --- VP8X / VP8L ---
p = tmp / "vp8x.webp"
p.write_bytes(b"RIFF" + struct.pack("<I", 100) + b"WEBPVP8X" + struct.pack("<I", 10)
              + b"\x00" * 4
              + bytes([399 & 255, (399 >> 8) & 255, 0, 299 & 255, (299 >> 8) & 255, 0])
              + b"\x00" * 4)
check("VP8X +1", m.get_image_size(str(p)) == (400, 300), str(m.get_image_size(str(p))))
p2 = tmp / "vp8l.webp"
bits = (400 - 1) | ((300 - 1) << 14)
p2.write_bytes(b"RIFF" + struct.pack("<I", 100) + b"WEBPVP8L" + struct.pack("<I", 10)
               + b"\x2f" + struct.pack("<I", bits) + b"\x00" * 4)
check("VP8L decoded", m.get_image_size(str(p2)) == (400, 300), str(m.get_image_size(str(p2))))

# --- flush race ---
def worker(k):
    for i in range(200):
        m.record_result("p%d-%d" % (k, i), {"ok": True, "n": i})


threads = [threading.Thread(target=worker, args=(k,)) for k in range(16)]
for t0 in threads:
    t0.start()
for t0 in threads:
    t0.join()
m.flush_results(force=True)
disk = json.loads(Path(m.RESULTS_FILE).read_text(encoding="utf-8"))
check("3200 concurrent records persisted", len(disk["records"]) == 3200, str(len(disk["records"])))

# --- queue expect_count ---
st, data = req("POST", "/api/queue", json.dumps({"files": [
    {"path": str(tmp / "a.jpg"), "name": "a.jpg"}]}).encode(), headers=base)
check("queue save ok", st == 200, data.decode()[:80])
st, data = req("POST", "/api/compress", json.dumps({
    "use_queue": True, "expect_count": 99, "output": str(tmp / "out")}).encode(), headers=base)
check("queue mismatch rejected", st == 409 and b"queue_mismatch" in data, data.decode()[:120])

# --- single flight ---
fake = {"id": "fake", "state": "running"}
m.TASKS["fake"] = fake
st, data = req("POST", "/api/compress", json.dumps({"files": [
    {"path": str(tmp / "a.jpg")}], "output": str(tmp / "out")}).encode(), headers=base)
check("busy task rejected", st == 409 and b"task_busy" in data, data.decode()[:120])
m.TASKS.pop("fake", None)

srv.shutdown()
srv.server_close()
print("\nTOTAL", ok, "passed,", fail, "failed")
print("TMP", tmp)
sys.exit(1 if fail else 0)
