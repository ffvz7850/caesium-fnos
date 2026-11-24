"""Rebuild the one-file exe by patching only two CArchive entries.

The original PyInstaller bundle is reused byte-for-byte for every dependency;
only ``app\\server.py`` and ``launcher_native`` are replaced.  This keeps the
runtime (Python 3.13, pywebview, pythonnet, WebView2 loader ...) identical to
the shipped 1.9.0 build and avoids dependency drift after a full rebuild.

Usage (Windows, Python 3.13):

    python build_exe.py --orig-exe "C:/path/to/CaesiumPC-1.9.0-single/CaesiumPC.exe"

The original 1.9.0 exe is NOT redistributed with this source package; use the
official release you already have.  Output goes to ``dist/CaesiumPC.exe``.
"""
from __future__ import annotations

import argparse
import hashlib
import marshal
import struct
import zlib
from pathlib import Path

MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
COOKIE_FMT = "!8sIIII64s"
TOC_FMT = "!IIIIBc"
HEAD = struct.calcsize(TOC_FMT)
HERE = Path(__file__).resolve().parent


def align16(n: int) -> int:
    return (n + 15) // 16 * 16


def parse_toc(data: bytes):
    i = data.rfind(MAGIC)
    if i < 0:
        raise SystemExit("PyInstaller cookie not found; is this a one-file exe?")
    _, pkglen, tocoff, toclen, pyvers, pylib = struct.unpack(COOKIE_FMT, data[i:i + 88])
    base = len(data) - pkglen
    toc_start = base + tocoff
    toc_end = toc_start + toclen
    entries = []
    p = toc_start
    while p < toc_end:
        elen, pos, clen, ulen, cflag, typ = struct.unpack(TOC_FMT, data[p:p + HEAD])
        name = data[p + HEAD:p + elen].split(b"\x00", 1)[0].decode("utf-8")
        entries.append({"elen": elen, "pos": pos, "clen": clen, "ulen": ulen,
                        "cflag": cflag, "type": typ, "name": name})
        p += elen
    return base, entries, pyvers, pylib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig-exe", required=True, help="original CaesiumPC 1.9.0 one-file exe")
    ap.add_argument("--server", default=str(HERE / "app" / "server.py"))
    ap.add_argument("--launcher", default=str(HERE / "launcher_native.py"))
    ap.add_argument("--out", default=str(HERE / "dist" / "CaesiumPC.exe"))
    args = ap.parse_args()

    data = Path(args.orig_exe).read_bytes()
    base, entries, pyvers, pylib = parse_toc(data)

    replacements = {
        "app\\server.py": Path(args.server).read_bytes(),
        "launcher_native": marshal.dumps(
            compile(Path(args.launcher).read_text(encoding="utf-8"),
                    "launcher_native.py", "exec")),
    }

    found = set()
    blob = bytearray()
    toc = bytearray()
    for e in entries:
        if e["name"] in replacements:
            raw = replacements[e["name"]]
            found.add(e["name"])
        else:
            raw = data[base + e["pos"]:base + e["pos"] + e["clen"]]
            if e["cflag"]:
                raw = zlib.decompress(raw)
        comp = zlib.compress(raw, 9) if e["cflag"] else raw
        new_pos = len(blob)
        blob.extend(comp)
        name_b = e["name"].encode("utf-8") + b"\x00"
        elen = align16(HEAD + len(name_b))
        toc.extend(struct.pack(TOC_FMT, elen, new_pos, len(comp), len(raw), e["cflag"], e["type"]))
        toc.extend(name_b)
        toc.extend(b"\x00" * (elen - HEAD - len(name_b)))

    missing = set(replacements) - found
    if missing:
        raise SystemExit("replacement entries not found: %s" % missing)

    pkglen = len(blob) + len(toc) + struct.calcsize(COOKIE_FMT)
    cookie = struct.pack(COOKIE_FMT, MAGIC, pkglen, len(blob), len(toc), pyvers, pylib)
    out = bytes(data[:base]) + bytes(blob) + bytes(toc) + cookie

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out)

    print("orig exe", len(data), "new exe", len(out))
    print("python", pyvers, "bootstrap", pylib.rstrip(b"\x00").decode())
    print("server.py", len(replacements["app\\server.py"]),
          "launcher code object", len(replacements["launcher_native"]))
    print("sha256", hashlib.sha256(out).hexdigest())
    print("out", out_path)


if __name__ == "__main__":
    main()
