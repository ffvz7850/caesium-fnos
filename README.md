# Caesium PC 1.9.1（单文件版）源代码包

本包是 **Caesium PC 1.9.1 单文件版**的完整可读源代码与构建材料，基于官方 1.9.0 单文件版
（PyInstaller onefile，Python 3.13 + pywebview/pythonnet）做**定向修复**后产出。

- 产品形态：单个 `CaesiumPC.exe`，双击启动，内置本地 HTTP 服务 + WebView2 原生窗口
- 修复来源：`PC-1.9.0-全面代码审查报告.md`（P0/P1 全部处理，P2 处理主要项）
- 引擎：官方 `caesium.dll`（未修改），本包 `app/caesium.dll` 与 1.9.0 发布包内文件一致

---

## 1. 目录结构

```
caesium-pc-1.9.1/
├─ README.md                 本文件
├─ launcher_native.py        原生窗口启动器源码（对应 PyInstaller 内 launcher_native）
├─ build_exe.py              二进制补丁式重打包脚本（只替换 2 个条目）
├─ test_fixed_server.py      后端回归测试（16 项，隔离临时目录）
└─ app/
   ├─ server.py              后端 + 内嵌前端（单文件，约 3790 行）
   ├─ caesium.dll            官方压缩引擎（未修改，供重打包使用）
   └─ ui/
      ├─ config              飞牛版遗留配置（PC 版不读取，保留以对齐原包结构）
      └─ images/             icon_64.png / icon_256.png
```

`app/server.py` 结构：

| 行段 | 内容 |
|---|---|
| 1–1823 | Python 后端：图像尺寸解析、结果持久化、任务与引擎、落盘、队列、扫描、HTTP 工具 |
| 1824–3361 | 内嵌 `PAGE` 字符串（HTML/CSS/JS 前端） |
| 3362–末尾 | `Handler` 全部路由（`/api/*`）+ `main()` |

---

## 2. 运行与数据目录

- 系统：Windows 10 1809+ / Windows 11，需 **WebView2 Runtime**（Win11 自带，Win10 需安装）
- 启动流程（`launcher_native.py`）：
  1. 取得单实例锁 `%LOCALAPPDATA%\CaesiumPC\instance.json`
  2. 生成一次性随机 token 写到 `%LOCALAPPDATA%\CaesiumPC\token`（仅当前用户可读）
  3. 按内容签名比对，把 `_MEIPASS/app` 释放/更新到 `%LOCALAPPDATA%\CaesiumPC\runtime\app`
  4. 以 `--host 127.0.0.1 --token ...` 起后端线程，`wait_ready` 后开 WebView2 窗口
  5. 关窗即退出进程（服务线程为 daemon）
- 用户数据全部在 `%LOCALAPPDATA%\CaesiumPC`：`results.json`、`queue.json`、`folders.json`、
  `excluded.json`、`preview/`、日志 `launcher.log`；删除该目录即可完全重置（会丢断点续传记录）

---

## 3. 重新打包

本包**不包含**原 1.9.0 exe（体积 16 MB，请使用你手上的官方发布包）。
`build_exe.py` 采用「只替换 CArchive 中两个条目」的方式，其余依赖字节原样保留：

```bat
python build_exe.py --orig-exe "D:\path\to\CaesiumPC-1.9.0-single\CaesiumPC.exe"
:: 产物：dist\CaesiumPC.exe
```

可选参数：`--server`、`--launcher`、`--out`。

产物自检（解包核对内嵌代码与工作副本一致）可参考 1.9.1 的审查流程：读取 exe 末尾
`MEI\x0c\x0b\x0a\x0b\x0e` cookie，解析 TOC，取出 `app\server.py` 比对 sha256。

---

## 4. 回归测试

```bat
python test_fixed_server.py
```

覆盖：无 token / 错 token / 坏 Host / 坏 Origin / `text/plain` 拒绝、页面 token 注入、
`/api/open` 拒绝文件与非白名单目录、`workers=0` 回退环境变量、VP8X `+1` 与 VP8L 解码、
16 线程 3200 条记录并发落盘、`use_queue` 数量不匹配 409、任务 single-flight 409。
当前结果：**16/16 通过**。

---

## 5. 1.9.1 修复清单

### P0 安全（阻断项）

