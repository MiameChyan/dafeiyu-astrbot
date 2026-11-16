# 启动大肥鱼：Docker Desktop → AstrBot（数据在 Docker 数据卷）→ OpenQQ（宿主机 QQ 协议端）→ 掉线监控。
# 右键“使用 PowerShell 运行”；在终端中可加 -NoPause。
#   -NoOpenQQ             不管 OpenQQ（自己拉起协议端时用）
#   -NoWatchdog           不启用 QQ 掉线监控
#   -LoginTimeoutSeconds  等 QQ 登录的秒数，默认 300
[CmdletBinding()]
param(
    [switch]$NoPause,
    [switch]$NoWatchdog,
    [switch]$NoOpenQQ,
    [ValidateRange(1, 600)][int]$DockerTimeoutSeconds = 180,
    [ValidateRange(1, 600)][int]$ReadyTimeoutSeconds = 180,
    [ValidateRange(1, 1800)][int]$LoginTimeoutSeconds = 300
)
$ErrorActionPreference = 'Stop'
$code = 0
try {
    $manager = Join-Path $PSScriptRoot 'deploy-windows\daily.ps1'
    if (-not (Test-Path -LiteralPath $manager)) {
        $manager = Join-Path $PSScriptRoot 'dafeiyu-astrbot\deploy-windows\daily.ps1'
    }
    if (-not (Test-Path -LiteralPath $manager)) { throw '找不到启停管理脚本，请把本文件放在项目目录。' }
    & $manager -Action Start -NoWatchdog:$NoWatchdog -NoOpenQQ:$NoOpenQQ `
        -DockerTimeoutSeconds $DockerTimeoutSeconds -ReadyTimeoutSeconds $ReadyTimeoutSeconds `
        -LoginTimeoutSeconds $LoginTimeoutSeconds
} catch {
    $code = 1
    Write-Host "启动未完成：$($_.Exception.Message)" -ForegroundColor Red
} finally {
    if (-not $NoPause -and -not [Console]::IsInputRedirected) { [void](Read-Host '按回车关闭窗口') }
}
exit $code
