# 交接：caesium for fnOS（**v1.7.4 已定稿交付，本文为 1.7.2 历史记录**）

> ✅✅✅✅ **最新状态（v1.7.4，2026-11-15）**：用户要求「GIF 不压缩、直接复制到输出目录一份」。
> 已实现：`format=keep` 且源是 `.gif` 时**不 spawn 引擎**，走 `_copy_through()` 直通复制（skip/overwrite/rename/same_folder 四路都遵守既有三态与原子落盘），
> 任务统计新增 `copied`（`total == ok + copied + skipped + fail`），`results.json` 记 `status=copied` 且 `result_is_done` 接受它（断点续传认识 GIF）；
> 前端新增「已复制」状态（节省率 `—`、完成提示「已复制 N 个（GIF 不压缩）」、日志标注、`继续压缩（已完成 N）` 计入 copied）。
> 最终交付：`dist/caesium_1.7.4_x86_64.fpk` **2 716 001 B** / sha256 `b1bc55feabcaa6ded7a69544ac33d8d8763b5e4064551316220fd0dc45861016` / md5 `b179dd719d8480b6786ebc064aa8ae7e`；
> `dist/caesium_1.7.4_arm64.fpk` **2 574 240 B** / sha256 `3135dfed8e06182b61377cde5b98949ab1f8b50565d2c439e54773682039988b` / md5 `b27e8a18b5d3c268c22234abb2a5247c`；
> 源码 sha `3ca7ce60af9393bf…417ae3`。详见 `dist/caesium-1.7.4-说明.md`（校验值表已与最终包逐项核对）。
> `run_all.py` 6 组全绿（单元 54 / HTTP 14 / 前端 34 / 变异 25 KILLED / 一致性 / 打包）；`dup_probe.js`、`qa_frontend_scan.js`、`qa_round2.py`、`qa_adv.py`、`qa_package_*` 全绿。
> ⚠️ **不要再重跑 `run_all.py` / `test_package.py`（会重打包改变哈希）**；1.7.1/1.7.2/1.7.3 历史交付物保留不动，`dist/` 里 1.7.4 各只有一份 x86 + 一份 arm。


> ✅✅✅ **最新状态（v1.7.3，2026-11-15）**：用户报「添加文件夹提示重复、像把不同文件夹的东西丢了」。
> 实测去重按完整 path 正确、文件没丢；已在 `PAGE` 里加**文件名下显示直接父目录名**（`.fdir`，`title` 仍为完整路径、表格列数/id/按钮文案不变），
> 并把 `finishScan()` 的「跳过 N 个重复」改为「N 个文件已在列表中（同一路径），未重复添加；如需全部重加请先点「清空」」（scanDup 不标红）。
> **v1.7.2 已交付用户，故升号 1.7.3**（manifest/desc、server_version、/api/status、docstring、启动日志、测试断言全部联动）。
> 最终交付：`dist/caesium_1.7.3_x86_64.fpk` **2 714 507 B** / sha256 `a4136de6fe9de8b34034e1d3d8f4e4af6fe1da2eefa08840e37e942b67ad6bf3` / md5 `df8b964981197f69a5cc19df9f721b10`；
> `dist/caesium_1.7.3_arm64.fpk` **2 572 416 B** / sha256 `e7eb3bc6fc47523ccce27e32a4af1df960cf984d9637c37bdef87ab4e00a2a51` / md5 `9b33920e84b828ca236aa056f713f54b`；
> 源码 sha `41b6757a62de9f37…b39354`。详见 `dist/caesium-1.7.3-说明.md`（校验值表已与最终包逐项核对）。
> `run_all.py` 6 组全绿（单元 48 / HTTP 14 / 前端 29 / 变异 24 KILLED / 一致性 / 打包），`dup_probe.js` 三场景 PASS，QA 各套件全绿。
> ⚠️ **不要再重跑 `run_all.py` / `test_package.py`（会重打包改变哈希）**；1.7.1 / 1.7.2 的历史交付物保留不动，`dist/` 里 1.7.3 各只有一份 x86 + 一份 arm。

