# Caesium for fnOS

**飞牛 fnOS（飞牛私有云 NAS）上的图片压缩应用** —— 网页版 Caesium Image Compressor

在 NAS 本机完成图片压缩，界面一比一复刻 Caesium 桌面版：左侧文件列表（名称 / 分辨率 / 大小 / 已节省 / 状态），右侧三标签（压缩选项 / 图片尺寸 / 输出），底部原图与压缩后对比预览。

当前版本：**v1.8.1**（多线程并行压缩，micro_app 网关模式，原生目录授权）

---

## 1.8.1 更新要点

### 多线程并行压缩（1.8.0 引入）
v1.7.5 的压缩是串行的，8 核 NAS 也只有 1 核在干活。1.8.0 起改为 worker 线程池，多个文件同时交给多个 `caesiumclt` 进程处理：

- 前端「输出」页新增**并行数**下拉：自动（用满 CPU，封顶 16）/ 1 / 2 / 4 / 8 / 16
- 顶部引擎状态显示核数与实际并行度，如「压缩引擎正常（8 核 / 并行 8）」
- 并行度**不进参数指纹**——调整并行数不会让已完成文件失效或触发重压
- 并发安全：原子落盘（`.caesium-tmp-*` → 校验 → `os.replace`）+ 路径锁，多线程下同一输出路径不会写出半个文件

### 1.8.1 修复（1 个 P0 + 3 个 P1）
| 级别 | 问题 | 修复 |
|---|---|---|
| P0 | `results.json` 并发落盘竞态（丢记录 / JSON 损坏） | 独立 `FLUSH_LOCK` 串行化 open→write→fsync→replace；每写者独有 `results.json.tmp.<uuid>`；generation 快照防陈旧覆盖；写失败保留脏标记并重试。800 条并发记录不再丢失 |
| P1-1 | `CAESIUM_WORKERS` 对前端"自动"档（`workers=0`）失效 | `0` / 负数 / 非数字 / 缺省一律回退环境变量；显式正数优先。优先级：显式正数 > 环境变量 > CPU 自动 |
| P1-2 | 取消无法中断在途 `caesiumclt` 进程 | `subprocess.run` 改为 `Popen + communicate`，按 task_id 登记进程；`/api/cancel` 定点 terminate，2 秒兜底 kill，只影响本任务 |
| P1-3 | 多任务叠加引擎进程无上限（OOM 风险） | 全局有界信号量 `PROCESS_BUDGET = min(cpu_count, 16)`，在途进程总数封顶 16 |

> 压缩引擎 `caesiumclt` 一个字节未改，压缩结果与 1.8.0 完全一致。

---

## 功能特性

### 压缩能力
- 支持格式：**JPG / PNG / WebP / GIF / TIFF / BMP / AVIF**
- 三种模式：**图片质量**（按输出格式独立设质量）/ **无损压缩** / **目标大小**
- JPEG 高级：色度二次采样（4:4:4 / 4:2:2 / 4:2:0 / 4:1:1）、渐进式 JPEG
- PNG 高级：优化级别（0–6）、Zopfli 深度优化
- 保留 EXIF 元数据、GIF 直通复制

### 图片尺寸
- 不调整 / 最长边 / 指定宽度 / 指定高度 / 按百分比
- 等比缩放，保持宽高比；「不放大图片」保护小图

### 输出
- 飞牛系统原生目录选择 + 授权（只显示已授权文件夹）
- 保留目录结构、导出格式（保持原格式 / JPEG / PNG / WebP / TIFF）、保留文件时间
- 三态策略：跳过已存在 / 覆盖 / 保留两份
- 断点续传：中断后接着压，已完成不重压
- 异步扫描可取消、列表分页（每页 5000）
- 文件列表自动持久化、「再压缩失败」一键重试失败项

### 使用体验
- 压缩中锁定设置、文件列表保持可操作
- 原图 / 压缩后对比预览、任务日志

