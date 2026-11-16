# 安全关闭大肥鱼：停掉线监控 → 等 AstrBot 正常退出（数据库写回）→ 没有其他容器时退出 Docker Desktop。
# OpenQQ 和 QQ 保持登录，下次启动免登录；要彻底下线就在 OpenQQ 窗口按 Ctrl+C 并退出 QQ。
# 右键“使用 PowerShell 运行”；在终端中可加 -NoPause。-KeepDocker 保留 Docker Desktop。
[CmdletBinding()]
param(
    [switch]$NoPause,
    [switch]$KeepDocker,
    [ValidateRange(1, 600)][int]$DockerTimeoutSeconds = 180,
    [ValidateRange(1, 600)][int]$StopTimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
$code = 0
try {
    $manager = Join-Path $PSScriptRoot 'deploy-windows\daily.ps1'
    if (-not (Test-Path -LiteralPath $manager)) {
        $manager = Join-Path $PSScriptRoot 'dafeiyu-astrbot\deploy-windows\daily.ps1'
    }
    if (-not (Test-Path -LiteralPath $manager)) { throw '找不到启停管理脚本，请把本文件放在项目目录。' }
    & $manager -Action Stop -KeepDocker:$KeepDocker `
        -DockerTimeoutSeconds $DockerTimeoutSeconds -StopTimeoutSeconds $StopTimeoutSeconds
} catch {
    $code = 1
    Write-Host "关闭未完成：$($_.Exception.Message)" -ForegroundColor Red
} finally {
    if (-not $NoPause -and -not [Console]::IsInputRedirected) { [void](Read-Host '按回车关闭窗口') }
}
exit $code