1. **跨站请求可执行任意程序**
   - 原状：`/api/open` 把请求路径直接交给 `os.startfile()`，且后端不校验 `Origin`/`Host`/`Content-Type`；
     恶意网页可用 `text/plain` 简单请求（不触发预检）静默执行本机 `.exe/.cmd`。
   - 修复：
     - 全 API 要求 `X-Caesium-Token`，token 由启动器每次启动随机生成并注入页面；
     - 所有请求校验 `Host` ∈ {127.0.0.1, localhost}；有 `Origin` 时必须同源；
     - POST/PUT 强制 `Content-Type: application/json`，否则 415；
     - `/api/open` 拒绝非目录，且只允许打开已浏览/已保存目录或任务输出目录；
     - 默认监听地址由 `0.0.0.0` 改为 `127.0.0.1`。
2. **任意路径读取**：`/api/img`、`/api/fs/list` 增加授权根校验（盘符/已保存目录/任务输出）。

### P1 功能与数据

3. **升级不生效**：资源签名由「仅 dll mtime+size」改为 `server.py` + `caesium.dll` + `ui/` 内容哈希，
   新版不再复用旧 runtime。
4. **服务冒名 / 多实例**：`instance.json` 记录 pid、exe 路径、版本；复用前校验 token、进程存活、
   版本一致；重复启动弹窗提示，不再静默共用后端。
5. **`results.json` 落盘竞态**：`FLUSH_LOCK` + generation 防陈旧快照 + uuid 临时文件 + `fsync` +
   失败保留脏标记。
6. **任务/队列可被污染**：压缩任务 single-flight（已有运行任务返回 409）；队列读写加锁；
   `use_queue` 提交带 `expect_count`，与实际条数不符返回 409，前端自动回退逐条提交。
7. **取消语义**：取消后在途文件不再计入完成数，未开始条目回填，计数闭合。

### P2 性能与正确性

8. `workers=0`/`"0"` 正确回退 `CAESIUM_WORKERS` 环境变量。
9. 临时目录清理由「每文件一次 listdir」改为「每任务一次」，消除万级文件 O(n²) 扫描（原 1 万文件约多花 77 秒）。
10. 任务轮询改增量（`since` 游标），不再每 1.2 秒回传全量 results。
11. VP8X 尺寸补 `+1`，新增 VP8L 分支；`targetSize` / `resizeVal` 加入设置持久化。
12. 拖动质量滑块不再整页 `render()`（防抖 + 仅更新当前页）。
13. 版本号统一 `1.9.1`；`/api/status` 增加 pid/version/engine 字段；启动器崩溃时弹窗并写日志。
14. `do_GET` 增加异常兜底；错误响应不再回显完整异常文本。

---

## 6. 验证记录

| 项目 | 结果 |
|---|---|
| `python -m py_compile app/server.py launcher_native.py` | 通过 |
| 内嵌 JS `node --check` | 通过 |
| `test_fixed_server.py` | 16/16 通过 |
| 新 exe `--diag` | 正常输出环境诊断 |
| 新 exe `--no-window` 端到端 | `/api/status` 返回 `version=1.9.1 / engine=dll`；无 token 403、错 token 403、`text/plain` 415、坏 Host/Origin 403 |
| 内嵌代码一致性 | 解包所得 `app/server.py` 与源码 sha256 一致 |

发布产物 sha256（1.9.1）：

```
CaesiumPC.exe                  c4190f04d42263ef0a2a6ee477d694aabbb64db441d3c46c3c3d5c6e67127e1a
CaesiumPC-1.9.1-single-win64.zip  c3c8859942da8cce96d03662af2658ddd3b221527c8552be9dd62b5eb9e98dcc
```

---

## 7. 已知限制

- 未做真机 GUI 人工点击验收（自动化只覆盖 `--diag` 与 `--no-window` 端到端）。
- token 以明文存于 `%LOCALAPPDATA%\CaesiumPC\token`，依赖 Windows 用户目录 ACL；同用户下的
  恶意程序仍可读取——本地单用户应用的合理边界，未做 DPAPI 加密。
- 端口 43117 被占用时会顺延（43118–43156 或随机），此时浏览器 origin 改变，localStorage 中的
  前端设置读不到（服务端 `queue.json`/`results.json` 不受影响）。
- 关闭窗口会立即中断进行中的压缩；已落盘输出采用「临时目录 + `os.replace`」原子替换，不会产生半文件，
  未完成任务下次启动可续传。
- DLL 压缩调用无法被真正中断，取消在「当前文件完成」后生效。

## 8. 许可

见发布包内 `LICENSE`（Apache-2.0）。`caesium.dll` 为 Caesium 官方引擎，版权归其作者所有，未做修改。
