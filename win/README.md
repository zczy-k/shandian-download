# 闪电全能下载 · Windows 版

macOS 原版是 SwiftUI 壳（11082 行）+ Gopeed 内核 + yt-dlp。SwiftUI 在 Windows 上没有实现，
所以这里重写壳，内核与 yt-dlp 直接复用：两端共用同一套 Gopeed REST API 与任务库格式。

## 安装

从 [Releases](../../releases/tag/win-latest) 下载 `Shandian-Windows-Setup-*.exe`，双击安装。

安装包自带：

| 组件 | 说明 |
|---|---|
| `ShandianDownload.exe` | 界面与本地服务 |
| `tools/gopeed.exe` | 打过补丁的无头内核（Actions 用 `engine/shandian-gopeed-v1.9.3.patch` 编译） |
| `tools/yt-dlp.exe` | 视频站下载 |

ffmpeg 体积大（约 80 MB），不打包，需要时在设置里点「安装 / 检查」按需下载。

## 与原版功能对照

| 功能 | macOS | Windows | 说明 |
|---|---|---|---|
| 磁力 / 种子 / ed2k | ✓ | ✓ | 含 tracker 注入、固定端口 6881、btih 长度校验 |
| HTTP 直链多连接 | ✓ | ✓ | 内核 8 连接、429/5xx 退避 |
| YouTube / B 站 / 抖音 / 小红书 / 微博 / 快手 / 西瓜 / Vimeo / X | ✓ | ✓ | yt-dlp，4 路分片并发 |
| 豆瓣预告片、m3u8 / mpd | ✓ | ✓ | 链接识别规则与原版一致 |
| 磁力解析后选文件 | ✓ | ✓ | 解析出文件列表后弹窗勾选 |
| 浏览器扩展网关 :6800 | ✓ | ✓ | aria2 RPC + `/extension/*` + Gopeed 代理 |
| LinkSwift 网盘直链 | ✓ | ✓ | aria2 RPC，密钥见设置 →「浏览器扩展」 |
| 剪贴板监听 | ✓ | ✓ | 复制链接后界面顶部弹提示 |
| 分类目录（电影 / 视频 / 其他） | ✓ | ✓ | 按链接类型自动落目录，可分别设置 |
| 记住上次保存位置 | ✓ | ✓ | 三类各记一份 |
| 同时下载数 | ✓ | ✓ | 1 / 2 / 3 / 5 / 8 / 10 |
| 下载限速 | ✓ | ✓ | 内核 `extra.shandianDownloadLimit` |
| 定时下载 | ✓ | ✓ | 时段外自动暂停，到点继续 |
| 完成通知 + 提示音 | ✓ | ✓ | Windows Toast + 系统提示音 |
| 回收站 | ✓ | ✓ | 移除的任务保留记录，可重新下载 |
| 诊断导出 | ✓ | ✓ | 不含路径、片名、链接、令牌 |
| 检查更新 | ✓ | ✓ | 查 `win-latest` Release |
| 开机自启 | ✓ | ✓ | 写 `HKCU\...\Run` |
| 阻止下载时睡眠 | ✓ | ✓ | `SetThreadExecutionState` |
| `shandian://` 协议投递 | ✓ | ✓ | 注册到 `HKCU\Software\Classes` |
| 中英双语 | ✓ | 部分 | 设置项已保留，界面文案待补 |
| 边下边播 | ✓ | — | 内核 `/play` 接口可用，播放器未接 |
| 资料库缩略图 | ✓ | — | 需要视频帧抽取 |
| PikPak 云盘离线 | ✓ | — | API 可移植，登录流程未做 |
| 百度网盘分享 | ✓ | — | 依赖 bdpan 命令行 |
| 飞书文档音视频 | ✓ | — | 依赖 lark-cli |
| 微信视频号 | ✓ | — | 依赖扩展侧解析 + 腾讯元宝登录 |
| 迅雷桥接 | ✓ | — | macOS 用 AppleScript 控制迅雷 App，Windows 迅雷无命令行接口 |
| MCP 服务 | ✓ | — | 待接 |

## 浏览器扩展

1. 打开「闪电全能下载 → 设置 → 浏览器扩展」，复制密钥
2. 在 Chrome / Edge 装原版的完整版扩展（`browser-extension/` 目录，开发者模式加载）
3. 扩展会自动连 `127.0.0.1:6800`

网关只接受三个来源：完整版扩展、Chrome 商店版、Edge 商店版。商店版只放行
`/extension/ping` 和 `/extension/add`，磁力、视频、批量、飞书等接口只给完整版。

## 数据位置

| 内容 | 路径 |
|---|---|
| 配置 | `%LOCALAPPDATA%\ShandianDownload\config.json` |
| 引擎与任务库 | `%LOCALAPPDATA%\ShandianDownload\data\engine\` |
| 按需下载的工具 | `%LOCALAPPDATA%\ShandianDownload\tools\` |
| 日志 | `%LOCALAPPDATA%\ShandianDownload\app.log` |

## 从源码构建

```bash
cd win
python -m pip install pyinstaller pillow
python -c "from PIL import Image; Image.open('../docs/images/icon.png').convert('RGBA').save('app.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
python -m PyInstaller shandian_win.spec --noconfirm
```

内核需要单独编译，见 `.github/workflows/windows.yml` 的「编译打过补丁的 Gopeed 内核」步骤。

开发时直接跑：

```bash
python shandian_win.py
```

## 已知限制

- Gopeed 官方 Windows release 是 GUI 版，不接受 `-A/-P/-T` 无头参数，必须用补丁源码编译。
- 磁力冷门资源长时间 0 速度属正常，界面会显示「解析元数据中」。
- 无头内核以 `CGO_ENABLED=0` 编译，BT 走纯 Go uTP，极端网络下连接数不如 macOS 的 libutp 版本。
