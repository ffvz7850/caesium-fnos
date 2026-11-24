# caesium for fnOS v1.7.1 — QA 独立回归验证报告

- 验证人：严过关（QA）
- 被测对象：`caesium/app/server.py`（2233 行，sha256 `dda56d00077283e5923218f9be9d151a453f6fd8ebd8e87c2ef8f04f56fefba9`）+ `manifest`（1.7.1）
- 交付产物：`dist/caesium_1.7.1_x86_64.fpk`（交付时快照 sha256 `34c7003d…86578`，2695727 B）
- 验证方式：干净状态重跑工程师套件 + 自建 28 条对抗用例 + 包级冒烟（用包内 server.py）+ 变异复核
- 复跑入口：`_test/qa/qa_adv.py`、`_test/qa/qa_package_smoke.py`、`_test/qa/qa_mutate.py`

## 一、智能路由判定：**Engineer（Alex）**

理由：工程师自带的 6 组套件确实全绿（我也重跑过，结果一致、"不是空转"），但**测试全绿与实际正确性有偏差**。我构造的对抗用例命中 1 个 P0 + 4 个 P1：

- 用户最关心的"闪退后接着压"在**被 kill 的那一刻正在压的那个文件**上会出问题：输出目录留下半截文件 → 重启后被 skip 判定当成"已存在"永久跳过 → 用户拿到损坏图片，界面还显示"已跳过"。
- 引擎非零退出码完全没有被检查，半成品被记成 `ok` 并写进 results.json（永久性误判）。
- "输出到原文件夹（覆盖原图）"+ 引擎中途崩溃 → 原图被截断（实测 34000 → 500 字节）。
- 两个任务并发压同一输出路径 → 文件内容被两个写者交错（实测一个 jpg 里混了两个 nonce），两个任务都报 ok。
- 交付包丢了 `LICENSE`（官方 v1.7.0 有）。

上述 P0/P1 都在 `compress_file` / `run_task` 的落盘逻辑里，一个"统一临时目录 + 原子 replace + 检查 returncode"的改动可以同时消掉前四条。

## 二、干净重跑（工程师套件，不改任何用例）

清理 `_test/build/` 后执行 `cd _test && python run_all.py`，原始结果：

```
== 一致性审查：PASS 全通过（JS 引用 82 个 id 全部存在；接口/签名/计数/版本一致）
== 后端单元测试：24 passed, 0 failed ==
== 真 HTTP 集成：10 passed, 0 failed ==
== 前端 jsdom：13 passed, 0 failed ==
== 变异测试：变异 12 个：KILLED 12 / SURVIVED 0 / 锚点失效 0 ==
== 打包校验通过 ==
6 组全部通过
```

结论：与工程师自述一致（数字对得上，不是复述——我这边独立跑出来的）。**但"全绿"没有覆盖下面第三节的场景**，这正是我需要补的地方。

## 三、对抗用例结果（28 条，PASS 17 / FAIL 11）
> 全部用 `_test/qa/qa_fake.cmd` 这个"更恶意"的假引擎（可半截写、可挂住、可分块慢写、可非零退出），服务端每次用全新 workdir 起。

