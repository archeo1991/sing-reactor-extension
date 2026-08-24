param([switch]$NoPause)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Wait-BeforeClose {
    if (-not $NoPause) {
        [void](Read-Host "按 Enter 关闭窗口")
    }
}

try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = [Console]::OutputEncoding
    $Host.UI.RawUI.WindowTitle = "声迹本地识别服务"
    Set-Location -LiteralPath $scriptRoot

    try {
        $healthResponse = Invoke-WebRequest -Uri "http://127.0.0.1:8765/health" -UseBasicParsing -TimeoutSec 3
        $health = $healthResponse.Content | ConvertFrom-Json
        if ($health.ok) {
            Write-Host "服务已经运行" -ForegroundColor Green
            Write-Host "模型：$($health.model)"
            Write-Host "设备：$($health.device)；计算类型：$($health.compute_type)"
            Wait-BeforeClose
            exit 0
        }
    }
    catch {
        # 健康接口不可访问时继续准备并启动服务。
    }

    Write-Host "正在检查本地识别环境..."

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "未找到 python 命令。请先安装 Python，并确保 python 已加入 PATH。"
    }

    $venvDir = Join-Path $scriptRoot ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Host "正在创建 Python 虚拟环境..."
        & $pythonCommand.Source -m venv $venvDir
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            throw "创建 .venv 失败。"
        }
    }

    & $venvPython -c "import fastapi, uvicorn, yt_dlp, faster_whisper" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $requirements = Join-Path $scriptRoot "requirements.txt"
        if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
            throw "缺少 requirements.txt，无法安装依赖。"
        }
        Write-Host "正在安装本地识别依赖..."
        & $venvPython -m pip install -r $requirements
        if ($LASTEXITCODE -ne 0) {
            throw "安装 Python 依赖失败。"
        }
    }

    if ([string]::IsNullOrWhiteSpace($env:WHISPER_MODEL)) {
        $modelDir = Join-Path $scriptRoot ".models\faster-whisper-small"
        $modelFile = Join-Path $modelDir "model.bin"
        if (-not (Test-Path -LiteralPath $modelFile -PathType Leaf)) {
            Write-Host "正在下载 faster-whisper-small 模型（首次运行需要一些时间）..."
            & $venvPython -c "from huggingface_hub import snapshot_download; import sys; snapshot_download(repo_id='Systran/faster-whisper-small', local_dir=sys.argv[1])" $modelDir
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $modelFile -PathType Leaf)) {
                throw "模型下载失败。"
            }
        }
        $env:WHISPER_MODEL = [System.IO.Path]::GetFullPath($modelDir)
    }

    $ffmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ffmpegCommand) {
        $wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
        if (Test-Path -LiteralPath $wingetPackages -PathType Container) {
            Write-Host "正在查找 winget 安装的 FFmpeg..."
            $ffmpegFile = Get-ChildItem -LiteralPath $wingetPackages -Filter "ffmpeg.exe" -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($ffmpegFile) {
                $env:PATH = "$($ffmpegFile.DirectoryName);$env:PATH"
                $ffmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
            }
        }
    }
    if (-not $ffmpegCommand) {
        throw "未找到 FFmpeg。请先运行：winget install --id Gyan.FFmpeg，然后重新双击 start-server.bat。"
    }

    $env:WHISPER_VAD_FILTER = "false"
    $env:PYTHONDONTWRITEBYTECODE = "1"

    Write-Host "正在启动服务：http://127.0.0.1:8765" -ForegroundColor Cyan
    & $venvPython (Join-Path $scriptRoot "server.py")
    if ($LASTEXITCODE -ne 0) {
        throw "本地识别服务异常退出（退出码：$LASTEXITCODE）。"
    }
    Write-Host "本地识别服务已退出。"
    Wait-BeforeClose
    exit 0
}
catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Wait-BeforeClose
    exit 1
}
