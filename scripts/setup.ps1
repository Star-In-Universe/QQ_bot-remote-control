# 交互式生成 config.toml：填 QQ 号、两个 Token、API Key、解锁口令。
# 用法：powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
$ErrorActionPreference = 'Stop'

try { chcp 65001 | Out-Null } catch { }
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = '1'
} catch { }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function New-Secret([int]$Length = 24) {
    $chars = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    -join (1..$Length | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
}

function Assert-Safe([string]$Value, [string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name 不能为空。"
    }
    if ($Value -match '["\\\r\n]') {
        throw "$Name 不能包含双引号、反斜杠或换行。"
    }
}

Write-Host '=== QQ 机器人配置向导 ===' -ForegroundColor Cyan
Write-Host '回车表示使用括号里的默认值。' -ForegroundColor DarkGray
Write-Host ''

$qq = Read-Host '你的 QQ 号（主号，用来给机器人发消息）'
if ($qq -notmatch '^\d{5,12}$') {
    Write-Host 'QQ 号应该是 5-12 位数字。' -ForegroundColor Red
    exit 1
}

$apiToken = Read-Host 'NapCat HTTP 服务端 Token（回车自动生成）'
if ([string]::IsNullOrWhiteSpace($apiToken)) { $apiToken = New-Secret 24 }
Assert-Safe $apiToken 'HTTP 服务端 Token'

$webhookToken = Read-Host 'NapCat HTTP 客户端 Token（回车自动生成）'
if ([string]::IsNullOrWhiteSpace($webhookToken)) { $webhookToken = New-Secret 24 }
Assert-Safe $webhookToken 'HTTP 客户端 Token'

$secureKey = Read-Host 'DeepSeek API Key（sk-...，输入不回显）' -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $apiKey = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    Write-Host 'API Key 不能为空。' -ForegroundColor Red
    exit 1
}
Assert-Safe $apiKey 'API Key'
if (-not $apiKey.StartsWith('sk-')) {
    Write-Host '提示：DeepSeek 的 Key 通常以 sk- 开头，请确认没有复制错。' -ForegroundColor Yellow
}

$secret = Read-Host '远程控制解锁口令（回车自动生成 32 位）'
if ([string]::IsNullOrWhiteSpace($secret)) { $secret = New-Secret 32 }
if ($secret.Length -lt 12) {
    Write-Host '解锁口令至少 12 位。' -ForegroundColor Red
    exit 1
}
Assert-Safe $secret '解锁口令'

$templatePath = Join-Path $root 'config.example.toml'
$configPath = Join-Path $root 'config.toml'
if (-not (Test-Path $templatePath)) {
    Write-Host '找不到 config.example.toml。' -ForegroundColor Red
    exit 1
}
if (Test-Path $configPath) {
    $answer = Read-Host 'config.toml 已存在，要覆盖吗？(y/N)'
    if ($answer -notin @('y', 'Y', 'yes', 'YES')) {
        Write-Host '已取消。' -ForegroundColor Yellow
        exit 0
    }
}

$text = Get-Content -Raw -Encoding UTF8 $templatePath
$text = $text.Replace('api_token = "REPLACE_ME"', 'api_token = "' + $apiToken + '"')
$text = $text.Replace('webhook_token = "REPLACE_ME"', 'webhook_token = "' + $webhookToken + '"')
$text = $text.Replace('owner_ids = [123456789]', 'owner_ids = [' + $qq + ']')
$text = $text.Replace('api_key = ""', 'api_key = "' + $apiKey + '"')
$text = $text.Replace('secret = "REPLACE_WITH_LONG_RANDOM_SECRET"', 'secret = "' + $secret + '"')

[System.IO.File]::WriteAllText($configPath, $text, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ''
Write-Host '已写入 config.toml。' -ForegroundColor Green
Write-Host ''
Write-Host '请把下面两组参数填进 NapCat 的「网络配置」：' -ForegroundColor Cyan
Write-Host "  HTTP 服务端  监听 127.0.0.1:3000  Token: $apiToken"
Write-Host "  HTTP 客户端  上报 http://127.0.0.1:3001/onebot  Token: $webhookToken"
Write-Host ''
Write-Host '解锁口令（只保存在 config.toml，别发群里）：' -ForegroundColor Yellow
Write-Host "  $secret"
Write-Host ''

$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if ($python) {
    Write-Host '正在检查配置…' -ForegroundColor DarkGray
    & $python -m qqbot --config $configPath --check
    if ($LASTEXITCODE -ne 0) {
        Write-Host '配置检查没有通过，请按上面的提示修改 config.toml。' -ForegroundColor Red
    }
} else {
    Write-Host '没有找到 python，跳过配置检查。' -ForegroundColor Yellow
}
