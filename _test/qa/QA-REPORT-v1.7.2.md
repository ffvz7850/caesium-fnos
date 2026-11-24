# caesium for fnOS v1.7.2（扫描异步化）—— QA 独立回归验证报告

- 验证人：严过关（QA）
- 被测：`caesium/app/server.py` 2862 行，sha256 `146a052d26d4c8851c7505f1e3605746fab5c1a6300e47e4e2d6efde586bd0e1`；`manifest` 1.7.2
- 交付产物（我验的是交付那一刻的快照）：
  - `dist/caesium_1.7.2_x86_64.fpk` sha256 `7c1063978b1599a44abb7a3c4ba06c6a50f6a4d155732242e6a30dde8a045f75`（2710334 B）
  - `dist/caesium_1.7.2_arm64.fpk` sha256 `b7d69bf6b43174d8ceb5dd1112164b756b588376fb3d8970b9f8a04174ffdefe`（2567583 B）
- 可重跑：`_test/qa/qa_scan_api.py`、`qa_frontend_scan.js`、`qa_dim_regress.py`、`qa_adv.py`、`qa_package_172.py`、`qa_gw_stub.py`（网关桩）、`qa_io.py`（进程 I/O 计数）
- 日志：`_test/qa/artifacts/`（run_all_172_clean / qa_scan_172 / qa_frontend_172 / qa_adv_172 / qa_package_172）

## 一、智能路由判定：**Engineer（Alex）**

用户报的两个故障（网关 504 → `Unexpected token '<'`；后台"拼命读取"停不下来）**已实质解决并且我用量化证据确认了**：

- 经真网关桩（1s 超时）`POST /api/scan`（6000 文件）**32 ms 返回 JSON**，不再是 504 HTML；
- 取消后 **1.5 秒内进程读 0 字节、scanned 增 0**（取消前 1 秒读 936,649 字节 / +2032 项）；
- 5000 张图的尺寸解析：旧实现读 **8,396,800** 字节，新实现读 **73,728** 字节（113×）。

但本轮引入了一个**新的 P0**：扫描分页收尾时**静默丢尾页**——大目录扫完，列表里的文件数少于目录里真实文件数，且用户无从察觉。

- 确定性证据（stub 驱动真 PAGE）：服务端返回 `state=done, total=1500` + 一页 500 条 → 页面只入 500 条，**丢 1000 条**。
- 真实端到端（真 server.py + 真 5000 张图 + 真 PAGE）：本地连跑 3 次 = 5000 / **4545** / 5000；模拟 300 ms RTT（远程/弱网访问 NAS）= **3500**，稳定丢 1500 条。

## 二、干净重跑工程师套件（不改它的用例）

```
cd _test && rm -rf build build-arm && python run_all.py
== 一致性审查 PASS（含 v1.7.2 新增项）
== 后端单元 44 passed, 0 failed
== 真 HTTP   12 passed, 0 failed
== 前端 jsdom 20 passed, 0 failed
== 变异 20 个：KILLED 20 / SURVIVED 0 / 锚点失效 0
== 打包校验通过（x86 + arm64）
6 组全部通过
```
与工程师自述一致。变异测试是真的（20/20 全 KILLED，我核对过其中"落盘改回直写""/api/scan 退回同步"这些锚点确实对应关键分支）。

## 三、A. 真实故障场景复现（最高优先级）

| 编号 | 场景 | 期望 | 实际（原始输出） | 结论 |
|---|---|---|---|---|
| A1a | 经**真网关桩**（1 s 超时）`POST /api/scan`，6000 文件 | 毫秒级返回 JSON，不是 504 HTML | `状态=200 耗时=32 ms 响应头='{"scan_id": "6258971d8eec", "state": "running", "errors": []'` | PASS |
| A1b | 拿到 scan_id 后分页查进度 | running→done，total 增长 | 正常 | PASS |
| A2a | 取消**前**：1 秒内读盘量 + scanned 增长 | 都在涨 | `1 秒内 scanned += 2032；进程读 936649 字节` | PASS |
| A2b | 取消**后**：1.5 秒内读盘量 + scanned 增长 | 冻结 | `state=canceled 取消后 scanned += 0；1.5 秒内进程读 0 字节` | PASS |
| A3 | 连点 5 次「添加文件夹」 | 只有 1 个在跑 | 仍在增长=1 个，running=1 个，前 4 个 canceled 且不再增长 | PASS |
| S1 | 前端：`POST api/scan` 收到真 nginx 504 HTML | 人话提示，无 `Unexpected token` | 状态栏「扫描失败：服务未响应或超时，请稍后重试」，按钮恢复、无残留"取消扫描" | PASS |
| S2 | 前端：轮询中途收到 504 HTML | 收尾、停轮询、按钮恢复 | 1.2 秒内状态请求数不再增加 | PASS |
| S8 | **端到端**：真 server.py + 真网关桩 504 HTML → 真 PAGE | 人话提示 | 通过（同一断言集） | PASS |

