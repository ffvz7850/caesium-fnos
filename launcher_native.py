#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Caesium PC 1.9.1 原生窗口启动器（单 exe onefile）。

修复点（相对 1.9.0）：
  * 资源释放戳纳入 server.py 与 ui 内容 hash，升级后不会再跑旧代码；
  * 本地 API 使用一次性 token + 实例文件校验，不复用冒名/无关服务；
  * 启动失败、依赖缺失、资源释放失败都有可见提示；
  * 端口检查使用独占绑定，减少抢占窗口。
"""
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.request
import uuid

APP_NAME = "Caesium PC"
APP_VERSION = "1.9.1"
PORT_DEFAULT = 43117
PORT_SCAN = 40
SERVER_ERROR = []
TOKEN = ""
DATA_DIR = ""
INSTANCE_PATH = ""


def log(msg):
    try:
        os.makedirs(DATA_DIR or os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CaesiumPC"), exist_ok=True)
        with open(os.path.join(DATA_DIR or os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CaesiumPC"), "launcher.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass
    print(msg, flush=True)


def message_box(title, text):
    """无控制台的 onefile 程序必须有可见错误提示，不静默退出。"""
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0x10)
            return
    except Exception:
        pass
    log("%s: %s" % (title, text))


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def bundle_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def exe_dir():
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def data_dir():
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    p = os.path.join(root, "CaesiumPC")
    os.makedirs(p, exist_ok=True)
    return p


def app_dir():
    cand = os.path.join(bundle_dir(), "app")
    if os.path.isfile(os.path.join(cand, "server.py")):
        return cand
    src = os.path.join(os.path.dirname(exe_dir()), "caesium", "app")
    if os.path.isfile(os.path.join(src, "server.py")):
        return src
    return cand


def load_token(root):
    """每台机器每份数据目录一个随机 token；不落明文到 exe 旁。"""
    global TOKEN
    path = os.path.join(root, "token")
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if token and len(token) >= 16:
            TOKEN = token
            return TOKEN
    except Exception:
        pass
    TOKEN = uuid.uuid4().hex + uuid.uuid4().hex
    tmp = path + ".tmp-" + uuid.uuid4().hex
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(TOKEN)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    return TOKEN


def resource_sig(src):
    """server.py / caesium.dll / ui 全量签名；只认 DLL 会导致升级失效。"""
    h = hashlib.sha256()
    for name in ("server.py", "caesium.dll"):
        p = os.path.join(src, name)
        try:
            st = os.stat(p)
            h.update(("%s:%d:" % (name, st.st_size)).encode("utf-8"))
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
        except Exception as e:
            h.update(("%s:ERR:%r:" % (name, e)).encode("utf-8"))
    ui = os.path.join(src, "ui")
    if os.path.isdir(ui):
        for root, dirs, files in os.walk(ui):
            dirs.sort()
            for name in sorted(files):
                p = os.path.join(root, name)
                rel = os.path.relpath(p, src).replace("\\", "/")
                try:
                    h.update(("%s:%d:" % (rel, os.path.getsize(p))).encode("utf-8"))
                    with open(p, "rb") as f:
                        h.update(f.read())
                except Exception as e:
                    h.update(("%s:ERR:%r:" % (rel, e)).encode("utf-8"))
    return h.hexdigest()


def prepare_resources(root):
    src = app_dir()
    target_root = os.path.join(root, "runtime")
    target = os.path.join(target_root, "app")
    need = ("server.py", "caesium.dll")
    sig = resource_sig(src)
    stamp = os.path.join(target_root, "stamp")
    uptodate = False
    try:
        with open(stamp, "r", encoding="utf-8") as f:
            uptodate = (f.read().strip() == sig
                        and all(os.path.isfile(os.path.join(target, n)) for n in need)
                        and os.path.isdir(os.path.join(target, "ui")))
    except Exception:
        uptodate = False
    if uptodate:
        return target
    os.makedirs(target, exist_ok=True)
    for n in need:
        s = os.path.join(src, n)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(target, n))
    ui_src = os.path.join(src, "ui")
    ui_dst = os.path.join(target, "ui")
    if os.path.isdir(ui_src):
        if os.path.isdir(ui_dst):
            shutil.rmtree(ui_dst)
        shutil.copytree(ui_src, ui_dst)
    tmp = stamp + ".tmp-" + uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(sig)
    os.replace(tmp, stamp)
    return target


def port_free(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        except Exception:
            pass
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_port(host, preferred):
    for p in range(preferred, preferred + PORT_SCAN):
        if port_free(host, p):
            return p
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def probe(port, timeout=1.0):
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/status" % port,
            headers={"X-Caesium-Token": TOKEN},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("app") == "caesium" and data.get("version") == APP_VERSION:
            return data
    except Exception:
        pass
    return None


def pid_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def read_instance():
    try:
        with open(INSTANCE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def write_instance(pid, exe):
    tmp = INSTANCE_PATH + ".tmp-" + uuid.uuid4().hex
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "exe": os.path.normcase(os.path.abspath(exe)),
                       "version": APP_VERSION}, f)
        os.replace(tmp, INSTANCE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def clear_instance():
    try:
        os.unlink(INSTANCE_PATH)
    except Exception:
        pass


def valid_existing(port):
    rec = read_instance()
    if not rec or rec.get("version") != APP_VERSION or not pid_alive(rec.get("pid")):
        clear_instance()
        return None
    st = probe(port)
    if not st or st.get("pid") != rec.get("pid"):
        clear_instance()
        return None
    if os.path.normcase(os.path.abspath(st.get("exe") or "")) != rec.get("exe"):
        clear_instance()
        return None
    return rec


def wait_ready(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if SERVER_ERROR:
            return None
        st = probe(port, 1.5)
        if st:
            return st
        time.sleep(0.2)
    return None


def start_server(port, workdir, app_path):
    global INSTANCE_PATH
    sys.path.insert(0, app_path)
    os.environ["CAESIUM_DLL"] = os.path.join(app_path, "caesium.dll")
    os.environ["CAESIUM_TOKEN"] = TOKEN
    import server
    argv = ["--port", str(port), "--host", "127.0.0.1", "--workdir", workdir]

    def run():
        old = sys.argv
        try:
            sys.argv = ["server.py"] + argv
            server.main()
        except SystemExit:
            pass
        except Exception as e:
            SERVER_ERROR.append(repr(e))
            log("server crashed: %r" % (e,))
        finally:
            sys.argv = old

    th = threading.Thread(target=run, name="caesium-server", daemon=True)
    th.start()
    return th


def diag():
    print("frozen       =", is_frozen())
    print("bundle       =", bundle_dir())
    print("app          =", app_dir())
    print("data         =", data_dir())
    root = data_dir()
    try:
        with open(os.path.join(root, "token"), "r", encoding="utf-8") as f:
            token_diag = f.read(8) + "..."
    except Exception:
        token_diag = "(将在启动时生成)"
    print("token        =", token_diag)
    print("resource_sig =", resource_sig(app_dir())[:16] + "...")
    print("port %d free = %s" % (PORT_DEFAULT, port_free("127.0.0.1", PORT_DEFAULT)))
    try:
        import webview
        print("pywebview    = OK", getattr(webview, "__version__", ""))
    except Exception as e:
        print("pywebview    = FAIL", repr(e))
    try:
        import clr
        print("pythonnet    = OK")
    except Exception as e:
        print("pythonnet    = FAIL", repr(e))


def icon_path():
    for p in (os.path.join(bundle_dir(), "caesium.ico"),
              os.path.join(bundle_dir(), "app", "caesium.ico")):
        if os.path.isfile(p):
            return p
    return None


def main():
    global DATA_DIR, INSTANCE_PATH
    if "--diag" in sys.argv:
        diag()
        return 0
    DATA_DIR = data_dir()
    INSTANCE_PATH = os.path.join(DATA_DIR, "instance.json")
    load_token(DATA_DIR)

    port = PORT_DEFAULT
    for i, a in enumerate(sys.argv[1:], 1):
        if a == "--port" and i + 1 < len(sys.argv):
            try:
                port = int(sys.argv[i + 1])
            except ValueError:
                message_box("Caesium PC", "端口参数无效，将使用默认端口 %d。" % PORT_DEFAULT)

    try:
        app_path = prepare_resources(DATA_DIR)
    except Exception as e:
        log("prepare_resources failed: %s" % traceback.format_exc())
        message_box("Caesium PC 启动失败", "资源释放失败：%r\n请检查 %s 的写入权限。" % (e, DATA_DIR))
        return 3
    log("start frozen=%s app=%s workdir=%s" % (is_frozen(), app_path, DATA_DIR))

    if not port_free("127.0.0.1", port):
        exist = valid_existing(port)
        if exist:
            message_box("Caesium PC", "程序已经在运行，请先关闭已有窗口。")
            return 0
        port = find_port("127.0.0.1", port)

    st = probe(port)
    if st:
        # 带正确 token 且版本一致：确实是我们自己的残留服务，写回实例信息并复用
        write_instance(st.get("pid") or os.getpid(), st.get("exe") or sys.executable)
    else:
        start_server(port, DATA_DIR, app_path)
        st = wait_ready(port)
        if not st:
            err = SERVER_ERROR[0] if SERVER_ERROR else "30 秒内未就绪"
            log("server not ready: %s" % err)
            message_box("Caesium PC 启动失败", "本地服务启动失败：%s\n详情见 %s\\launcher.log" % (err, DATA_DIR))
            return 2
        write_instance(os.getpid(), sys.executable)

    url = "http://127.0.0.1:%d/" % port
    log("engine=%s cpus=%s workers=%s" % (st.get("engine"), st.get("cpus"), st.get("workers")))

    if "--no-window" in sys.argv:
        print("serving on %s (ctrl+c to stop)" % url, flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            clear_instance()
        return 0

    try:
        import webview
    except Exception as e:
        log("webview import failed: %s" % traceback.format_exc())
        message_box("Caesium PC 启动失败", "缺少 WebView2/pythonnet 依赖：%r" % (e,))
        return 4
    try:
        window = webview.create_window(
            APP_NAME, url, width=1360, height=900, min_size=(980, 640),
            resizable=True, confirm_close=False, text_select=True,
            background_color="#FFFFFF",
        )
        ico = icon_path()
        kwargs = {"icon": ico} if ico else {}
        log("opening native window url=%s icon=%s" % (url, ico))
        webview.start(**kwargs)
        log("window closed, exiting")
    finally:
        clear_instance()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        text = traceback.format_exc()
        log("launcher crashed:\n" + text)
        message_box("Caesium PC 启动失败", text[-1200:])
        code = 3
    sys.exit(code)
