# 大肥鱼 · Windows 首次部署 / 重建镜像（Docker Desktop）
# 日常启停请用仓库根目录的 启动大肥鱼.ps1 / 安全关闭大肥鱼.ps1（会管 OpenQQ）。
#
#   .\start.ps1            # 检查 docker → 建目录/.env/数据卷 → 构建镜像 → 起 AstrBot → 等面板就绪
#   .\start.ps1 -NoBuild   # 不重建镜像，只起容器
#   .\start.ps1 -Pull      # 构建前先拉 AstrBot 基础镜像
#   .\start.ps1 -NoWatchdog  # 不启动后台的 QQ 掉线监控（watchdog.py）
param([switch]$NoBuild, [switch]$Pull, [switch]$NoWatchdog)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "错误: $m" -ForegroundColor Red; exit 1 }

# 1. 环境
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die '没找到 docker，先装 Docker Desktop' }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Die 'Docker 守护进程没起来：启动 Docker Desktop，等鲸鱼图标稳定后再试' }
Say "Docker $(docker info --format '{{.ServerVersion}}') · compose $(docker compose version --short)"

# 2. 目录与 .env
'data\plugins', 'data\config', 'napcat\config', 'napcat\qq' | ForEach-Object { New-Item -ItemType Directory -Force $_ | Out-Null }
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Say '首次运行：已生成 .env（要填 QQ 小号就编辑它）' }
$envMap = @{}
Get-Content .env -Encoding utf8 | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*([^#]*)' } | ForEach-Object {
  $envMap[$Matches[1]] = $Matches[2].Trim()
}
$astrPort = if ($envMap.ASTRBOT_PORT) { $envMap.ASTRBOT_PORT } else { '6185' }
$napPort  = if ($envMap.NAPCAT_PORT)  { $envMap.NAPCAT_PORT }  else { '6099' }
$dicePort = if ($envMap.SEALDICE_PORT) { $envMap.SEALDICE_PORT } else { '3211' }

# 3. 依赖文件（补丁与 Dockerfile 复用 ../deploy/）
$missing = 'dashscope_tts.py', 'stage.py', 'respond_stage.py', 'chat_layout.py' |
  Where-Object { -not (Test-Path "..\deploy\patches\$_") }
if ($missing) { Die "缺少核心补丁：$($missing -join ', ')（本套件要在仓库里运行）" }
if (-not (Test-Path ..\deploy\Dockerfile)) { Die '缺少 ..\deploy\Dockerfile' }

# 3.5 数据卷：AstrBot 数据放在 Docker 数据卷 dafeiyu-astrbot-data（原因见 compose.yaml）
docker volume inspect dafeiyu-astrbot-data *> $null
if ($LASTEXITCODE -ne 0) {
  if (Test-Path data\data_v4.db) { Die '.\data 里有旧数据但还没迁移：先运行 .\migrate-data-volume.ps1，否则会起一个空白实例' }
  Say '创建数据卷 dafeiyu-astrbot-data（全新部署）'
  docker volume create --label dafeiyu=astrbot-data dafeiyu-astrbot-data | Out-Null
  if ($LASTEXITCODE -ne 0) { Die '数据卷创建失败' }
}

# 4. 构建并启动
docker compose config -q
if ($LASTEXITCODE -ne 0) { Die 'compose.yaml 解析失败' }
if ($NoBuild) {
  docker image inspect dafeiyu-astrbot:windows *> $null
  if ($LASTEXITCODE -ne 0) { Warn '本地还没有镜像，忽略 -NoBuild'; $NoBuild = $false }
}
if (-not $NoBuild) {
  Say '构建 AstrBot 镜像（首次几分钟，之后走缓存）'
  if ($Pull) { docker compose build --pull } else { docker compose build }
  if ($LASTEXITCODE -ne 0) { Die '镜像构建失败（国内拉不动 Docker Hub 时，在 Docker Desktop 设置里配 registry-mirrors）' }
}
Say '启动 AstrBot（NapCat 在 napcat profile 里，默认不启动）'
docker compose up -d --remove-orphans astrbot
if ($LASTEXITCODE -ne 0) { Die 'docker compose up 失败' }

# 5. 等面板
Say '等待 AstrBot 面板就绪（首次要初始化 data\，最长约 3 分钟）'
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
  try { Invoke-WebRequest "http://127.0.0.1:$astrPort/" -UseBasicParsing -TimeoutSec 3 | Out-Null; $ready = $true; break } catch { Start-Sleep 2 }
}
if (-not $ready) { docker compose logs --tail 40 astrbot; Die '面板 3 分钟没起来，data\ 已保留' }

# 6. QQ 掉线 / 假在线监控：后台每 10 分钟查一次，异常时弹 Windows 通知，只通知不动手
if (-not $NoWatchdog) {
  $wdPidFile = Join-Path $PSScriptRoot 'logs\watchdog.pid'
  $wdRunning = $false
  if (Test-Path $wdPidFile) {
    try {
      $wdOld = [int](Get-Content $wdPidFile -Raw).Trim()
      $wdProc = Get-CimInstance Win32_Process -Filter "ProcessId = $wdOld" -ErrorAction SilentlyContinue
      if ($wdProc -and $wdProc.CommandLine -match 'watchdog\.py') { $wdRunning = $true }
    } catch { }
  }
  $pyw = Get-Command pythonw -ErrorAction SilentlyContinue
  if ($wdRunning) { Say "掉线监控已在运行（PID $wdOld）" }
  elseif (-not $pyw) { Warn '没找到 pythonw，跳过掉线监控（装好 Python 后重跑 start.ps1）' }
  else {
    $wdProc = Start-Process -FilePath $pyw.Source -ArgumentList "`"$PSScriptRoot\watchdog.py`"" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    New-Item -ItemType Directory -Force (Split-Path $wdPidFile) | Out-Null
    [IO.File]::WriteAllText($wdPidFile, [string]$wdProc.Id, (New-Object Text.UTF8Encoding($false)))
    Say '掉线监控已在后台启动（日志 logs\watchdog.log；手动查一次：python watchdog.py --once）'
  }
}

Write-Host @"

=== 起来了 ===
  AstrBot 面板   http://127.0.0.1:$astrPort
  NapCat WebUI   http://127.0.0.1:$napPort/webui   （仅在 --profile napcat 启动 NapCat 时）
  海豹 WebUI     http://127.0.0.1:$dicePort   （骰子插件把海豹拉起来之后才有）

下一步：
 1. 登录 QQ：按 README「改用宿主机上的 OpenQQ」配好 OpenQQ，之后用 启动大肥鱼.ps1 一起拉起。
    （仍用 NapCat：docker compose --profile napcat up -d napcat，反向 WS ws://astrbot:6199/ws 已自动写好。）
 2. 面板初始密码：docker compose logs astrbot | Select-String 'Initial password'
    登录后立刻改密码；平台适配器选 QQ(aiocqhttp)，端口 6199。

常用：..\启动大肥鱼.ps1   ..\安全关闭大肥鱼.ps1   docker compose logs -f astrbot
"@