> ✅✅ **1.7.2 最终状态（QA 第 2 轮遗留收尾完成，勿再改动代码/重打包）**
> 源码定稿：`caesium/app/server.py` = `caesium-arm/app/server.py` = sha256
> `459f1234c9ecff81cda4d78e7ea91a499909eb3c24167d59d751d67fbe866539`（136 441 B）。
> 三处遗留已修：`truncated=true` 时继续拉完已收录页（S10b 1500/1500，S10a 不误报）、
> EOI 回扫块间 1 字节滑窗 `chunk + carry`（A5 变绿、A8 I/O 预算不变）、
> 分批保存失败置 `queueDirty=true` 自动补存（B7b）。
> **最终交付包（当前 dist 里的真实字节，QA 将据此定点复核）**：
> `dist/caesium_1.7.2_x86_64.fpk` 2 713 913 B / sha256 `fbd619c10c5be2022547b90c13f28fa7801f254bdb7e7b850069ba8f85e4c023` / md5 `eba9ac02f55d5bdd6569b1e30c589884`；
> `dist/caesium_1.7.2_arm64.fpk` 2 571 625 B / sha256 `e6dd86fe09b6ebc3ec3b213bd7b861f259725bd67096165f72cb66c98377fb90` / md5 `505cdd9d5047a98979317f58d6404601`。
> `dist/caesium-1.7.2-说明.md` 校验值表与最终包逐项一致（qa_package_172.py R1 PASS）；`dist/caesium-1.7.2-src.zip` 已用最终源码重打（R2 PASS）。
> `python _test/mutate.py` 24/24 KILLED、0 SURVIVED、跑完 sha 还原 `459f1234…`；四组基础套件 一致性通过 / 单元 48 / HTTP 14 / 前端 27；`qa_frontend_scan.js` 13/13、`qa_round2.py` 13/13、`qa_adv.py` 30 PASS、`qa_package_172.py` 16/16、`qa_package_smoke.py` 8/8。
> ⚠️ **不要再跑 `run_all.py` / `test_package.py`（末尾会重新打包并改变哈希）**；`1.7.2` 从未交付用户，就地沿用同号，`dist/` 内只有一份 1.7.2 x86 + 一份 1.7.2 arm（另保留 4 个 1.7.1 历史交付物）。
> 下方 §一~§六 全部是当时的历史记录，其中的 sha / 套件数量 / 「未完成」描述**均已过期**，不要照着执行。

> ✅ 补记（第 1 轮收尾）：下方 §三/§四 描述的「半成品」状态已过期。
> P0（分页收尾丢文件 + 轮询互斥）/ P1（Motion Photo 回扫 EOI）/ P2（队列分批 POST、上限 20000）**三项均已在 `caesium/app/server.py`（sha256 `2fc2e186…b5ae664125`，134945 B）实现并全量验证**。
> `run_all.py` 6 组全绿（单元 47 / HTTP 14 / 前端 24 / 变异 22 KILLED / 一致性 / 打包）；`qa_adv.py` 30 PASS（C01d 变绿）；`qa_frontend_scan.js` 10/10（5000 张本地与 300ms RTT 均收满 5000）；`qa_package_172.py` 14/14。
> 最终交付见 `dist/caesium-1.7.2-说明.md`（含校验值）与两个 `dist/caesium_1.7.2_*.fpk`。`_hold/*.DO-NOT-INSTALL` 已删除。
> **不要按本文 §五「下次开工怎么做」再改一遍代码**；如需复核，直接跑上面那些命令即可。

时间：2026-11-15 02:20（用户休息，电脑关机）

## 一、为什么停在这里

工程师 `software-engineer-3` 这一轮**失败了**：`429 您的使用量已超出频率限制，将在 2026-11-15 18:20:34 UTC+8 重置，您也可以切换其他模型继续使用`。
跑了 9m55s，**改了源码但没验完、没打包、没回滚**。这不是代码问题，是账号限流。
→ 明早继续时：要么换模型，要么等 18:20 之后。别原地重试同一个模型。

