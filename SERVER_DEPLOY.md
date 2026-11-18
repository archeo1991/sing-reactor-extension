# 声迹服务端部署

这份说明面向服务维护者。普通用户只安装浏览器扩展，不需要 Python、FFmpeg 或模型文件。

## 环境要求

- Python 3.10 或更高版本
- FFmpeg，并已加入 PATH
- 可访问 B站、模型下载源和歌词来源的网络环境
- 生产环境域名与 HTTPS 证书

## 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

首次识别时 Faster-Whisper 会下载所配置的模型，请预留磁盘空间。

Windows 上也可以直接运行 `.\start-server.ps1`：脚本会复用或创建 `.venv`、安装依赖、预下载 `faster-whisper-small` 模型并启动服务。

## 配置环境变量

PowerShell 示例：

```powershell
$env:SING_REACTOR_HOST="0.0.0.0"
$env:SING_REACTOR_PORT="8765"
$env:SING_REACTOR_EXTENSION_ORIGINS="chrome-extension://<extension-id>"
$env:SING_REACTOR_TOKEN="<random-secret>"
$env:WHISPER_MODEL="small"
```

- `SING_REACTOR_EXTENSION_ORIGINS` 支持逗号分隔多个扩展来源。生产环境应填写实际扩展 ID。
- `SING_REACTOR_TOKEN` 应使用随机高强度密钥，并通过服务器环境变量管理。
- `LYRICS_CONFIG_PATH` 可指向自定义歌词配置；文件必须使用 `schema_version` 1，并包含 `lyrics_config.json` 的全部字段（包括 `title_cleanup.contextual_terms`）。
- 未配置允许来源时，开发模式会接受 Chrome / Firefox 扩展来源；生产环境不要依赖该回退行为。

## 启动服务

直接启动：

```powershell
python server.py
```

也可以使用 Uvicorn：

```powershell
uvicorn server:app --host 0.0.0.0 --port 8765
```

健康检查：

```text
http://127.0.0.1:8765/health
```

注意：`SING_REACTOR_HOST` / `SING_REACTOR_PORT` 只影响 `python server.py`。使用下面的 `uvicorn` 命令或 `deploy/singreactor.service` 时，监听地址由命令行参数决定，修改端口需要同时更新对应文件与 Nginx 配置。

## 生产发布

1. 使用 Nginx、Caddy 或云负载均衡将 HTTPS 请求反向代理到 `127.0.0.1:8765`。
2. 不要直接向公网暴露 Uvicorn 开发端口。
3. 配置进程守护、日志轮转、请求超时和资源监控。
4. 将实际 HTTPS 地址传给发布脚本：

```powershell
.\build-release.ps1 -ApiBase "https://api.example.com"
```

5. 把生成的 `dist/sing-reactor-extension-<version>.zip` 发给用户。

更换服务域名后需要重新生成并发布扩展包。