> 网关桩 `_test/qa/qa_gw_stub.py` 返回的是真实 nginx 504 页面（`<html>\r\n<head><title>504 Gateway Time-out</title></head>…`），
> 与用户看到的 `"<html> <h"` 前缀一致；且"超时"时**不再等上游**（上游继续跑），与真实网关行为一致。

## 四、B. 扫描正确性与分页

| 编号 | 场景 | 期望 | 实际 | 结论 |
|---|---|---|---|---|
| B4 | 扫描**进行中**按 offset 翻页（5000 文件 / 页 200） | 不漏不重 | 页数 25，累计 5000，重条 0，漏条 0，与服务端最终列表完全一致 | PASS |
| B5a | `total` 语义 | 中途=已发现数，结束=最终数 | 中途 total ≤ 最终 total，结束时 = 3000 | PASS |
| B5b | offset 越界 / 负数 / limit 超大 | 空页且 next_offset 不倒退；负数当 0；limit 夹到 2000 | 越界 files=0 next_offset=999999；负数 offset=0；limit=999999 返回 ≤2000 | PASS |
| B6 | 取消 A → 发起 B | 结果不互相污染 | A=400 条 / B=120 条，交集 0 | PASS |
| B7 | 空目录 / 只有非图片 / 15 层嵌套 / 中文+emoji+200 字长名 / 断链符号链接 | `state` 必须落到 done，不卡 running | `state=done`，中文与 emoji 都收录，深 15 层静默跳过 | PASS |
| B8 | scan_id 不存在 / 缺参 / offset 垃圾值 / paths 是字符串或缺失 | 不许 5xx | 404 / 200 / 200 / 200 / 200(给 scan_id) / 200 | PASS |
| **S3** | **前端分页收尾**：服务端 `state=done, total=1500`，一页 500 条 | 列表 1500 条 | **列表 500 条，丢 1000 条** | **FAIL** |
| **S9** | **端到端**：5000 张真图（本地直连） | 5000 | 3 次：5000 / **4545** / 5000 | **FAIL（间发）** |
| **S9b** | **端到端**：5000 张真图 + 300 ms RTT（弱网） | 5000 | **3500，丢 1500** | **FAIL（稳定）** |

### 根因（`PAGE` 内 `pullScanPage`，L1931-1950）

```js
if(j.state==='running'){
  ...
  if(!j.truncated&&scanOffset<scanTotal&&depth<SCAN_MAX_PAGES)pullScanPage(depth+1);
  return;
}
finishScan(j);          // ← 只要这一帧不是 running 就立刻收尾，
                        //   完全没检查 scanOffset 是否还小于 total
```
服务端一页最多 500 条（`SCAN_PAGE=500`）。只要"收尾那一帧"客户端还落后 500 条以上，尾页就永远不会被拉，
而状态栏仍显示"添加 N 个文件"（N 是实际入列表的数量，用户没有参照系，看不出少了）。

触发条件（实测）：扫描吞吐 > 客户端排空速度（≈ `SCAN_MAX_PAGES×SCAN_PAGE / SCAN_TICK` ≈ 5000 条/秒），
或链路 RTT 偏高（300 ms 即可 100% 复现）。5000 张图、本地直连在我的机器上 3 次里就有 1 次触发。

### 修法（3 行，改 `pullScanPage`）

```js
if(j.state!=='running'){
  if(!j.truncated && scanOffset<(j.total||0)){       // 还没拉完，别收尾
    setStatus('正在整理扫描结果…（已收 '+scanOffset+'/'+(j.total||0)+'）');
    return;                                          // 下一个 tick（interval 还在跑）继续拉
  }
  finishScan(j); return;
}
```
同时建议 `finishScan` 里加一道自检：`if(!trunc && scanAdded+dup < total) 提示"结果可能不完整"`，避免以后再出静默不一致。

顺带（P2）：`scanTick` 每 500 ms 都发一轮，不和"上一条链路是否在飞"互斥，实测产生 **2000~4499 条重复拉取**
（`appendScanFiles` 去重兜住了，所以不影响正确性，但白花带宽/CPU，弱网下会进一步拖慢排空速度、放大上面的丢尾页）。

## 五、C. 尺寸解析回归

