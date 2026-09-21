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

## 歌词识别方案

服务采用“多来源召回 + 音频证据裁决 + 分级回退”的统一歌词引擎：

1. 归一化 B站总标题、分P标题、描述、歌手、版本和语言。
2. 将 B站字幕、网易云、LRCLIB 统一转换为歌词候选。
3. 通过歌名、歌手、时长、版本和来源质量进行候选评分。
4. 使用当前视频的 Whisper 音频锚点验证候选，而不是仅凭歌名采用歌词。
5. 根据固定偏移、保守线性模型或逐行重建校准时间轴。
6. 结构化来源均不可靠时，再回退到可信网页歌词和 Whisper 原始结果。

支持短片段识别、现场版、翻唱、版本时长差异和歌手不一致但音频强匹配的场景。

完整设计、候选格式、评分规则、时间轴模型、缓存和测试说明见 [LYRICS_PIPELINE.md](LYRICS_PIPELINE.md)。

## 配置

歌词搜索和同步参数位于 `lyrics_config.json`。服务监听地址、端口、扩展来源、API token、任务并发和缓存等通过环境变量配置，详见 [SERVER_DEPLOY.md](SERVER_DEPLOY.md)。

## 隐私说明

- 默认情况下，用户选择的视频片段会由配置好的声迹服务下载并处理。
- 服务只接受 `bilibili.com` 页面 URL，并可能访问 LRCLIB、搜索引擎和歌词网页。
- 歌词、时间调整和掌握状态保存在浏览器扩展的本地存储中。
- 只有开发者自行部署本地服务时，音频识别才会在开发者自己的机器上完成。