| 编号 | 场景 | 期望 | 实际 | 结论 |
|---|---|---|---|---|
| C01a | 输出目录放用户自己的同名文件，skip 策略 | 跳过、不 spawn、不写归属记录、不改文件 | skipped=1 spawn=0 内容不变 record=None | PASS |
| C01b | 紧接着改 jpeg_quality 再压 | 用户文件仍不得被覆盖 | skipped=1 spawn=0 内容不变 | PASS |
| **C02** | 输出目录有**半截残骸**（本应用上次被打断留下的，非空、无记录） | 识别为半成品并重压 | **skipped=1 spawn=0，260 字节残骸原样保留** | **FAIL** |
| C03a | 已完成产物被用户删掉 | 重压 | ok=1 spawn=1 文件重建 | PASS |
| C03b | 产物被换成不同大小的别的文件 | 重压 | ok=1 spawn=1 | PASS |
| **C03c** | 产物被换成**相同大小**的别的文件 | 能识别（或至少如实说明局限） | **skipped=1 spawn=0，伪造内容原地不动** | **FAIL** |
| **C04** | rename 模式连压 5 次同一张图 | a.jpg / a (2)…a (5) | **只产出 a.jpg，第 2-5 次全部 skipped** | **FAIL** |
| C04b | rename + force 重压 5 次 | 每次新增副本、原图不变 | a (2)…a (6)，原图字节不变，零残留 | PASS |
| C05a | same_folder + exist_policy=skip | 仍按覆盖原图 | ok=1，原图被替换 | PASS |
| C05b | same_folder 同参数再点一次 | skipped，不再二次压缩 | skipped=1 内容不变 | PASS |
| C05c | same_folder 改参数再压 | 按新参数覆盖 | ok=1 q=40 生效 | PASS |
| **C06a** | 6 文件压到中途 `taskkill /F /T` 真杀（含引擎子树） | 输出目录只有完整文件 | **残留 `k0.jpg` 仅 777 字节** | **FAIL** |
| C06b | 重启后 results.json 是否把半成品记成已完成 | 不得有指向残缺文件的 ok 记录 | 记录数=0（记录本身没写错） | PASS |
| **C06c** | 重启后再点压缩 | 半截文件必须重压 | **ok=5 skipped=1，777 字节残骸被当"已存在"跳过** | **FAIL** |
| **C07** | 两个任务并发压同一输出文件（参数不同） | 最终文件是其中一次的完整产物 | **文件 5836800 B 里混了 2 个写者标记，两个任务都 ok=1** | **FAIL** |
| C08a | 取消后的计数口径 | done == ok+skipped+fail | total=6 done=2 ok=2 skipped=0 fail=0 | PASS |
| C08b | 取消后再点压缩 | 已完成 0 次 spawn | 已完成 2 个，本轮 spawn 4 次，重压已完成文件=[] | PASS |
| **C09** | 11 条畸形/越界请求 + `/api/img` 传目录 | 全 4xx/2xx，服务存活 | **`[]`、`files:"abc"`、`options:"str"` 三条连接被重置（无响应）** | **FAIL** |
| C10a | 静态：build_clt_cmd 读取的 option key ⊆ SIG_FIELDS | 无遗漏 | 缺失=[] | PASS |
| C10b | 动态：20 个内容参数逐个改动 | 每个都改变 sig；exist_policy/overwrite 不变 | 未改变 sig=[]；误改变=[] | PASS |
| C10c | 前端 collectOptions 发出的键 ⊆ SIG_FIELDS | 无遗漏 | 19 个键全部命中 | PASS |
| **C11a** | 引擎退出码 9 但留下 900 字节半成品 | fail=1，不写 ok 记录 | **ok=1，记录 status=ok out_size=900** | **FAIL** |
| **C11b** | 紧接着再压一次 | 必须重压 | **skipped=1 spawn=0，900 字节半成品永久生效** | **FAIL** |
| **C11c** | same_folder（覆盖原图）+ 引擎中途崩溃 | 原图完好 | **原图 34000 B → 500 B（截断，永久损坏）** | **FAIL** |
| **C11d** | **引擎直接失败（什么都不写）+ 目标路径上是上一次的旧产物，参数 q=90→30** | fail=1 | **ok=1，旧文件原地不动，记录却被更新成 q=30 的 ok** | **FAIL** |
| C12a | 原有 id 集合 | 只增不改 | 丢失=[]，新增 9 个（existPolicy/confirm*/recompressAllBtn） | PASS |
| C12b | 原有按钮/标签文案 | 一个不丢 | 丢失=[] | PASS |
| C12c | 是否新增原生 alert/confirm | 新增 0 处 | 基线 2 处 → 现在 1 处（无新增） | PASS |

### 关键 FAIL 的原始输出

真 kill 场景（C06，`taskkill /F /T` 杀服务 + 引擎子树，不是模拟）：

