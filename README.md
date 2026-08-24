# 声迹

声迹是一款面向 B站视频和番剧页面的逐句歌词练习扩展。普通用户只需安装浏览器扩展；Faster-Whisper 识别服务由项目维护者部署到服务器，扩展通过 HTTPS 调用该服务。

## 架构

- 用户侧：`browser-extension/`，负责播放器交互、歌词显示、练习和浏览器本地数据保存。
- 服务侧：FastAPI + Faster-Whisper，负责下载所选音频片段、语音识别、歌词搜索与时间轴校准。
- 发布包：只包含浏览器扩展和用户安装说明，不包含 Python、模型或服务启动脚本。

## 主要功能

- 通过声迹服务进行 Faster-Whisper 音频识别
- 优先匹配 LRCLIB 同步歌词
- 无可靠同步歌词时回退到网页歌词或模型识别结果
- 支持 B站普通视频字幕
- 支持手动粘贴和导入 LRC
- 点击歌词跳转、逐句暂停、单句循环和重复播放
- 歌词文字及起止时间微调
- 按视频页面保存歌词、修改结果和掌握状态

## 项目结构

```text
sing reactor extension/
├─ browser-extension/     Chrome / Edge 扩展源码
├─ server.py              FastAPI 识别服务
├─ app_config.py          服务端环境配置
├─ requirements.txt       服务端 Python 依赖
├─ lyrics_config.json     歌词搜索、筛选和同步配置
├─ INSTALL.md             普通用户安装说明
├─ SERVER_DEPLOY.md       服务维护者部署说明
└─ build-release.ps1      扩展发布包生成脚本
```

## 生成用户安装包

先准备一个已经部署并可通过 HTTPS 访问的服务地址，然后运行：

```powershell
.\build-release.ps1 -ApiBase "https://api.example.com"
```

脚本会在 `dist/` 中生成：

- `sing-reactor-extension-<version>/`：可直接加载的扩展目录。
- `sing-reactor-extension-<version>.zip`：发给普通用户的扩展压缩包。

打包脚本会将服务地址写入生成包的 `config.js`，并把对应 HTTPS 源加入 `manifest.json` 的 `host_permissions`。用户无需安装 Python、FFmpeg 或启动后台服务。

## 用户安装与使用

用户按照 [INSTALL.md](INSTALL.md) 解压并加载扩展。打开 B站视频或番剧播放页面后，设置歌曲范围并点击“获取歌词”即可。

## 部署识别服务

服务器需要 Python 3.10+、FFmpeg，并能访问 B站及歌词来源。完整部署步骤和环境变量见 [SERVER_DEPLOY.md](SERVER_DEPLOY.md)。

## 本地开发调试

本节仅供开发者使用。

1. 安装 Python 3.10+、FFmpeg 和 `requirements.txt` 中的依赖。
2. 使用 `python server.py` 启动开发服务，默认监听 `127.0.0.1:8765`。
3. 使用可访问的 HTTPS 测试地址打包扩展，或按开发环境调整 `browser-extension/config.js` 与清单权限。
4. 在 `chrome://extensions` 中加载 `browser-extension/`。

服务端健康检查为 `/health`。生产环境应使用 HTTPS 反向代理，并限制允许访问的扩展来源。

## 歌词来源与回退顺序

1. 服务使用 Whisper 识别音频并生成时间锚点。
2. 优先查询 LRCLIB 的结构化同步歌词。
3. LRCLIB 结果不可靠时搜索普通网页歌词。
4. 无法可靠重建时间轴时，仅校正歌词文字。
5. 所有联网校正均不可靠时，保留 Whisper 识别结果。

结构化 LRC 决定歌词行边界，Whisper 主要负责校时和质量验证。

## 配置

歌词搜索和同步参数位于 `lyrics_config.json`。服务监听地址、端口、扩展来源、API token、任务并发和缓存等通过环境变量配置，详见 [SERVER_DEPLOY.md](SERVER_DEPLOY.md)。

## 隐私说明

- 默认情况下，用户选择的视频片段会由配置好的声迹服务下载并处理。
- 服务只接受 `bilibili.com` 页面 URL，并可能访问 LRCLIB、搜索引擎和歌词网页。
- 歌词、时间调整和掌握状态保存在浏览器扩展的本地存储中。
- 只有开发者自行部署本地服务时，音频识别才会在开发者自己的机器上完成。
