# caesium for fnOS v1.7.2 —— 第 2 轮（最后一轮）独立放行回归

- 验证人：严过关（QA）
- 源码 `caesium/app/server.py` sha256 `2fc2e186f7b16cb6cf809fcbbacf0016172f4c8bd258625cb79b30b5ae664125`（2862→本轮 2920+ 行），`caesium-arm/app/server.py` 同源
- 最终交付包（本轮验的是交付那一刻的快照 `_test/qa/artifacts/final_*.fpk`）：
  - x86 `ce196fba468dfc879850ccc2e1ec7374cdc21b1e41b8361919f2c79130b6b4fe` 2,713,179 B
  - arm `d2422ae70bfd907f3248909dadf197bd233462adc0e3ab038d4c8537fb75a776` 2,570,767 B
- 结论先行：**放行（NoOne）——可以交付给用户安装**。放行标准三项全部满足；新发现 1 个 P1 + 1 个 P2 属极端边界，如实列为 Known Issues（含可直接落地的修法），不阻塞本次交付。

## 一、先回答"sha 没变是不是没修"

**没有因为 sha 未变就判定没修，也没有盲信工程师**。我自己读代码 + 用功能测试证明：

| 声称修复 | 代码位置（我自己 grep/读到的） | 功能证据 |
|---|---|---|
| P0 收尾继续拉 + 数量自检 | `pullScanPage` L2049-2053 `if(!j.truncated&&scanOffset<scanTotal){ '正在整理扫描结果…（已收 X/Y）'; if(depth<SCAN_MAX_PAGES)pullScanPage(depth+1); return; }`；`finishScan` L2063/2073 `consumed<total` 提示 | S3 stub 1500/1500、S9 本地 5000/5000、S9b 300ms RTT 5000/5000 |
| P0 `scanInFlight` 互斥 | L1995 变量、L2024 `scanTick(){if(scanId&&scanInFlight===0)...}`、L2031 `++`、L2057 成功/失败都 `--` | S11（响应 700 ms > tick 500 ms）offset 序列 `0,500,1000,1500`、最大并发 1、重复 0 |
| P1 `_jpeg_has_eoi` | L204-234：尾 2KB 命中即返回；否则按 64KB 从尾回扫，`EOI_SCAN_MAX=8MB` 预算耗尽返回 True（不判残骸）；`image_looks_truncated` L276-281 调用 | C01d `skipped=1 ok=0 spawn=0 用户文件未被覆盖=True`；A4 六个样本判定全部符合；A8 读盘量实测 |
| P2 队列分批 | 前端 `saveQueue` L1925-1954（`QUEUE_BATCH` 串行、`append:!first`）；服务端 L2805-2836（`QUEUE_MAX_FILES=20000`、按 path 幂等去重、tmp+`os.replace`） | B7a/b/c/d 4/4 PASS；20001 条→保存 20000 条 |

## 二、放行标准（我上一轮自己定的，逐条自己跑）

| 标准 | 命令 | 原始 stdout | 结论 |
|---|---|---|---|
| `qa_frontend_scan.js` S3/S9/S9b 全绿 | `node qa_frontend_scan.js` | `[实测] 本地 状态栏: 添加 5000 个文件` / `列表行数=5000 / 目录里真实文件数=5000`；`[实测] 300ms RTT 列表行数=5000`；S3 PASS | PASS |
| `qa_adv.py` C01d 变绿 | `python qa_adv.py` | `C01d … 实际: skipped=1 ok=0 spawn=0 用户文件未被覆盖=True`；`合计 31 条，PASS 30，FAIL 1`（唯一 FAIL 是 C04，已知设计取舍，文档第六节已声明） | PASS |
| 工程师 6 组全绿 | `cd _test && rm -rf build build-arm && python run_all.py` | `== 47 passed, 0 failed ==` / `== 14 passed, 0 failed ==` / `== 24 passed, 0 failed ==` / `== 变异 22 个：KILLED 22 / SURVIVED 0 / 锚点失效 0 ==` / `6 组全部通过` | PASS |

> `run_all.py` 的变异阶段会原地改 `server.py`，我全程独占运行、期间没有并行文件操作；跑完校验
> `caesium/app/server.py` 与 `caesium-arm/app/server.py` 都还原为 `2fc2e186…b5ae664125`。
> 它的 `test_package.py` 会重新打包并**覆盖 dist 两个 fpk**（重打后 sha 变成 `50e05e3a…`/`c6b5e426…`），
> 我已用交付快照把 `dist/` 恢复回 `ce196fba…`/`d2422ae7…`，并复核与说明文档一致（见第六节）。

## 三、A. 上一轮两个缺陷是否真消失