| 编号 | 场景 | 期望 | 实际 | 结论 |
|---|---|---|---|---|
| C8 | 新旧 `get_image_size` 对 **25 个**构造样本（PNG/GIF87/GIF89/BMP/WebP-VP8X/VP8/JPEG-SOF0/SOF1/SOF2/带 EXIF/多 APPn/0xFF 填充/无 EOI/只有 SOI/段长非法/0 字节/纯文本/随机字节） | 除声明的那条外完全一致，且不抛异常 | `不一致=[('jpeg_sof_after_600k', (1080,1980), None)]；抛异常=无` | PASS |
| C9 | SOF 被推到 600 KB 之后的 JPEG | 新实现返回 None（工程师声明的取舍） | 旧=(1080,1980) 新=None | PASS（仅核对行为一致，不判错） |
| C10a | 同一张 8 MB JPEG 解析一次读多少字节 | 旧 ≈8 MB；新 ≤128 KB | **旧读 8,396,800 字节；新读 73,728 字节**，尺寸结果相同 | PASS |
| C10b | 损坏/0 字节/只有 SOI/段长非法 | 不抛异常、不卡 | 每个样本 <50 ms | PASS |

## 六、D. 前端（jsdom 真跑 PAGE）

| 编号 | 场景 | 结论 |
|---|---|---|
| S1/S2/S8 | 504 HTML（桩 / 真网关 / 轮询中途）→ 人话提示 | PASS |
| S4 | 扫描中再点「添加」（连点 4 次） | 只发 1 次 `POST api/scan`，PASS |
| S5 | 扫描中两个「添加」禁用 + 出现「取消扫描」，完成后恢复 | PASS |
| S6 | 两页含重复条目 | 不出现重复行，PASS |
| S7 | 扫描中途改参数 / 切标签页 | 不重复、不误报失败、按钮正常恢复，PASS |
| S3/S9/S9b | 分页收尾 | **FAIL（见上）** |

静态门禁（`grep` PAGE 脚本）：
```
直接 .json() 出现次数 = 0            ← 所有解析都走 jsonSafe
fetch( 出现次数 = 20，未接 jsonSafe 的 4 处全部是 fire-and-forget（不解析响应体）
脚本内原生 alert/confirm 调用 = []   ← 无原生弹窗
```

## 七、E. 1.7.1 能力回归（31 条，PASS 29 / FAIL 2）

上一轮我打的 P0/P1 逐条复验，**全部修复确认**：

| 上轮问题 | 本轮用例 | 结果 |
|---|---|---|
| P0 落盘非原子（半截文件被永久跳过） | C02a / C06a / C06c（真 `taskkill /F /T` 打服务+引擎） | PASS（kill 后输出目录无残骸，重压正常） |
| P1 引擎退出码被忽略 | C11a / C11b / C11d | PASS（引擎 non-zero → error，旧产物不被当成功） |
| P1 same_folder 崩溃毁原图 | C11c | PASS（原图 34000 字节 → 34000 字节，未动） |
| P1 并发写同一路径 | C07 | PASS（零交错，两任务各自 ok） |
| P2 同大小替换识别不出 | C03c | PASS（加了 out_mtime） |
| P2 畸形请求打挂连接 | C09（11 条） | PASS（统一 400 JSON） |

| 编号 | 场景 | 期望 | 实际 | 结论 |
|---|---|---|---|---|
| **C01d** | 输出目录里是**用户自己的** JPEG，但 EOI 之后还跟着 5 KB 数据（手机 Motion Photo / 尾部元数据就是这个结构） | 仍应 skip，不能覆盖用户的照片 | `skipped=0 ok=1 spawn=1 用户文件未被覆盖=False` | **FAIL** |
| C04 | rename 模式连压 5 次 | 出 a (2)…a (5) | 只出 1 份，第 2-5 次 skipped（续传优先，设计取舍，1.7.1 起就如此） | FAIL（P2 遗留） |

### C01d 根因（`image_looks_truncated` L223-247）

```python
if head[:2] == JPEG_SOI:
    return JPEG_EOI not in tail      # tail 只取文件尾 2048 字节
```
只要 EOI 距离文件尾超过 2 KB（Motion Photo、部分相机的尾部附加数据），合法完整的图就被判成"残骸"，
于是 skip 放行、直接覆盖 —— 这违反 C01a 的语义边界（用户的文件必须被保护）。

### 修法

```python
def _jpeg_has_eoi(path, size):
    """从文件尾向前按 64KB 分块找 EOI；找到即停（不整文件读进内存）。"""
    with open(path, "rb") as f:
        end = size
        while end > 0:
            start = max(0, end - 65536)
            f.seek(start)
            if JPEG_EOI in f.read(end - start):
                return True
            end = start
    return False
# image_looks_truncated 里：
if head[:2] == JPEG_SOI:
    return not _jpeg_has_eoi(path, os.path.getsize(path))
```
正常图（EOI 在末尾）零额外开销；只有"尾部没有 EOI"的可疑文件才会向前多读，代价可接受。
（同理 PNG 的 IEND 也可以这样做，但 PNG 尾部附加数据的概率远低于 JPEG，可后置。）

