# Caesium for fnOS

**飞牛 fnOS（飞牛私有云 NAS）上的原生图片压缩工具** —— 网页版 Caesium Image Compressor

在 NAS 本机完成图片压缩，界面一比一复刻 Caesium 桌面版：左侧文件列表（名称 / 分辨率 / 大小 / 已节省 / 状态），右侧三标签（压缩选项 / 图片尺寸 / 输出），底部原图与压缩后对比预览。

当前版本：**v1.7.0**（micro_app 网关模式，原生目录授权）

---

## 功能特性

### 压缩能力
- 支持格式：**JPG / PNG / WebP / GIF / TIFF / BMP / AVIF**
- 三种模式：
  - **图片质量**：按最终输出格式独立设置质量（JPEG 质量 / PNG 质量+优化级别 / WebP 质量 / TIFF 压缩算法）
  - **无损压缩**：PNG / WebP 等无损优化
  - **目标大小**：压缩到指定 KB 以内（尽力而为）
- JPEG 高级：色度二次采样（4:4:4 / 4:2:2 / 4:2:0 / 4:1:1）、渐进式 JPEG
- PNG 高级：优化级别（0–6）、Zopfli 深度优化
- 保留 EXIF 元数据开关

### 图片尺寸
- 不调整 / 最长边 / 指定宽度 / 指定高度 / 按百分比
- 等比缩放，保持原始宽高比；「不放大图片」保护小图

### 输出
- 选择输出目录（飞牛系统原生目录选择 + 授权，只显示已授权文件夹）
- 保留目录结构
- 导出格式：保持原格式 / JPEG / PNG / WebP / TIFF
- 保留文件时间
- 覆盖策略：输出到原文件夹时提示风险

### 使用体验
- 文件列表自动持久化（关闭页面不丢失，可恢复上次添加的文件）
- 压缩过程中锁定设置、文件列表保持可操作
- 「再压缩失败」：一键只重试失败项
- 原图 / 压缩后对比预览
- 压缩任务日志

### 授权与安全
- 基于飞牛官方 **micro_app 网关模式** + 官方授权 SDK（`@trimjs/web-app`）
- 添加文件夹 / 添加文件 / 选择输出目录均调用**系统原生授权选择器**，只显示已授权目录
- 应用只访问你在飞牛中授权的目录，文件全程在 NAS 本机处理，不上传任何第三方服务
- 卸载时可选择保留或彻底删除应用数据

---

## 安装

### 环境要求
- 飞牛 fnOS **1.2.0401 或更高**（micro_app 网关特性要求）
- 选择与 NAS CPU 架构匹配的安装包：`x86_64`（Intel/AMD）或 `arm64`（ARM）

### 安装步骤
1. 从 Releases 下载最新 `.fpk` 安装包
2. 进入飞牛 fnOS「应用中心 → 手动安装」，选择 FPK 文件
3. 安装完成后，从 fnOS 桌面打开 **Caesium**
4. 首次使用点击「添加文件夹」或顶部授权横幅，在弹出的系统窗口中选择要授权的目录即可

### 命令行安装（可选）
```bash
appcenter-cli stop caesium || true
appcenter-cli uninstall caesium || true
appcenter-cli install-fpk /tmp/caesium_1.7.0_x86_64.fpk
appcenter-cli start caesium
```

---

## 使用说明

1. 点击「添加文件夹」→ 系统原生窗口选择 NAS 中的图片目录（自动授权并扫描图片）
2. 或在「压缩选项 / 图片尺寸 / 输出」中按需调整参数
3. 选择输出目录（默认可勾选「保留目录结构」）
4. 点击「压缩」，底部状态栏显示进度；压缩完成后文件列表显示节省百分比
5. 有失败项时，可点击「再压缩失败」仅重试失败文件

---

## 技术架构

| 层级 | 技术 |
|------|------|
| 应用封装 | 飞牛 fnOS FPK、micro_app 网关模式（unix socket）、生命周期脚本 |
| 后端服务 | Python 3 标准库 HTTP 服务（单文件 `server.py`，含全部前端） |
| 压缩引擎 | caesiumclt（Rust，基于 [caesium-image-compressor](https://github.com/Lymphatus/caesium-image-compressor)） |
| 授权桥接 | 飞牛官方 SDK `@trimjs/web-app`（随包分发，原生目录选择） |
| 前端界面 | 原生 HTML / CSS / JavaScript（内嵌于 server.py） |

### 关键设计
- **micro_app 网关模式**：应用监听 `app.sock`（unix socket），由飞牛统一网关转发 `/app/caesium/` 请求；该模式是官方授权 SDK 正常工作的前提
- **授权目录**：通过 `TRIM_DATA_ACCESSIBLE_PATHS` 环境变量 + 历史记录管理可访问目录，只展示已授权路径
- **打包双架构**：manifest `platform=x86` / `platform=arm` 分别产出 `x86_64` / `arm64` 安装包

---

## 目录结构

```
caesium/
├── manifest              # FPK 应用清单（micro_app、版本、权限）
├── app/
│   ├── server.py         # 后端服务 + 全部前端（单文件）
│   ├── caesiumclt        # 压缩引擎二进制（Linux x86_64 / aarch64）
│   ├── ui/
│   │   ├── config        # 桌面入口配置（网关模式）
│   │   └── images/       # 应用图标
│   └── www/
│       └── trimjs-web-app.js   # 飞牛官方授权 SDK（@trimjs/web-app）
├── cmd/                  # 生命周期脚本（start / stop / status / log）
├── config/
│   ├── privilege         # 运行权限（package 用户）
│   └── resource          # api-scope：宿主 API 权限声明
├── wizard/               # 安装 / 卸载向导
└── ICON.PNG / ICON_256.PNG
```

---

## 本地开发与测试

> **引擎二进制说明**：本仓库不包含 `caesiumclt` 引擎二进制（约 6 MB，双架构）。编译好的引擎随 FPK 安装包在 Releases 分发；如需自行编译，请基于 [caesium-image-compressor](https://github.com/Lymphatus/caesium-image-compressor) 交叉编译 Linux x86_64 / aarch64 版本（含 `--format`、`--keep-dates`、`--tiff-algorithm` 等扩展参数），放入 `app/` 目录即可。

### 启动（TCP 模式，便于调试）
```bash
python3 app/server.py --port 8390 --workdir ./var
```

### 测试
- `_test/mock_clt.c`：mock 压缩引擎（winlibs gcc 编译为 caesiumclt.exe）
- `_test/e2e_test170.py`：E2E 回归（API + 网关前缀路由）
- `_test/verify_fpk_170.py`：FPK 解包校验（manifest / 引擎架构 / SDK）

### 打包
```bash
fnpack build --directory caesium
```
- `platform=x86` + x86_64 引擎 → `caesium_<版本>_x86_64.fpk`
- `platform=arm` + aarch64 引擎 → `caesium_<版本>_arm64.fpk`

---

## 许可证

- 本项目代码：**Apache License 2.0**（见 [LICENSE](LICENSE)）
- 压缩引擎基于 [caesium-image-compressor](https://github.com/Lymphatus/caesium-image-compressor) 构建
- 飞牛官方授权 SDK（`@trimjs/web-app`）按飞牛官方许可分发

本项目为第三方开源项目，与飞牛官方无隶属关系。