| 编号 | 场景 | 期望 | 实际 | 结论 |
|---|---|---|---|---|
| S3 | stub：`state=done, total=1500` 只给一页 500 条 | 列表 1500 | PASS（1500） | PASS |
| S9 | 真 server + 真 5000 张图，本地直连 | 5000 | `[实测] 列表行数=5000 / 真实 5000` | PASS |
| S9b | 同上 + 模拟 300 ms RTT | 5000 | `[实测] 列表行数=5000 / 真实 5000` | PASS |
| S10a | `truncated=true` 不得误报「结果可能不完整」 | 状态栏含「上限」、不含「结果可能不完整」 | PASS | PASS |
| S11 | 响应 700 ms > tick 500 ms | 在飞请求 ≤1、offset 零重复 | `[实测] offset 序列=0,500,1000,1500 最大并发=1 重复=0` | PASS |
| C01d | Motion Photo（EOI 后 5KB） | skip、零 spawn、逐字节不变 | `skipped=1 ok=0 spawn=0 用户文件未被覆盖=True` | PASS |
| A4 | 残骸判定不能被 P1 改松：截断 JPEG 2KB/300KB、截断 PNG 300KB、完整图、Motion Photo、9MB 无 EOI | 截断 True / 其余 False | `不符合=无` | PASS |
| A4b | 9MB 无 EOI 的用户同名文件（超 8MB 预算） | skip，0 spawn，文件不变 | `state=done ok=0 skipped=1 spawn=0 内容未变=True` | PASS |
| A8 | `_jpeg_has_eoi` 的 I/O 代价 | 20MB 正常图零额外读；尾部无 EOI 大图被 8MB 预算截断 | `正常图读 12288 字节；无 EOI 大图读 8400896 字节（预算=8388608）` | PASS |
| B9a/b/c | 真网关 504 复跑 | 人话提示 / 504 HTML 可辨 / 取消后读盘 0 | `status=200 耗时=4 ms`；`status=504 body='<html>…504 Gateway Time-out…'`；`cancel 前 state=running，前 0.3s 读 75150 字节；取消后 1.5s 读 0 字节；最终 state=canceled` | PASS |

## 四、B. 我自己再挖的边界结果

| 编号 | 场景 | 期望 | 实际 | 结论 |
|---|---|---|---|---|
| B6a | 扫描中乱序/重复/越界/空页翻页（offset=0,0,200,99999,400,800,1200,0,600,1000,1400） | 最终 1200 条、无重复、state=done | `最终 total=1200 全量=1200 唯一=1200 state=done` | PASS |
| B6b | 取消 A 后立刻重扫 B | A=canceled、B=done、交集 0 | `A state=canceled total=315；B state=done total=1200；交集=0` | PASS |
| B7a | 4 批 append（第三批与第二批重叠 200 个 path） | 去重后 1800 条 | `queue 条数=1800 唯一=1800` | PASS |
| B7b | 第 2 批失败（模拟：只成功第一整批） | 服务端留下 500 条（半份队列） | `第 2 批失败后 queue=500 条（原来 1800 条）` | PASS（事实记录，见遗留 P2-3） |
| B7c | `append=false` 整份替换 | 只剩本次 300 条，无旧条目 | `条数=300 含旧条目=False` | PASS |
| B7d | 一次 POST 20001 条 | 保存 20000、`truncated` | `queue=20000 条` | PASS |
| **S10b** | **`truncated=true` 且客户端落后时，已收录 total 是否拉完** | **1500 条全收** | **`[实测] 列表行数=500 / 已收录 total=1500 状态栏=添加 500 个文件（目录太大，已到上限 1500 个，其余未加入）`** | **FAIL（新 P1）** |
| **A5** | **EOI(FFD9) 恰好跨 64KB 回扫块边界** | **判 False（不是残骸）** | **`EOI 位置=65535/65536，image_looks_truncated=True（跨块=True）`** | **FAIL（新 P2）** |

## 五、C. 回归与包级

| 套件 | 结果 |
|---|---|
| `qa_scan_api.py` | `合计 11 条，PASS 11，FAIL 0` |
| `qa_dim_regress.py` | `合计 4 条，PASS 4，FAIL 0` |
| `qa_package_smoke.py artifacts/final_x86.fpk` | `合计 8 条，PASS 8，FAIL 0` |
| `qa_package_172.py`（final x86 + arm + 说明文档） | `合计 16 条，PASS 16，FAIL 0` |
| `qa_adv.py` | `合计 31 条，PASS 30，FAIL 1`（C04 已知取舍） |
| `qa_frontend_scan.js`（13 条） | `12 passed, 1 failed`（S10b） |
| `qa_round2.py`（13 条，本轮新增） | `合计 13 条，PASS 12，FAIL 1，耗时 15.5s`（唯一 FAIL = A5） |

> `qa_round2.py` 中途出现过 3 个**我自己脚本**的 bug（`wait_scan` 的 URL 漏 `% sid`、B7a 期望值算错、
> B9c 观察窗太长导致扫描自然结束），已按 QA 路由自行修正并全量复跑；最终数字如上，与产品无关。