## 八、F. 包级验证（14 条全过）

```
[x86] manifest version=1.7.2 platform=x86 checksum == md5(app.tgz)          PASS
[x86] 包内 server.py 与工作树源码同源（146a052d…）                          PASS
[x86] 顶层清单 = ['ICON.PNG','ICON_256.PNG','LICENSE','app.tgz','manifest']  PASS（LICENSE 已补回）
[x86] app.tgz 对比官方 1.7.0：少=[] 多=[] 变化=['server.py']；引擎 ebe954f7… 逐字节一致  PASS
[x86] 用包内 server.py 起服务 → version 1.7.2；/api/scan 3.3 ms 返回，30 个文件全拉到  PASS
[arm] manifest version=1.7.2 platform=arm checksum 自洽                     PASS
[arm] 包内 server.py 同源                                                   PASS
[arm] 顶层含 LICENSE                                                       PASS
[arm] app.tgz 对比 1.7.1_arm64：只有 server.py 变；引擎 6d092e17… 一致        PASS
[arm] 用包内 server.py 起服务 + 扫描 30 文件                                  PASS
```
说明两点（都不判错，但要写进交付口径）：
1. `caesium/manifest` 工作树里的 `checksum = dce6f824…` 是 v1.7.0 的值，是 fnpack 打包时重算覆盖的；**以包内 manifest 为准**（我核过包内值 = 该包 `app.tgz` 的 md5）。
2. 两平台 fpk 仍不可字节重现（tar 里带 mtime），重打会得到不同 sha256；交付说明里的 sha256 必须取自最终交付的那份。

## 九、问题分级与修复清单（给 Alex）

### P0-1 扫描分页收尾丢尾页（静默丢文件）
- 证据：S3（确定性 1500→500）、S9（本地 3 次里 1 次丢 455）、S9b（300 ms RTT 稳定丢 1500）
- 位置：`PAGE` 内 `pullScanPage()`，L1944-1947（`if(j.state==='running'){...} finishScan(j);`）
- 改法：见第四节三行补丁；`finishScan` 增加"数量自检"提示。
- 为什么是 P0：v1.7.2 的交付目标就是"大目录扫描"，现在大目录会**静默少文件**——用户比"明确报错"更难发现，直接导致压缩漏文件。

### P1-2 用户自己的 JPEG（EOI 之后带附加数据）被当成残骸覆盖
- 证据：C01d（`skipped=0 ok=1 spawn=1 用户文件未被覆盖=False`）
- 位置：`image_looks_truncated()` L238-240
- 改法：见第七节的 `_jpeg_has_eoi()` 反向分块查找。

### P2-3 rename 模式连压 5 次只出 1 份（遗留，非本轮引入）
- 建议维持"续传优先"，只改文案：rename 下 skipped 时提示"已跳过（如需再存一份请点「全部重压」）"。

### P2-4 轮询重叠造成 1.5~3 倍重复拉取
- 现象：S9 里 `跳过 2000~4499 个重复`；位置 `scanTick()`/`pullScanPage()` 无"在飞"互斥。
- 建议：`var scanInFlight=false;` 包住 `pullScanPage`，或把三个常量提成配置。

### P2-5 `save_queue(clean[-2000:])` 与 `SCAN_MAX_FILES=50000` 不匹配
- 现象：扫到 2 万条后刷新页面，只能恢复最后 2000 条（`/api/queue` L2710 的截断）。
- 建议：把上限跟 `SCAN_MAX_FILES` 对齐，或持久化时说明只存最近 N 条。

## 十、结论

- 用户报的两个故障（504 HTML 导致 `Unexpected token`、后台停不下来）**真解决了**，并且我用真网关桩 + 进程级 I/O 计数给出了可核对的数字（32 ms / 0 字节）。
- 1.7.1 的 P0/P1（原子落盘、退出码、覆盖原图崩溃、并发）**全部复验通过**，没有回退。
- 新引入的 P0（分页收尾丢尾页）必须修完才能交付——它让"大目录"这个主场景静默少文件。
- P1-2（Motion Photo 被覆盖）属于数据破坏，建议一并修（改动很小）。
- 修完我做第 2 轮：`qa_frontend_scan.js` 的 S3/S9/S9b 全绿 + `qa_adv.py` 的 C01d 变绿 + 工程师 6 组套件仍全绿 = 放行。