## 二、最后可用版本 = **v1.7.1**

`dist/` 里现在只剩 4 个 1.7.1 交付物（均已完整验证）：
`caesium_1.7.1_x86_64.fpk` / `caesium_1.7.1_arm64.fpk` / `caesium-1.7.1-说明.md` / `caesium-1.7.1-src.zip`

**1.7.1 已知问题**：大目录扫描同步阻塞 → 飞牛网关 504 HTML → 前端 `Unexpected token '<'`，且服务端扫描线程不停。
临时规避：不要一次扫几千张以上的目录；卡住就应用中心停止/启动 Caesium。

## 三、1.7.2 的目标（QA 第 1 轮判定 Engineer 的两个修复）

1. **P0 分页收尾静默丢文件**（1.7.2 异步分页引入）：`PAGE` 里 `pullScanPage` 只要 `state!=='running'` 就 `finishScan`，没检查 `scanOffset < j.total`。实测 5000 张只收 4545、300ms RTT 稳定收 3500。**还没修**（当前源码里搜 `scanOffset < (j.total` = 0 处）。
   修法见 `docs/` 或本会话给工程师的消息：收尾前若还没拉完就 `return` 让下一 tick 继续；`finishScan` 加数量自检；`scanTick` 加 in-flight 互斥。
2. **P1 Motion Photo 被误判残骸**：EOI 距文件尾 >2KB 的 JPEG 被判残骸 → skip 策略下**覆盖用户原图**。**已实现**（`_jpeg_has_eoi` / `EOI_SCAN_MAX=8MB` 从尾向前 64KB 回扫）。
3. P2 队列分批保存（避免 5000 条时 POST 体超网关 1MB、且 2000 条上限与 50000 扫描上限不匹配）：**未实现**。

## 四、当前文件状态（重要）

| 路径 | sha256 | 说明 |
|---|---|---|
| `caesium/app/server.py` | `2fc2e186…625cb79b30b5ae664125` | **半成品**，134945 B，`py_compile` 通过；P1 已做、P0/P2 未做，**未跑测试、未打包** |
| `caesium-arm/app/server.py` | `146a052d…6fab5c1a6300e47e4e2d6efde586bd0e1` | QA 验过的 1.7.2 基线（含 P0 bug），可作对比基准 |
| `_hold/server-partial-1.7.2-20261115.py` | 同上 2fc2e186 | 半成品备份 |
| `_hold/caesium_1.7.2_{x86_64,arm64}.fpk.DO-NOT-INSTALL` | — | **含 P0 丢文件 bug 的包，绝对不要装** |

`dist/` 里那两个 1.7.2 包已被移进 `_hold/` 并加了 `.DO-NOT-INSTALL` 后缀，避免误装。

## 五、下次开工怎么做

1. 从 `_hold/server-partial-1.7.2-20261115.py` 接着改（P1 已完成，别重做）。
2. 补 P0（3 行）+ 轮询互斥 + P2 分批队列。
3. 按给工程师的自证清单跑：`run_all.py` 6 组、`qa_adv.py` 的 C01d、`qa_frontend_scan.js`、`qa_package_172.py`；重点盯「5000 张 + 300ms RTT 必须收满 5000」。
4. 全绿后**删掉 `_hold/` 里两个旧 1.7.2 包**、重打 x86/arm 到 `dist/`，再出说明.md 和 src.zip。
5. 1.7.2 从未交付给用户，可沿用同版本号，但必须确保 `dist/` 里只有最终那两个包。

## 六、其他

- arm 引擎来自本地 `D:\PCAPP\DBAO\caesium-fnos\caesium_1.7.0_arm64.fpk`（GitHub arm 资产下载一直超时）。
- fnpack：`/d/PCAPP/DBAO/GONGJU/fnpack.exe`；打包产物落在当前工作目录，不是 `-d` 目录。
- 工作区记忆见 `.workbuddy/memory/2026-11-15.md`。
