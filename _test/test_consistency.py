#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局一致性审查（跨文件/跨前后端，静态可复核）。

检查项：
1. 前端 JS 里引用的元素 id 是否都在 PAGE 的 HTML 里存在（拼错 id = 静默失效）
2. 前端 CSS 类（状态列 .st-*）是否都有样式定义
3. 前端 fetch('api/xxx') 的每个接口后端是否都有分支处理；反向也查（后端有没有白写的接口）
4. compress_file 的新签名 (ok, out_path, err, status) 是否所有调用点都改了
5. 任务统计口径 done == ok + skipped + fail：所有计数自增点是否成对
6. 关键函数/常量是否都存在

用法： python test_consistency.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "caesium", "app", "server.py")

FAILS = []


def check(cond, ok_msg, bad_msg):
    if cond:
        print("  PASS %s" % ok_msg)
    else:
        print("  FAIL %s" % bad_msg)
        FAILS.append(bad_msg)


def main():
    with open(SERVER, "r", encoding="utf-8") as f:
        src = f.read()

    # PAGE 是 r"""...""" 常量：HTML 部分 + <script> 部分
    m = re.search(r'PAGE = r"""(.*?)"""\n', src, re.S)
    if not m:
        print("FAIL 解析不到 PAGE 常量")
        return 1
    page = m.group(1)
    script = page[page.index("<script>"):page.index("</script>")]
    html = page[:page.index("<script>")]

    print("== 1. 前端 id 引用 vs HTML 定义 ==")
    defined = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))
    used = set(re.findall(r"""\$\(['"]([A-Za-z0-9_\-]+)['"]\)""", script))
    used |= set(re.findall(r"""getElementById\(['"]([A-Za-z0-9_\-]+)['"]\)""", script))
    # 批量引用 id 的写法（setLocked 等）：只认 .forEach(function(id)) 前的数组字面量，
    # 避免把 ['c','r','o']（tab key）误判成 id 列表
    for group in re.findall(r"\[([^\]]*)\]\s*\n?\s*\.forEach\(function\(id\)", script):
        used |= set(re.findall(r"'([A-Za-z0-9_\-]+)'", group))
    # 运行时动态创建的按钮（loadMyFolders 里 innerHTML 拼出来的），静态扫描看不到
    DYNAMIC_IDS = {"reloadBtn"}
    missing = sorted(used - defined - DYNAMIC_IDS)
    check(not missing, "JS 引用的 %d 个 id 全部在 HTML 中存在" % len(used),
          "JS 引用了不存在的 id：%s" % missing)
    unrefed = sorted(defined - used)
    print("  （HTML 里定义但 JS 未直接引用的 id：%s —— 仅作提示）" % (unrefed or "无"))

    print("\n== 2. 状态列 CSS 类 ==")
    css_classes = set(re.findall(r"\.(st-[a-z]+)\{", page))
    js_classes = set(re.findall(r"""class="(st-[a-z]+)\"""", script))
    missing_css = sorted(js_classes - css_classes)
    check(not missing_css, "状态类样式齐全：%s" % sorted(css_classes), "缺少样式定义：%s" % missing_css)

    print("\n== 3. 前端调用的接口 vs 后端路由 ==")
    calls = set()
    for raw in re.findall(r"""fetch\(\s*'([^']+)'""", script):
        path = raw.split("?")[0]
        if not path.startswith("api/"):
            continue
        calls.add("/" + path)
    # 处理 'api/tasks/'+taskId 这类拼接
    routes = set(re.findall(r'p == "(/api/[^"]+)"', src))
    prefixes = set(re.findall(r'p\.startswith\("(/api/[^"]+)"\)', src))
    unknown = sorted(c for c in calls
                     if c not in routes and not any(c.startswith(p) for p in prefixes))
    check(not unknown, "前端调用的接口后端都有分支：%s" % sorted(calls),
          "前端调了后端没实现的接口：%s" % unknown)
    for r in sorted(routes):
        print("    路由 %-24s 前端%s调用" % (r, "" if r in calls else "未直接"))

    print("\n== 4. compress_file 新签名调用点 ==")
    assignments = re.findall(r"([\w, ]+)=\s*compress_file\(", src)
    ok4 = [a for a in assignments if [x.strip() for x in a.split(",")] == ["ok", "out_path", "err", "status"]]
    check(len(assignments) == len(ok4) and len(ok4) >= 1,
          "compress_file 的 %d 个调用点都用 (ok, out_path, err, status) 解包" % len(assignments),
          "compress_file 调用点解包不匹配：%s" % assignments)
    check("return False, None, \"caesiumclt error" not in src,
          "旧的 3 元组返回点已全部清理", "仍有返回 3 元组的旧代码")
    for ret in re.findall(r"return (True|False), [^\n]+", src):
        pass
    bad3 = re.findall(r"return (?:True|False), [^\n,#]+, [^\n,#]+\n", src)
    check(not bad3, "compress_file 系列全部返回 4 元组", "存在 3 元组 return：%s" % bad3[:3])

    print("\n== 5. 计数口径 done == ok + copied + skipped + fail ==")
    # run_task 里每个 done += 1 之后必须恰好有一个 ok/copied/skipped/fail 自增
    body = src[src.index("def run_task(") : src.index("# ---------------------------------------------------------------- 文件夹管理")]
    n_done = body.count('t["done"] += 1')
    n_bucket = (body.count('t["ok"] += 1') + body.count('t["skipped"] += 1')
                + body.count('t["fail"] += 1') + body.count('t["copied"] = t.get("copied", 0) + 1'))
    print("     done += 1 出现 %d 次；ok/copied/skipped/fail += 1 共 %d 次" % (n_done, n_bucket))
    check(n_done > 0 and n_bucket >= n_done,
          "每个 done 自增都伴随一个结果桶自增（%d/%d）" % (n_done, n_bucket),
          "计数不自洽：done=%d 桶=%d" % (n_done, n_bucket))
    check("skipped" in body, "run_task 统计了 skipped", "run_task 未统计 skipped")
    check('t["copied"] = t.get("copied", 0) + 1' in body,
          "run_task 统计了 copied（GIF 直通）", "run_task 未统计 copied")
    check('"copied": 0' in src, "new_task 初始化了 copied", "new_task 缺 copied 字段")
    check('"copied": t.get("copied", 0)' in src, "task_snapshot 带上了 copied", "task_snapshot 缺 copied")
    check('not in ("ok", "skipped", "copied")' in src,
          "result_is_done 接受 copied（断点续传认识 GIF）", "result_is_done 不接受 copied")

    print("\n== 6. 关键符号存在性 ==")
    for sym in ["results_signature", "load_results", "flush_results", "record_result",
                "clear_results", "result_is_done", "has_record", "normalize_exist_policy",
                "resolve_out_format", "resolve_dst_dir", "task_snapshot", "_claim_target",
                "_compress_rename", "_send_file", "MAX_IMAGE_BYTES", "STREAM_CHUNK",
                "RESULTS_FILE", "SIG_FIELDS", "EXIST_POLICIES", "image_looks_truncated",
                "JPEG_EOI", "PNG_IEND", "TRUNC_TAIL_BYTES",
                # v1.7.2：扫描异步化 + JPEG 分块解析
                "_jpeg_size", "JPEG_MAX_SCAN", "JPEG_CHUNK", "new_scan", "run_scan",
                "cancel_scan", "scan_snapshot", "SCANS", "SCAN_LOCK", "SCAN_MAX_FILES"]:
        check(("def %s(" % sym in src) or ("%s = " % sym in src), "存在 %s" % sym, "缺少 %s" % sym)

    print("\n== 7. 版本号一致性（版本号以 manifest 为唯一真源，动态读取） ==")
    with open(os.path.join(ROOT, "caesium", "manifest"), "r", encoding="utf-8") as f:
        manifest = f.read()
    mv = re.search(r"(?m)^\s*version\s*=\s*([0-9][0-9A-Za-z.\-]*)\s*$", manifest)
    version = mv.group(1) if mv else None
    check(bool(version), "manifest 可解析出版本号", "manifest 缺 version 字段，版本一致性无从校验")
    if version:
        check('server_version = "CaesiumWeb/%s"' % version in src,
              "server_version = %s" % version, "server_version 与 manifest(%s) 不一致" % version)
        check('"version": "%s"' % version in src,
              "/api/status 报 %s" % version, "/api/status 版本与 manifest(%s) 不一致" % version)
        # 启动日志可能带 (multi-thread) 等后缀，只匹配前缀
        check("Caesium for fnOS %s" % version in src,
              "启动日志含 %s" % version, "启动日志版本与 manifest(%s) 不一致" % version)
        check("v%s" % version in src.split('"""')[1],
              "docstring 版本 v%s" % version, "docstring 版本与 manifest(%s) 不一致" % version)
        check(re.search(r"(?m)^\s*version\s*=\s*%s\s*$" % re.escape(version), manifest) is not None,
              "manifest 版本 %s" % version, "manifest 版本行异常")

    print("\n== 8. 前端安全解析（网关返回 HTML 时不许炸 JSON 解析） ==")
    check("function jsonSafe(" in script, "存在 jsonSafe() 安全解析函数", "缺少 jsonSafe()")
    naked = re.findall(r"\.json\(\)", script)
    check(not naked, "前端 %d 处响应解析全部走 jsonSafe()" % script.count(".then(jsonSafe)"),
          "前端仍有 %d 处裸 .json()：网关超时（HTML 错误页）时会抛 Unexpected token '<'" % len(naked))
    check("服务未响应或超时" in script, "非 JSON 响应给出人话提示", "非 JSON 响应没有友好提示")

    print("\n== %s ==" % ("一致性审查通过" if not FAILS else "一致性审查失败 %d 项" % len(FAILS)))
    for f in FAILS:
        print("  - %s" % f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
