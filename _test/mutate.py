#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：把 v1.7.1 新加的关键分支逐个改坏，确认测试会变红，再改回来。

不做这一步，测试全绿也不可信 —— 因为"永远为真"的测试和"什么都没测"的测试长得一样。

用法： python mutate.py            # 跑全部变异
      python mutate.py --list     # 只列出变异清单

安全性：每个变异都在 try/finally 里恢复原文件，并在开始/结束各校验一次 sha256。
"""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "caesium", "app", "server.py")

SUITE_UNIT = [sys.executable, os.path.join(HERE, "test_unit.py")]
SUITE_FRONT = ["node", os.path.join(HERE, "test_frontend.js")]

# (说明, 原文, 改坏成, 期望变红的套件, 期望变红的原因)
MUTATIONS = [
    ("skip 策略不再先判存在",
     '        if policy == "skip":\n'
     '            try:\n'
     '                if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:',
     '        if policy == "skip":\n'
     '            try:\n'
     '                if False:',
     SUITE_UNIT, "skip 命中时仍会 spawn 引擎 → 用例 test_skip_policy_does_not_spawn_engine 变红"),

    ("sig 比对失效（参数变了也算已完成）",
     'if rec.get("sig") != sig:',
     'if False:',
     SUITE_UNIT, "改参数后仍被判已完成 → test_changed_quality_forces_recompress 变红"),

    ("rename 冲突命名格式改坏",
     '("%s%s (%d)%s" % (base, suffix, n, out_ext))',
     '("%s%s_%d%s" % (base, suffix, n, out_ext))',
     SUITE_UNIT, "落盘变成 a_2.jpg 而非 a (2).jpg → test_rename_policy_* 变红"),

    ("run_task 异常兜底改成卡在 running",
     '        t["state"] = "error"\n        t["error"] = "%s: %s" % (type(e).__name__, e)',
     '        t["state"] = "running"\n        t["error"] = "%s: %s" % (type(e).__name__, e)',
     SUITE_UNIT, "注入异常后任务停在 running → test_run_task_exception_lands_in_error 变红"),

    ("完成记录不落盘（只在内存）",
     '        os.replace(tmp, RESULTS_FILE)',
     '        pass',
     SUITE_UNIT, "重启后读不到记录 → test_results_survive_reload / 落盘相关用例变红"),

    ("已完成判定不再校验输出目录",
     '            if os.path.dirname(os.path.abspath(out)) != os.path.abspath(dst_dir):',
     '            if False:',
     SUITE_UNIT, "换输出目录后仍判已完成 → test_result_is_done_rules 变红"),

    ("skipped 计数串到 ok 上",
     '                if status == "skipped":\n                    final_status = "skipped"\n                    t["skipped"] += 1',
     '                if status == "skipped":\n                    final_status = "skipped"\n                    t["ok"] += 1',
     SUITE_UNIT, "total != ok+skipped+fail，跳过数为 0 → 多个统计用例变红"),

    ("引擎 -O 覆盖标志恒为 never",
     'overwrite_flag = "all" if normalize_exist_policy(options) == "overwrite" else "never"',
     'overwrite_flag = "never"',
     SUITE_UNIT, "覆盖策略不真覆盖 → test_overwrite_policy_really_overwrites 变红"),

    ("skip 放过的外部文件也被登记成我们的产物",
     '                if final_status in ("ok", "copied"):\n                    record_result(src,',
     '                if True:\n                    record_result(src,',
     SUITE_UNIT, "改参数后会把用户自己的同名文件覆盖掉 → test_param_change_never_overwrites_foreign_file 变红"),

    ("前端「已跳过」文案缺失",
     "  if(f.status==='skip')return '<span class=\"st-skip\"'+(f.note?' title=\"'+esc(f.note)+'\"':'')+'>已跳过</span>';",
     "  if(f.status==='skip')return '';   # 变异：不显示「已跳过」",
     SUITE_FRONT, "状态列不显示「已跳过」→ 前端用例变红"),

    ("前端改参数不清空完成状态",
     '    files.forEach(function(f){if(f.status)had=true;f.status=null;f.out=null;f.out_size=null;f.in_size=null;f.note=\'\';});',
     '    files.forEach(function(f){if(f.status)had=true;});',
     SUITE_FRONT, "改质量后仍显示「继续压缩（已完成 N）」→ 前端用例变红"),

    ("前端「清空」退回原生 confirm",
     '  askConfirm(\'清空全部 \'+files.length+\' 个文件？已压缩的结果记录会保留，可「继续压缩」。\',function(){\n'
     '    files=[];rebuildPathIndex();curPage=0;selIdx=-1;render();saveQueue();\n  });',
     '  if(!confirm(\'清空全部 \'+files.length+\' 个文件？\'))return;\n'
     '  files=[];rebuildPathIndex();curPage=0;selIdx=-1;render();saveQueue();',
     SUITE_FRONT, "又用了会被 sandbox 拦掉的 confirm() → 前端用例变红"),

    # ---- v1.7.1 第 2 轮：P0-1 / P1-2 / P1-4 的反向变异 ----
    # 把「先临时目录 + 原子 replace」改回 v1.7.0 的「引擎直接写最终路径」
    ("P0-1 反向变异：落盘改回直接写最终路径（不留临时目录）",
     '    tmp_dir, err = _new_tmp_dir(dst_dir)\n'
     '    if tmp_dir is None:\n'
     '        return False, None, err, "error"\n'
     '    try:\n'
     '        tmp_out, err = _encode_to_tmp(src, tmp_dir, options, out_format, base, suffix, out_ext)\n'
     '        if not tmp_out:\n'
     '            return False, None, err, "error"\n'
     '        try:\n'
     '            os.replace(tmp_out, out_path)   # 同文件系统内的原子替换\n'
     '        except Exception as e:\n'
     '            return False, None, "move output failed: %s" % e, "error"\n'
     '        return True, out_path, "", "ok"\n'
     '    finally:\n'
     '        shutil.rmtree(tmp_dir, ignore_errors=True)',
     '    cmd, _ = build_clt_cmd(src, dst_dir, options, out_format)\n'
     '    proc, launch_err = _run_clt(cmd)\n'
     '    if proc is None:\n'
     '        return False, None, launch_err, "error"\n'
     '    produced = _pick_output(dst_dir, out_path, set())\n'
     '    if produced:\n'
     '        return True, produced, "", "ok"\n'
     '    return False, None, _clt_error_text(proc), "error"',
     SUITE_UNIT,
     "引擎中途崩掉会在最终路径留下半截文件（闪退后被当成已完成跳过）→ "
     "test_atomic_output_never_leaves_partial_file / test_same_folder_crash_keeps_original_intact 变红"),

    # 只删 returncode 校验（QA 的 M5 就是这条，当时 SURVIVED）
    ("P1-2 反向变异：忽略引擎退出码（只看有没有产出文件）",
     '    if proc.returncode != 0:\n'
     '        return None, _clt_error_text(proc, "caesiumclt exited with code %s" % proc.returncode)',
     '    if False:\n'
     '        return None, _clt_error_text(proc, "caesiumclt exited with code %s" % proc.returncode)',
     SUITE_UNIT,
     "引擎崩了但留了半截/目标有旧产物都被记成 ok 并写进记录 → "
     "test_engine_nonzero_exit_is_failure_even_with_stale_output / "
     "test_run_task_engine_failure_is_never_recorded_as_ok 变红"),

    # 去掉 per-output 互斥（前端 setLocked 挡不住双开/换窗口）
    ("P1-4 反向变异：去掉同一输出路径的互斥锁",
     '    with _path_lock(out_path):',
     '    if True:',
     SUITE_UNIT,
     "两个任务同时判定「目标不存在」并各压一遍，skip 语义失效 → "
     "test_concurrent_skip_policy_writes_only_once 变红"),

    # 「输出到原文件夹」的安全提示退回原生 alert（飞牛 iframe 里会被静默拦掉）
    ("P1-10 反向变异：「覆盖原图」提示退回原生 alert",
     "    askConfirm('输出到原文件夹会覆盖原图，请谨慎使用。确认继续？',function(){\n"
     "      setStatus('已启用「输出到原文件夹」：压缩结果会直接覆盖原图，请确认已有备份');\n"
     "    },'确认覆盖原图',revertSameFolder);",
     "    alert('输出到原文件夹会覆盖原图，请谨慎使用');",
     SUITE_FRONT,
     "原生 alert 被 sandbox 拦掉 = 覆盖原图完全无提示 → "
     "「勾选「输出到原文件夹」走自绘确认层」用例变红"),

    # ---- v1.7.1 第 3 轮：C02 残骸例外（升级路径上的存量坏图）的反向变异 ----
    # 恒假 = 等于把「残骸例外」删掉，退回 v1.7.1 第 2 轮的行为
    ("C02 反向变异：残骸判定恒假（残骸例外等于被删掉）",
     "    if not head:\n        return False",
     "    if True:\n        return False",
     SUITE_UNIT,
     "截断的旧残骸又被 skip 永久跳过（用户拿坏图还显示「已跳过」）→ "
     "test_skip_policy_recompresses_truncated_residue / "
     "test_run_task_recompresses_truncated_residue_and_records / "
     "test_image_looks_truncated_rules 变红"),

    # 恒真 = 把判据放宽成「非空就是残骸」，会覆盖用户自己的同名文件（破坏 C01a）
    ("C02 反向变异：残骸判定恒真（任何非空文件都当残骸）",
     "    if not head:\n        return False",
     "    if head:\n        return True",
     SUITE_UNIT,
     "skip 策略永远命中不了，用户自己的同名文件被当残骸覆盖（C01a 语义被破坏）→ "
     "test_skip_policy_does_not_spawn_engine / test_param_change_never_overwrites_foreign_file / "
     "test_image_looks_truncated_rules 变红"),

    # ---- v1.7.2：扫描异步化 + 尺寸解析分块读的反向变异 ----
    # 退回"同步扫盘"：POST /api/scan 又会卡住直到扫完 → 飞牛网关 504 HTML
    ("v1.7.2 反向变异：/api/scan 退回同步扫描（new_scan 里跑完再返回）",
     '    th = threading.Thread(target=run_scan, args=(s,), daemon=True)\n'
     '    th.start()\n'
     '    return s["id"]',
     '    run_scan(s)          # 变异：同步扫完再返回（v1.7.0/v1.7.1 的阻塞行为）\n'
     '    return s["id"]',
     SUITE_UNIT,
     "POST /api/scan 要等整棵树扫完才返回（2000 个文件几百 ms，NAS 上几分钟）→ "
     "网关超时回 HTML，前端报 Unexpected token '<' → "
     "test_scan_returns_immediately_and_pages_are_complete 变红（elapsed < 50ms 断言）"),

    # 退回"整文件读"：尺寸解析又把整张图读进内存
    ("v1.7.2 反向变异：JPEG 尺寸解析退回整文件读（f.read() 全读）",
     '        buf = b""\n'
     '        eof = False\n'
     '        read_total = 0\n'
     '        pos = 2                       # 跳过 SOI(FFD8)',
     '        buf = f.read()   # 变异：整文件读进内存（v1.7.1 的老行为）\n'
     '        eof = True\n'
     '        read_total = len(buf)\n'
     '        pos = 2',
     SUITE_UNIT,
     "一张 8MB 照片白读 8MB（2000 张 = 15.6 GB，用户看到「还在拼命读取」）→ "
     "test_jpeg_size_never_reads_whole_file 变红（读取量 < 600KB 断言）"),

    # ---- v1.7.2 第 2 轮（QA 第 1 轮判定）: P0 分页收尾 / P1 Motion Photo 的反向变异 ----
    # 收尾只看 state，不检查"是否真的拉完了" → 弱网/多页时尾页永久丢失
    ("QA-P0 反向变异：分页收尾不检查 scanOffset<total（尾页丢弃）",
     "    if(progressed&&scanOffset<scanTotal){\n"
     "      setStatus('正在整理扫描结果…（已收 '+scanOffset+'/'+scanTotal+'）');",
     "    if(false){\n"
     "      setStatus('正在整理扫描结果…（已收 '+scanOffset+'/'+scanTotal+'）');",
     SUITE_FRONT,
     "服务端说 done 但客户端只收到一页就收尾 → 5000 张只显示 500~4545 张 → "
     "「收尾帧落后于 total 时不许提前收尾（1500 条一条不少）」用例变红"),

    # EOI 只查尾部 2KB → Motion Photo 被判成残骸 → skip 被绕过、覆盖用户照片
    ("QA-P1 反向变异：JPEG 的 EOI 只查文件尾 2KB（Motion Photo 被当残骸）",
     "            return not _jpeg_has_eoi(path, size)",
     "            return JPEG_EOI not in tail",
     SUITE_UNIT,
     "EOI 之后带 5KB 附加数据的手机照片被判成「闪退残骸」，skip 策略失效 → "
     "test_image_looks_truncated_motion_photo / test_motion_photo_is_never_overwritten_by_skip 变红"),

    # ---- QA 第 2 轮遗留：truncated 丢页 / EOI 跨块边界 的反向变异 ----
    # 两处条件重新带上 !j.truncated → 到 50000 上限时已收录的尾页被静默丢掉
    ("QA-R2-P1 反向变异：truncated=true 时不继续拉页（两处条件恢复 !j.truncated）",
     "      if(progressed&&scanOffset<scanTotal&&depth<SCAN_MAX_PAGES)pullScanPage(depth+1);\n"
     "      return;\n"
     "    }\n"
     "    /* 收尾帧：服务端说\"扫完了\"，但客户端可能还落后（弱网/一帧只给 500 条）。\n"
     "       只按 state 收尾会把尾页永久丢掉（QA P0：5000 张只收到 1500~4545 张）。\n"
     "       只要还没拉完就继续拉，绝不在中途收尾。\n"
     "       注意：这里**不能**因为 truncated=true 就跳过 —— truncated 只代表\"目录没扫全\n"
     "       （到 50000 上限）\"，不代表\"已收录的这几页可以少拉\"（QA S10b：500/1500）。\n"
     "       是否该告警「结果可能不完整」由 finishScan 单独判断（那里才看 truncated）。\n"
     "       progressed 是防死循环闸：服务端（或桩）返回没有推进 offset 的重复页时，\n"
     "       不能无限轮询下去，直接收尾由 finishScan 如实说明。 */\n"
     "    if(progressed&&scanOffset<scanTotal){",
     "      if(progressed&&!j.truncated&&scanOffset<scanTotal&&depth<SCAN_MAX_PAGES)pullScanPage(depth+1);\n"
     "      return;\n"
     "    }\n"
     "    /* 变异：恢复 v1.7.2 第 2 轮的 !j.truncated（truncated 时不继续拉页） */\n"
     "    if(progressed&&!j.truncated&&scanOffset<scanTotal){",
     SUITE_FRONT,
     "truncated=true 且客户端落后时只收到第一页就收尾（QA S10b：500/1500）→ "
     "「truncated=true 且客户端落后时，已收录的 total 仍要全部拉到（1500/1500）」用例变红"),

    # ---- v1.7.4：GIF 直通复制 ----
    ("v1.7.4 反向变异：GIF 直通分支去掉（GIF 又去调引擎压缩）",
     '        if _fmt2 == "gif":\n'
     '            return _copy_through(src, dst_dir, out_path, policy, base, suffix, out_ext)',
     '        if False:   # 变异：GIF 退回走引擎\n'
     '            return _copy_through(src, dst_dir, out_path, policy, base, suffix, out_ext)',
     SUITE_UNIT,
     "GIF 又被 spawn 引擎、输出不再与源逐字节相同、状态记成 ok 而不是 copied → "
     "test_gif_is_copied_not_compressed / test_gif_respects_exist_policies / "
     "test_gif_resume_after_copy_is_skipped 变红"),

    # 去掉块间 1 字节滑窗 → EOI 恰好跨 64KB 边界时漏判，合法 JPEG 被覆盖
    ("QA-R2-P2 反向变异：EOI 回扫块间不留重叠（跨 64KB 边界漏判）",
     "            if JPEG_EOI in (chunk + carry):",
     "            if JPEG_EOI in chunk:",
     SUITE_UNIT,
     "FF 在前一块末尾、D9 在后一块开头时匹配不到 FFD9 → 合法 JPEG 被判残骸并覆盖用户原图 → "
     "test_jpeg_eoi_cross_chunk_boundary 变红"),

    # ---- v1.7.5：列表分页 ----
    # 变异：渲染时又铺满整表（不切片）→ 上万文件回到"一次性 12000 行"的卡死形态
    ("v1.7.5 反向变异：分页失效（render 又铺满整表）",
     "  for(i=start;i<end;i++)html.push(rowHtml(files[i],i));",
     "  for(i=0;i<files.length;i++)html.push(rowHtml(files[i],i));   # 变异：不分页，整表铺开",
     SUITE_FRONT,
     "12000 条会一次性渲染 12000 行 → 「12000 条只渲染第一页 5000 行」与 "
     "「切每页条数后行数=1000」两条用例变红"),

    # 变异：大列表又走"把 files 塞进请求体"——1 万条以上就会撞网关 1MB 上限
    ("v1.7.5 反向变异：任务提交不再用队列引用（files 塞进请求体）",
     "  var useQueue=wholeList&&files.length>=QUEUE_REF_MIN&&files.length<=QUEUE_MAX_FILES;",
     "  var useQueue=false;   # 变异：退回逐条塞 files",
     SUITE_FRONT,
     "12000 条的请求体会变成 1MB+ → 「use_queue=true 且 body 很小（不撞网关 1MB）」用例变红"),
]


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_suite(cmd):
    proc = subprocess.run(cmd, capture_output=True, cwd=ROOT)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    tail = [ln for ln in out.splitlines() if ln.startswith("FAIL ") or ln.startswith("== ")]
    return proc.returncode, tail


def main():
    if "--list" in sys.argv:
        for i, m in enumerate(MUTATIONS, 1):
            print("%2d. %s\n    %s" % (i, m[0], m[4]))
        return 0

    # --only 10,12 只跑指定的几条（改了锚点后不用等全套 14 分钟）
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--only"):
            only = set()
            for part in a.split("=", 1)[-1].replace("--only", "").split(","):
                part = part.strip()
                if part.isdigit():
                    only.add(int(part))
    original_hash = sha(SERVER)
    print("baseline sha256 = %s" % original_hash)
    # 先确认基线是绿的，否则变异结果没有意义
    for cmd, label in ((SUITE_UNIT, "unit"), (SUITE_FRONT, "frontend")):
        rc, tail = run_suite(cmd)
        print("baseline %-8s rc=%d %s" % (label, rc, tail[-1] if tail else ""))
        if rc != 0:
            print("!! 基线测试未通过，变异测试没有意义，先修基线")
            return 2

    results = []
    for i, (name, old, new, suite, why) in enumerate(MUTATIONS, 1):
        if only is not None and i not in only:
            continue
        with open(SERVER, "r", encoding="utf-8", newline="") as f:
            src = f.read()
        if src.count(old) != 1:
            print("SKIP %2d. %s —— 锚点出现 %d 次（应为 1），锚点已失效" %
                  (i, name, src.count(old)))
            results.append((name, "ANCHOR-MISS", why))
            continue
        mutated = src.replace(old, new)
        try:
            with open(SERVER, "w", encoding="utf-8", newline="") as f:
                f.write(mutated)
            rc, tail = run_suite(suite)
        finally:
            with open(SERVER, "w", encoding="utf-8", newline="") as f:
                f.write(src)
        killed = rc != 0
        print("%s %2d. %s → %s" % ("KILLED  " if killed else "SURVIVED", i, name,
                                   "测试变红 ✓" if killed else "测试仍绿 ✗（这条分支没被测到）"))
        for ln in tail[-3:]:
            print("        %s" % ln)
        results.append((name, "KILLED" if killed else "SURVIVED", why))

    restored = sha(SERVER)
    print("\nrestored sha256 = %s  %s" % (restored, "OK" if restored == original_hash else "!!! 恢复失败"))
    # 恢复后再跑一次基线，证明改回来是干净的
    for cmd, label in ((SUITE_UNIT, "unit"), (SUITE_FRONT, "frontend")):
        rc, tail = run_suite(cmd)
        print("after-restore %-8s rc=%d %s" % (label, rc, tail[-1] if tail else ""))

    killed_n = len([r for r in results if r[1] == "KILLED"])
    print("\n== 变异 %d 个：KILLED %d / SURVIVED %d / 锚点失效 %d ==" %
          (len(results), killed_n,
           len([r for r in results if r[1] == "SURVIVED"]),
           len([r for r in results if r[1] == "ANCHOR-MISS"])))
    for name, verdict, why in results:
        print("  [%s] %s\n        期望：%s" % (verdict, name, why))
    if restored != original_hash:
        return 3
    return 0 if killed_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