包级明细：
```
P5 [x86] 少=[] 多=[] 变化=['server.py']；引擎一致=True（sha=ebe954f796f946d1）
Q5 [arm] 少=[] 多=[] 变化=['server.py']；引擎一致=True（sha=6d092e173e18f1fd）
P7 [x86] POST 耗时 3.x ms scan_id=... 最终 total=30 state=done   ← 用包内 server.py 真起服务扫描
Q7 [arm] 同
顶层项=8 ['ICON.PNG','ICON_256.PNG','LICENSE','app.tgz','cmd','config','manifest','wizard']（两包均同）
包内 server.py sha256 = 2fc2e186f7b16cb6…（与工作树源码同源）
R1 说明文档 caesium-1.7.2-说明.md 的 sha256/md5/大小 vs 最终包：未对上=无 → PASS
R2 源码包 caesium-1.7.2-src.zip 可解、含 server.py 与 LICENSE → PASS
```
说明文档四、文件与校验值里的 `ce196fba…` / `d2422ae7…`、大小 `2,713,179 B` / `2,570,767 B`、
md5 `196e1b64…` / `9e4bf5de…` 与我实测的最终包逐项一致，**不是旧哈希**。

## 六、遗留问题（Known Issues，如实标注，不含糊）

### P1（新，极端边界，不阻塞本次交付）：`truncated=true` 时不继续拉尾页
- 位置：`PAGE.pullScanPage()` L2043 / L2049
- 现象：服务端到 `SCAN_MAX_FILES=50000` 上限后置 `truncated=true`，客户端一旦看到该标志就
  **既不递归拉页、也在收尾帧直接 `finishScan`**；若此刻 `scanOffset<total`，已收录的文件会丢一大截，
  而状态栏只说「目录太大，已到上限 N 个，其余未加入」，**不会**提示「结果可能不完整」（S10a 特意验证了不误报）。
- 触发门槛：目录 >50000 文件（设计上限）+ 客户端落后（弱网/大目录）。普通 5000 张图的场景已全绿。
- 修法（2 处，把「是否继续拉」和「是否自检告警」解耦）：
  ```js
  // L2043 running 分支
  if(scanOffset<scanTotal&&depth<SCAN_MAX_PAGES)pullScanPage(depth+1);
  // L2049 收尾分支
  if(scanOffset<scanTotal){ setStatus('正在整理扫描结果…（已收 '+scanOffset+'/'+scanTotal+'）');
                            if(depth<SCAN_MAX_PAGES)pullScanPage(depth+1); return; }
  // finishScan 的自检保持原样：if(!trunc&&total&&consumed<total) 才提示
  ```
  即：`truncated` 只代表"目录没扫全"，不代表"已收录的可以少拉"。

### P2（新，需精确构造）：`_jpeg_has_eoi` 跨 64KB 块边界漏判 FFD9
- 位置：`_jpeg_has_eoi()` L225-233，每块独立 `if JPEG_EOI in f.read(end-start)`；
  若 `FF` 落在前一块、`D9` 落在后一块，两块都找不到 → 返回 False → `image_looks_truncated=True`
  → 合法 JPEG 被当残骸、在 skip 策略下被覆盖。
- 触发门槛：EOI 恰好落在 64KB 回扫边界（A5 是我人为构造的），真实文件概率极低。
- 修法：块间留 1 字节重叠（读 `end-start+1`，或把上一块最后一字节带到下一块一起匹配）。

### P2（观察，已有提示）：分批保存中途失败留下半份队列
- B7b 实测：第 2 批失败后服务端只剩第一整批 500 条。前端 `saveQueue().catch` 会提示
  「文件列表保存失败…（刷新后可能只恢复部分）」，**有如实告知**，但 `queueDirty=false` 丢弃了自动重试机会。
- 建议：catch 里改 `queueDirty=true`，让下一次状态变化触发重存；或失败时回退为整份重发。

### 已知取舍（工程师文档已声明，非本轮缺陷）
- C04「保留两份」同参数重复点压缩会 skipped（续传优先，1.7.1 起的设计）；
- `_jpeg_size` 512KB 元数据上限；EOI 回扫 8MB 预算；队列 20000 上限；fnpack 打包不可字节重现。

## 七、最终判定

- **智能路由判定：`NoOne`（放行）** —— 我上一轮定的三项放行标准全部满足，用户报的两个故障
  （网关 504 / 后台停不下来）与上一轮 P0/P1 均经独立复验证实修复，1.7.1 能力无回退，包与文档自洽。
- **是否可以交付给用户安装：可以。** 建议交付时把第六节的 P1/P2 作为 Known Issues 随版本说明一并告知；
  若团队选择"先修完再发布"，则路由应改为 Engineer，改动量仅上面几行。
- 本轮是第 2 轮（最后一轮），不再开第 3 轮；所有脚本与原始日志都在 `_test/qa/`，可重跑。
