# 启动 QQ 机器人（前台运行；关闭这个窗口或按 Ctrl+C 即停止）
$ErrorActionPreference = 'Stop'

try { chcp 65001 | Out-Null } catch { }
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
} catch { }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$config = Join-Path $root 'config.toml'
if (-not (Test-Path $config)) {
    Write-Host '找不到 config.toml。' -ForegroundColor Red
    Write-Host '请先把 config.example.toml 复制为 config.toml，并填写 QQ 号、API Key 和解锁口令。' -ForegroundColor Yellow
    Read-Host '按回车退出'
    exit 1
}

$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Write-Host '没有找到 Python。请先安装 Python 3.11 或更高版本，并勾选“Add python.exe to PATH”。' -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}

Write-Host "使用 Python：$python" -ForegroundColor DarkGray
Write-Host "配置文件：$config" -ForegroundColor DarkGray
Write-Host ''

try {
    & $python -m qqbot --config $config
} finally {
    if ($Host.Name -eq 'ConsoleHost' -and -not $env:QQBOT_NO_PAUSE) {
        Write-Host ''
        Read-Host '机器人已停止，按回车关闭窗口'
    }
}
