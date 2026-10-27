# 把机器人加入“启动”文件夹：登录 Windows 后自动在后台运行。
# 不需要管理员权限。撤销方法见脚本末尾。
$ErrorActionPreference = 'Stop'

try { chcp 65001 | Out-Null } catch { }

$root = Split-Path -Parent $PSScriptRoot
$config = Join-Path $root 'config.toml'
if (-not (Test-Path $config)) {
    Write-Host '还没有 config.toml，请先配置好再设置开机自启。' -ForegroundColor Red
    exit 1
}

$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host '没有找到 Python。' -ForegroundColor Red
    exit 1
}
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) {
    Write-Host '提示：没有找到 pythonw.exe，将使用 python.exe；登录时会闪一下控制台窗口。' -ForegroundColor Yellow
    $pythonw = $python
}

$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'QQBot.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "-m qqbot --config `"$config`""
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7
$shortcut.Description = 'QQ 机器人（开机自启，后台运行）'
$shortcut.Save()

Write-Host '已设置开机自启。' -ForegroundColor Green
Write-Host "快捷方式：$shortcutPath"
Write-Host "日志文件：$(Join-Path $root 'data\bot.log')"
Write-Host ''
Write-Host '撤销自启：' -ForegroundColor Yellow
Write-Host "  Remove-Item `"$shortcutPath`""
