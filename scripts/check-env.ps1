# 启动前自检：Python 版本、配置文件、端口占用、NapCat 是否在监听。
$ErrorActionPreference = 'Continue'

try { chcp 65001 | Out-Null } catch { }
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
} catch { }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$ok = $true

Write-Host '=== 1. Python ===' -ForegroundColor Cyan
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host '  [X] 没有找到 python，请安装 Python 3.11+ 并加入 PATH。' -ForegroundColor Red
    $ok = $false
} else {
    $version = & $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
    $majorMinor = & $python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    if ([version]$majorMinor -lt [version]'3.11') {
        Write-Host "  [X] Python $version 太旧，需要 3.11 或更高版本。" -ForegroundColor Red
        $ok = $false
    } else {
        Write-Host "  [OK] Python $version（$python）" -ForegroundColor Green
    }
}

Write-Host '=== 2. 配置文件 ===' -ForegroundColor Cyan
$config = Join-Path $root 'config.toml'
if (-not (Test-Path $config)) {
    Write-Host '  [X] 没有 config.toml，请复制 config.example.toml 后填写。' -ForegroundColor Red
    $ok = $false
} else {
    if ($python) {
        & $python -m qqbot --config $config --check
        if ($LASTEXITCODE -ne 0) { $ok = $false }
    }
}

Write-Host '=== 3. 端口 ===' -ForegroundColor Cyan
$botPort = 3001
$napcatPort = 3000
$listener = Get-NetTCPConnection -LocalPort $botPort -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Host "  [!] 端口 $botPort 已被占用（可能机器人已经在运行）。" -ForegroundColor Yellow
} else {
    Write-Host "  [OK] 端口 $botPort 空闲。" -ForegroundColor Green
}

$napcatOpen = Test-NetConnection -ComputerName 127.0.0.1 -Port $napcatPort -InformationLevel Quiet -WarningAction SilentlyContinue
if ($napcatOpen) {
    Write-Host "  [OK] NapCat HTTP 服务端在 127.0.0.1:$napcatPort 监听。" -ForegroundColor Green
} else {
    Write-Host "  [!] 127.0.0.1:$napcatPort 没有监听：NapCat 还没启动，或还没启用 HTTP 服务端。" -ForegroundColor Yellow
}

Write-Host '=== 4. 数据目录 ===' -ForegroundColor Cyan
$dataDir = Join-Path $root 'data'
try {
    New-Item -ItemType Directory -Force $dataDir | Out-Null
    $probe = Join-Path $dataDir '.write-test'
    Set-Content -Path $probe -Value 'ok' -Encoding utf8
    Remove-Item $probe -Force
    Write-Host "  [OK] $dataDir 可写。" -ForegroundColor Green
} catch {
    Write-Host "  [X] $dataDir 不可写：$($_.Exception.Message)" -ForegroundColor Red
    $ok = $false
}

Write-Host ''
if ($ok) {
    Write-Host '自检通过。下一步：python -m qqbot --config config.toml' -ForegroundColor Green
} else {
    Write-Host '自检没有通过，请先处理上面标 [X] 的项目。' -ForegroundColor Red
}
