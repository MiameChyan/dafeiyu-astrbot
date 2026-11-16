# 大肥鱼 · Windows 停止
#
#   .\stop.ps1         # 停容器，数据保留
#   .\stop.ps1 -Down   # 删除容器与网络，data\ 与 napcat\ 保留
param([switch]$Down)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# 先停掉线监控，免得它把主动停机当成掉线来报
$wdPidFile = Join-Path $PSScriptRoot 'logs\watchdog.pid'
if (Test-Path $wdPidFile) {
  try {
    $wdPid = [int](Get-Content $wdPidFile -Raw).Trim()
    $wdProc = Get-CimInstance Win32_Process -Filter "ProcessId = $wdPid" -ErrorAction SilentlyContinue
    if ($wdProc -and $wdProc.CommandLine -match 'watchdog\.py') {
      Stop-Process -Id $wdPid -Force
      Write-Host '==> 已停止掉线监控' -ForegroundColor Cyan
    }
  } catch { }
  Remove-Item $wdPidFile -ErrorAction SilentlyContinue
}

if ($Down) {
  docker compose down --remove-orphans
  Write-Host '==> 已删除容器，数据保留。重启用 .\start.ps1' -ForegroundColor Cyan
} else {
  docker compose stop
  Write-Host '==> 已停止。重启用 .\start.ps1 -NoBuild' -ForegroundColor Cyan
}