```
--- 真 kill：6 个文件，压到第 3 个时把服务+引擎整棵树强杀 → 重启 → 再压。 ---
  [FAIL] C06a  压到中途强杀（含引擎子进程），观察输出目录残留
         实际: kill 前 done=0/6；残留={'k0.jpg': 777}
  [PASS] C06b  重启后 results.json 是否把「半成品」记成已完成
         实际: 记录数=0，指向残缺文件的=[]
  [FAIL] C06c  重启后再点压缩：半截文件会不会被当成「已完成」跳过
         实际: ok=5 skipped=1 fail=0；仍是 777 字节的残骸=['k0.jpg']；被误判 skipped=1
```

并发场景（C07）：

```
  [FAIL] C07  两个任务并发压同一个输出文件（连点两次压缩，参数不同）
         实际: task1=(done ok=1) task2=(done ok=1) 文件大小=5836800（合法尺寸 5836800 或 1228800）
               文件里出现 2 个写者标记=['<n689187-10048>', '<n689187-16436>'] spawn=2
```

引擎失败被当成功（C11）：

```
  [FAIL] C11a 引擎退出码 9（编码中途崩溃）但留下了 900 字节半成品
         实际: ok=1 fail=0 落盘大小=900 记录={'status': 'ok', 'out_size': 900}
  [FAIL] C11c same_folder（覆盖原图）+ 引擎中途崩溃
         实际: status=ok ok=1 原图 34000 字节 → 500 字节
  [FAIL] C11d 引擎直接失败（未写任何文件）+ 目标路径上是上一次的旧产物（改 q=90→30 重压）
         实际: ok=1 fail=0 spawn=1 文件内容未变=True 记录 status=ok（参数已是 q=30）
```
C11d 这条**不依赖"引擎是否原子落盘"的任何假设**：只要某个文件压缩失败（源图损坏 / 磁盘满 / 引擎崩），
而目标路径上正好有上一次的产物，用户就会看到"成功"、实际文件一个字节都没变，
且记录被刷成新参数的 ok → 以后永远跳过。这是最容易在真实 NAS 上复现的一条。

畸形请求（C09）根因（服务端 stderr 原文）：

```
  File "...\server.py", line 2118, in do_POST
    files = req.get("files", [])
AttributeError: 'list' object has no attribute 'get'
---
  File "...\server.py", line 2128, in do_POST
    fp = str(f.get("path", ""))
AttributeError: 'str' object has no attribute 'get'
```
→ 客户端看到的是连接被重置（urllib `RemoteDisconnected`），前端只会显示"创建任务失败"。

## 四、包级验证（用**包内那份** server.py 跑冒烟，8 条 / FAIL 1）

| 编号 | 检查 | 实际 | 结论 |
|---|---|---|---|
| P01 | fpk 顶层清单 | ICON×2/app.tgz/cmd/config/manifest/wizard | PASS |
| P02 | 包内 server.py 与工作树源码同源 | 两边都 `dda56d00077283e5…` | PASS |
| P03 | manifest | version=1.7.1 platform=x86 checksum=134a094b… = md5(app.tgz)（与官方约定一致） | PASS |
| **P04** | 与官方 v1.7.0 包顶层清单比对 | **丢失 `['LICENSE']`**；内容变化=['app.tgz','manifest'] | **FAIL** |
| P05 | app.tgz 内逐个 sha256（对比官方 1.7.0） | 少=[] 多=[] 变化=['server.py']；caesiumclt `ebe954f7…` 逐字节一致 | PASS |
| P06 | 新文案真的进包 | 已跳过/继续压缩（已完成/全部重压/已存在文件/断点续传/results.json/exist_policy/skipped 全部命中 | PASS |
| P07 | 包内 server.py 以 TCP 起服务 | `/api/status` = {'ok':True,'clt':True,'version':'1.7.1'} | PASS |
| P08 | 包内 server.py 真压 + 二次压 | 第一次 ok=1 落盘；第二次 skipped=1；累计 spawn=1 | PASS |

补充事实（交付口径）：

- **打包不可字节重现**：交付包 sha256 `34c7003d…`（2695727 B），我从源码重新 `fnpack` 得到 `4b9818c3…`（2695787 B）。已逐文件比对 `app.tgz` 内 12 个文件 sha256 —— **内容完全一致**，差异只来自 tar 里写入的 mtime 与本包 checksum 字段。→ 交付/公告里的 sha256 必须以最终交付的那份为准，不能沿用旧值。
- 官方 v1.7.0 包顶层有 `LICENSE`（Apache-2.0，9303 B），本次 1.7.1 包没有；`caesium/` 工程目录里就从来没放。Apache-2.0 再分发要求随附许可全文，属合规缺口。