---

## 安装

### 环境要求
- 飞牛 fnOS **1.1.3100 或更高**（统一网关；micro_app 网关模式需 ≥ 1.2.0401）
- 按 NAS 架构选择：`x86_64`（Intel/AMD）或 `arm64`（ARM）

### 安装步骤
1. 从 Releases 下载对应架构的 `.fpk`
2. 飞牛「应用中心 → 手动安装」选择 FPK
3. 从 fnOS 桌面打开 **Caesium**
4. 首次使用点击「添加文件夹」，在系统原生窗口中选择要授权的目录

从 1.7.x 直接升级即可（appname 不变），设置和历史压缩记录保留。

---

## 技术架构

| 层级 | 技术 |
|---|---|
| 应用封装 | 飞牛 fnOS FPK、micro_app 网关模式（unix socket + 网关前缀） |
| 后端服务 | Python 3 标准库 HTTP 服务（单文件 `server.py`，含全部前端） |
| 压缩引擎 | caesiumclt（Rust，基于 caesium-image-compressor） |
| 授权桥接 | 飞牛官方 SDK `@trimjs/web-app`（随包分发，原生目录选择） |
| 前端界面 | 原生 HTML / CSS / JavaScript（内嵌于 server.py） |
| 并发模型 | worker 线程池 + 全局进程预算（≤16 在途进程） |

---

## 目录结构

```
.
├─ caesium/                   x86_64 工程（打包 platform=x86）
│  ├─ manifest
│  ├─ app/
│  │  ├─ server.py            后端 + 全部前端（单文件）
│  │  ├─ caesiumclt           压缩引擎（不入库，见 Releases）
│  │  ├─ ui/config            桌面入口（网关模式）
│  │  ├─ ui/images/           图标
│  │  └─ www/trimjs-web-app.js 飞牛官方授权 SDK
│  ├─ cmd/                    生命周期脚本
│  ├─ config/                 privilege / resource（api-scope）
│  └─ wizard/                 安装 / 卸载向导
├─ caesium-arm/               arm64 工程（打包 platform=arm，结构同上）
├─ _test/                     回归测试与 QA
├─ docs/                      交接文档（HANDOVER-1.7.2.md）
├─ build_181_pkg.py           1.8.1 双架构打包脚本
├─ _verify_181.py             1.8.1 修复自验脚本
├─ _make_src_zip_181.py       源码包生成脚本
├─ 说明.md                     1.8.1 用户说明
└─ README.md                  本文件
```

> **引擎二进制说明**：本仓库不包含 `caesiumclt`（约 6 MB，双架构），随 FPK 在 Releases 分发。自行构建流程见 `build_181_pkg.py`。

---

## 本地开发与测试

```bash
python caesium/app/server.py --port 8390 --workdir ./var
```

测试命令：

```bash
python _test/test_threads.py        # 多线程测试（9 项）
python _test/test_unit.py           # 单元测试（54 项）
python _test/test_http.py           # HTTP 测试（15 项）
python _test/test_consistency.py    # 一致性审查（8 项）
python _test/mutate_threads.py      # 多线程变异测试（5/5 KILLED）
python _verify_181.py               # 1.8.1 修复自验（24 项）
```

### 打包
```bash
python build_181_pkg.py
```
- `platform=x86` + x86_64 引擎 → `caesium_1.8.1_x86_64.fpk`
- `platform=arm` + aarch64 引擎 → `caesium_1.8.1_arm64.fpk`

---

## 许可证

- 本项目代码：**Apache-2.0**（见 [LICENSE](LICENSE)）
- 压缩引擎来自 [caesium-image-compressor](https://github.com/Lymphatus/caesium-image-compressor)（Lymphatus，GPL-3.0），未修改
- 飞牛官方授权 SDK（`@trimjs/web-app`）按飞牛官方许可分发

本项目为第三方开源项目，与飞牛官方无隶属关系。
