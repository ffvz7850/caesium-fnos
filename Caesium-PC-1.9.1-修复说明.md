# Caesium PC 1.9.1 单文件版 —— 修复交付说明

## 1. 交付物

| 文件 | 大小 | sha256 |
|---|---:|---|
| `dist/CaesiumPC-1.9.1-single/CaesiumPC.exe` | 16,920,908 B | `c4190f04d42263ef0a2a6ee477d694aabbb64db441d3c46c3c3d5c6e67127e1a` |
| `dist/CaesiumPC-1.9.1-single-win64.zip` | 16,630,777 B | `c3c8859942da8cce96d03662af2658ddd3b221527c8552be9dd62b5eb9e98dcc` |
| `dist/caesium-pc-1.9.1-src.zip` | 74,531 B | `099f974f16d0166ad049dfae7951501f6bc0915ccd68967fa27a4aa7d418bc3a` |

Zip 内含 `CaesiumPC-1.9.1-single/{CaesiumPC.exe, 说明.txt, LICENSE}`。源码包含修复后的 `server.py`、重建的 `launcher_native.py`、补丁脚本、打包脚本与回归测试。

## 2. 修复清单（相对 1.9.0）

### P0
- **跨站请求执行任意程序**：本地 API 现在强制 `Content-Type: application/json`，校验 `X-Caesium-Token`、Host 与 Origin；`/api/open` 改为仅接受已浏览/保存过的目录或本次任务输出目录，并用 `explorer.exe` 打开，不再执行任意文件。
- token 在首次启动时随机生成，存放 `%LOCALAPPDATA%\CaesiumPC\token`，注入到页面 JS；所有前端 `fetch` 自动携带 token，图片预览用 URL 参数携带。
- 后端默认监听地址从 `0.0.0.0` 改为 `127.0.0.1`。

### P1
- **升级后继续跑旧代码**：资源释放签名纳入 `server.py`、`caesium.dll` 与 `ui/` 全量内容 hash；任何产品文件变化都会重新释放。
- **服务冒名复用**：启动器用 token + instance.json（pid/exe/version）+ 进程存活检查确认已有服务；无效则换端口启动新服务，弹窗提示重复启动。
- **崩溃静默**：资源释放、服务启动、WebView2/缺依赖失败全部弹 MessageBox 并写 `launcher.log`。
- **results.json 竞态**：`FLUSH_LOCK` + generation + uuid 临时文件 + `fsync` + 成功后才抵扣脏标记；写失败保留脏标记并打日志。
- **多任务抢状态**：`/api/compress` 在 `TASK_GATE` 内做 single-flight，已有 running/pending 任务返回 409；`TASKS` 只保留最近 8 个已完成任务。
- **队列引用漏压**：`use_queue` 必须带 `expect_count`，与服务端队列长度不符返回 409 并让前端回退逐条提交；队列读写加 `QUEUE_LOCK`。

### P2
- `workers=0` / `"0"` 正确回退 `CAESIUM_WORKERS` 环境变量。
- VP8X 尺寸 `+1` 修正；新增 VP8L 尺寸解析。
- `_sweep_tmp_dirs` 从每文件一次改为每任务一次，消除万级文件 O(n²) 目录扫描。
- `/api/tasks/<id>?since=N` 增量回传结果，前端用 `pollCursor` 合并；终态再拉全量生成日志。
- `targetSize`、`resizeVal` 纳入设置持久化；滑块拖动不再每次 `input` 整页重渲染。
- 版本标识统一为 1.9.1，`/api/status` 增加 `pid`/`exe` 供启动器校验。

## 3. 验证结果

- Python 语法编译通过；前端 JS `node --check` 通过。
- 后端回归 **16/16 通过**：
  - 无 token / 错 token / 坏 Host / 坏 Origin / `text/plain` 全部被拒；
  - 页面正确注入 token；
  - `/api/open` 对文件与非白名单目录返回 400/403；
  - `workers=0` 跟随 env=3；
  - VP8X 400×300 解析为 `(400,300)`，VP8L 同为 `(400,300)`；
  - 16 线程 × 200 条并发记录全部落盘（3200/3200）；
  - 队列 expect_count 不符返回 409；
  - 已有任务时新任务返回 409。
- 新 exe `--diag` 模式运行正常，PyInstaller TOC/PYZ 解包成功，内置 `server.py` 与工作副本 sha256 一致。
- 新 exe `--no-window --port 43120` 端到端启动成功：`/api/status` 返回 `version=1.9.1`、`engine=dll`、`pid`/`exe` 正确；无 token=403、`text/plain`=415、错 token=403、授权 clear=200。
- 打包自检：134 个 CArchive 条目完整，Dependencies（PYZ、python313.dll、WebView2 组件、caesium.dll）字节未改动。

## 4. 升级注意

- 覆盖旧 `CaesiumPC.exe` 后首次启动会重新释放 `%LOCALAPPDATA%\CaesiumPC\runtime\app` 下的 server.py 与 UI；旧的 `results.json` / `queue.json` 保留，断点续传继续有效。
- 旧的 `instance.json` 如因强杀残留，启动器会用 pid 存活检查自动识别并清理。
- 引擎 DLL 与 1.9.0 完全一致（8,886,032 B），压缩结果不变。