## 五、变异测试复核（D）

1. 工程师的 `python mutate.py` 我自己重跑：**12/12 KILLED，锚点失效 0，还原后 sha256 一致**，且基线 unit 24 / frontend 13 全绿 —— 变异测试是真的，不是空转。
2. 我自己另做 5 个变异（`_test/qa/qa_mutate.py`），**4 KILLED / 1 SURVIVED**：

| 变异 | 说明 | 结论 |
|---|---|---|
| M1 | skip 判定去掉 `getsize>0`（0 字节残骸也算完成） | KILLED（test_skip_policy_ignores_empty_file） |
| M2 | run_task 去掉 `has_record(src)` 判断 | KILLED（test_changed_quality_forces_recompress） |
| M3 | `status not in ("ok","skipped")` 改成恒假 | KILLED（test_result_is_done_rules） |
| M4 | `_claim_target` 去掉 `O_EXCL` | KILLED（rename 两个用例） |
| **M5** | **反向变异：给 compress_file 补上"非零退出即失败"** | **SURVIVED（24 passed 全绿）** |

M5 的 SURVIVED 是**覆盖盲区证据**：现有 24 条单测里没有任何一条约束"引擎退出码"的语义，所以"引擎崩了但留下半截文件"这条路径可以被任意实现而测试无感 —— 这正是 C11 漏洞长期存在的原因。

## 六、问题分级与修复建议（给 Alex）

### P0-1 输出落盘非原子：闪退/引擎崩溃留下半截文件，重启后被 skip 永久跳过
- 证据：C02、C06a、C06c（真 kill）
- 位置：`compress_file()` L477-533（引擎直接写 `dst_dir`，`-O never/all` 都写最终路径）
- 影响：**直接命中用户诉求**（闪退 → 接着压）。用户看到"已跳过"，实际拿到的是一张残缺图；只有点「全部重压」才能救，但 UI 不会提示。
- 改法（与 rename 已有实现对齐，改动很小）：
  1. 三种策略统一先压到 `dst_dir/.caesium-tmp-<uuid>/`（`-O never` 即可，目录是空的，永远不冲突），成功后再 `os.replace(tmp_out, 最终路径)`；`finally` 里 `shutil.rmtree(tmp_dir, ignore_errors=True)`（`_compress_rename()` L536-566 已经是这个模式，把它的 tmp 逻辑抽成 `_encode_to_tmp()` 给三种策略复用）。
  2. 最终路径上就永远不会出现"半个文件"；被 kill 只会留下 `.caesium-tmp-*`（启动或下次压同目录时顺手清掉 1 天前的）。
  3. skip 判定保持现状（先算 out_path，存在且非空 → skipped，不 spawn）；因为最终路径只会有完整文件，这个判定才成立。

### P1-2 引擎非零退出码被忽略，失败被记成 ok（且写入 results.json 永久生效）
- 证据：C11a / C11b / **C11d**（最干净的一条：旧产物原地不动却报成功）；变异 M5 SURVIVED 说明无测试约束
- 位置：`compress_file()` L526-533（只看 `_pick_output` 是否找到文件，不看 `proc.returncode`）
- 影响：任何一次"引擎失败但目标路径有文件"（旧产物 / 半成品）都会：界面显示成功 → `record_result` 用新 sig 覆盖旧记录 → 之后永远 skipped。用户改了质量却没压，还以为压了。
- 改法：`proc, launch_err = _run_clt(cmd)` 之后加
  ```python
  if proc.returncode != 0:
      return False, None, _clt_error_text(proc), "error"
  ```
  并且在 `record_result` 之前确认"这次真的产出了文件"（用 tmp 落盘方案后，tmp 里没有文件就是失败）。
- 另外补两条单测（现在的套件没有）：引擎 `returncode!=0` + 已写出半截 → 必须 `status=error` 且不写 ok 记录；引擎 `returncode!=0` + 目标已有旧文件 → 必须 `fail`，且不得更新记录。

### P1-3 same_folder 覆盖原图 + 引擎中途崩溃 → 原图被截断
- 证据：C11c（34000 B → 500 B）
- 位置：同 P0-1（覆盖写目标=源文件）。这是 v1.7.0 就有的行为，但用户场景就是"闪退"，且覆盖原图意味着**不可恢复**。
- 改法：同 P0-1，`-o tmp` + `os.replace`，原图只在最后一步被整体替换；失败时原图完好。

### P1-4 两个任务并发压同一输出路径 → 内容交错，双方都报 ok
- 证据：C07（一个文件里出现 2 个写者标记）
- 位置：`compress_file()` 与 `run_task()` 之间没有 per-output 互斥；前端 `setLocked()` 只挡同页面重复点击（换浏览器窗口 / 双开 / 刷新后另一入口仍可提交）。
- 改法：全局 `_PATH_LOCKS: dict[str, threading.Lock]`（用 `_path_lock(out_path)` 取，`with` 包住"sorted检查 + 压 + replace"）；配合 P0-1 的 `os.replace`，即使漏锁最后也只会是"完整文件"而不是"交错文件"。

### P1-5 交付包缺 LICENSE（合规）
- 证据：P04 / P05（官方 1.7.0 顶层有 LICENSE，我们没了）
- 改法：把 `LICENSE` 放回 `caesium/` 根目录（`src/LICENSE` 与官方包里的 `LICENSE` 逐字节一致，`965a90ca…`，9303 B，直接拷），重新 `fnpack`；`test_package.py` 的"文件清单与官方基线完全一致"只比了 `app.tgz` 内部，建议把 **fpk 顶层清单**也纳入比对。

### P2-6 rename 模式连压 5 次只出 1 份
- 证据：C04（第 2-5 次全部 skipped="已跳过"）
- 说明：这是"续传优先"的必然结果（`result_is_done` 命中就跳过），但和下拉框"保留两份"的直觉冲突。
- 建议：**不要改逻辑**（续传正确性更重要），只改文案：rename 模式下 status=skipped 且已有 out 时，log/状态栏写"已跳过（如需再存一份请点「全部重压」）"。

### P2-7 同大小替换检测不到
- 证据：C03c（把产物换成同样大小的别的文件 → 仍判"已完成"）
- 说明：BRIEF 3.1 只要求"存在且大小相符"，所以按规格是对的；但可以低成本加强：记录里多存 `out_mtime` 或前 4KB 的 md5，判定时比一下。优先级低。

### P2-8 畸形请求打挂请求（不是服务）
- 证据：C09（`[]`、`files:"abc"`、`options:"str"` → 连接重置）
- 位置：`_read_json()` L1972-1976 不校验顶层类型；`/api/compress` L2118/L2124-2128 直接 `req.get(...)` / `f.get(...)`
- 改法：`_read_json` 末尾 `return obj if isinstance(obj, dict) else {}`；`opts = req.get("options") if isinstance(req.get("options"), dict) else {}`；`for f in files:` 前加 `if not isinstance(f, dict): continue`。服务本身不崩（线程级），但请求没响应。

### P2-9 打包不可字节重现
- 说明：内容一致、sha 不一致（tar mtime）。交付说明里的 sha256 要用最终包重算。

## 七、结论

- 工程师的核心设计（results.json 逐文件落盘 + sig 指纹 + 三态策略 + `run_task` 兜底 + `/api/img` 流式）**方向正确、实现干净**，sig 覆盖率、用户文件保护（C01）、断点续传（C06/C08）、取消与统计口径（C08a）、前端契约（C12）都过了。
- 但落盘这一层缺了"原子性"这一环，导致**用户最关心的"闪退"场景在最关键的那一个文件上会静默出错**（C02/C06/C11），并且失败会被持久化成"已完成"。
- 判定给 Engineer 修 P0-1 + P1-2/3/4/5；修完我做第 2 轮回归（`_test/qa/qa_adv.py` 全绿 + 工程师 6 组套件仍全绿即可放行）。P2 可进下一轮，不阻塞交付。
