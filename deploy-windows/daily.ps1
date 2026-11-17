# 日常启停：AstrBot 容器（数据在 Docker 数据卷）+ 宿主机上的 OpenQQ 协议端 + 掉线监控。
# 首次部署仍用 start.ps1；从旧的 .\data 目录迁移用 migrate-data-volume.ps1。
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Start', 'Stop')][string]$Action,
    [switch]$KeepDocker,
    [switch]$NoWatchdog,
    [switch]$NoOpenQQ,
    [ValidateRange(1, 600)][int]$DockerTimeoutSeconds = 180,
    [ValidateRange(1, 600)][int]$ReadyTimeoutSeconds = 180,
    [ValidateRange(1, 1800)][int]$LoginTimeoutSeconds = 300,
    [ValidateRange(1, 600)][int]$StopTimeoutSeconds = 120,
    [ValidateRange(1, 600)][int]$ConnectTimeoutSeconds = 60
)
$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
$compose = @('compose', '--project-directory', $kit, '--file', (Join-Path $kit 'compose.yaml'),
             '--project-name', 'dafeiyu-windows')
$pidFile = Join-Path $kit 'logs\watchdog.pid'
$watchdogPath = Join-Path $kit 'watchdog.py'
$dataVolume = 'dafeiyu-astrbot-data'
$astrImage = 'dafeiyu-astrbot:windows'
$openQQStart = '(?i)main\.mjs"?\s+start\b'
$openQQFailed = @{
    'failed'     = 'OpenQQ 启动失败'
    'hook_error' = 'OpenQQ 接入 QQ 出错'
    'detached'   = 'OpenQQ 已解除挂接'
    'offline'    = 'QQ 账号被迫下线'
}

function Say([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

function DockerResult([string[]]$Arguments) {
    # Windows PowerShell 5.1 把原生命令 stderr 包装成 ErrorRecord。
    # 收集输出并显式检查退出码，不把 daemon 不在线误当作脚本崩溃。
    $ErrorActionPreference = 'Continue'
    $output = @(& docker @Arguments 2>&1 | ForEach-Object { [string]$_ })
    $code = $LASTEXITCODE
    [pscustomobject]@{Code=$code; Lines=$output; Text=($output -join "`n")}
}

function DockerChecked([string[]]$Arguments, [string]$Failure) {
    $result = DockerResult $Arguments
    if ($result.Code -ne 0) { throw "$Failure（Docker 退出码 $($result.Code)）。$($result.Text)" }
    $result
}

function EngineReady {
    # Docker 28 在 daemon 不在线时仍可能以 0 退出并返回空版本。
    $result = DockerResult @('info', '--format', '{{.ServerVersion}}')
    $result.Code -eq 0 -and $result.Text.Trim() -match '^\d+\.\d+\.\d+\S*$'
}
function DesktopProcesses { @(Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue) }

function AssertLocalDocker {
    if ($env:DOCKER_HOST -and $env:DOCKER_HOST -notlike 'npipe:////./pipe/*') {
        throw 'DOCKER_HOST 指向其他主机，已停止操作。请切回本机 Docker Desktop。'
    }
    $endpoint = DockerChecked @('context', 'inspect', '--format', '{{.Endpoints.docker.Host}}') '无法检查 Docker 上下文'
    if ($endpoint.Text.Trim() -notlike 'npipe:////./pipe/*') {
        throw '当前 Docker 上下文不是本机 Windows Docker Desktop，已停止操作。'
    }
}

function EnvValue([string]$Name) {
    # 进程环境变量优先，其次是同目录 .env（与 compose / watchdog 的读取方式一致）。
    $fromProcess = [Environment]::GetEnvironmentVariable($Name)
    if ($fromProcess) { return $fromProcess.Trim() }
    $envFile = Join-Path $kit '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { return $null }
    foreach ($line in (Get-Content -LiteralPath $envFile -Encoding UTF8)) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*?)\s*$") {
            return $Matches[1].Trim('"', "'")
        }
    }
    $null
}

# ---------- 掉线监控 ----------

function WatchdogProcess {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $number = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$number) -or $number -le 0) {
        Write-Warning '监控 PID 文件无效，将清除过期记录。'
        Remove-Item -LiteralPath $pidFile
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $number" -ErrorAction Stop
    # PID 可被系统复用，只有命令行包含本套件的完整 watchdog.py 路径才允许停止。
    $pattern = '(?i)(?:^|\s)"?' + [regex]::Escape($watchdogPath) + '"?(?:\s|$)'
    if ($process -and $process.CommandLine -match $pattern) { return $process }
    Write-Warning '监控 PID 已过期或属于其他程序，仅清除过期记录。'
    Remove-Item -LiteralPath $pidFile
    return $null
}

function StopWatchdog {
    $process = WatchdogProcess
    if ($process) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
        Say '掉线监控已停止'
    }
}

function StartWatchdog {
    if ($NoWatchdog) { return }
    if (WatchdogProcess) { Say '掉线监控已在运行'; return }
    $python = Get-Command pythonw -ErrorAction SilentlyContinue
    if (-not $python) { Write-Warning '没有找到 pythonw，大肥鱼正常运行，但未开启掉线监控。'; return }
    if (-not (Test-Path -LiteralPath $watchdogPath)) { throw "缺少监控脚本：$watchdogPath" }
    New-Item -ItemType Directory -Path (Split-Path -Parent $pidFile) -Force | Out-Null
    $process = Start-Process -FilePath $python.Source -ArgumentList ('"' + $watchdogPath + '"') `
        -WorkingDirectory $kit -WindowStyle Hidden -PassThru
    [IO.File]::WriteAllText($pidFile, [string]$process.Id, (New-Object Text.UTF8Encoding($false)))
    Say '掉线监控已在后台启动'
}

# ---------- 容器 ----------

function ContainerId([string]$Service) {
    $result = DockerChecked ($compose + @('ps', '--all', '--quiet', $Service)) "无法查询 $Service 容器"
    $ids = @($result.Lines | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() })
    if ($ids.Count -eq 0) { return $null }
    if ($ids.Count -ne 1 -or $ids[0] -notmatch '^[a-f0-9]{12,64}$') { throw "$Service 容器数量异常，请在 Docker Desktop 中检查。" }
    $ids[0]
}

function ContainerState([string]$Id) {
    $result = DockerChecked @('inspect', '--format', '{{json .State}}', $Id) '无法读取容器状态'
    $state = $result.Text | ConvertFrom-Json
    if ($null -eq $state.Running -or $null -eq $state.ExitCode -or -not $state.Status) {
        throw 'Docker 返回了不完整的容器状态，已停止操作。'
    }
    $state
}

function StopContainer([string]$Service, [string]$Id) {
    # 返回问题描述；正常退出返回 $null。
    $state = ContainerState $Id
    if ($state.Paused) { throw "$Service 容器已暂停，请先恢复后再正常停止。" }
    if (-not $state.Running) {
        if ($state.Status -notin @('exited', 'created')) { throw "$Service 当前状态异常：$($state.Status)" }
        Say "$Service 已停止"
        return $null
    }
    Say "等待 $Service 正常退出（最多 $StopTimeoutSeconds 秒）"
    $null = DockerChecked @('stop', '--time', [string]$StopTimeoutSeconds, $Id) "$Service 停止失败，未退出 Docker"
    $state = ContainerState $Id
    if ($state.Running -or $state.Status -ne 'exited') { throw "$Service 尚未完全退出，未退出 Docker。" }
    if ($state.ExitCode -eq 137 -or $state.OOMKilled) {
        return "$Service 被强制结束（退出码 $($state.ExitCode)），无法确认正常收尾"
    }
    if ($state.ExitCode -notin @(0, 143)) { return "$Service 异常退出（退出码 $($state.ExitCode)），请检查日志" }
    $null
}

function StopNapCatIfRunning {
    # NapCat 与 OpenQQ 登同一个小号，同时在线会互相顶号。
    $nap = ContainerId 'napcat'
    if (-not $nap) { return }
    if (-not (ContainerState $nap).Running) { return }
    Say 'NapCat 容器在运行，会和 OpenQQ 抢同一个 QQ 号，先停掉它'
    $issue = StopContainer 'napcat' $nap
    if ($issue) { Write-Warning $issue }
}

function AssertDataVolume {
    if ((DockerResult @('volume', 'inspect', $dataVolume)).Code -ne 0) {
        throw "数据卷 $dataVolume 不存在。从旧的 data 目录迁移请运行 deploy-windows\migrate-data-volume.ps1；全新部署请用 deploy-windows\start.ps1。"
    }
    $probe = DockerResult @('run', '--rm', '--network', 'none',
        '--mount', "type=volume,source=$dataVolume,target=/d,readonly",
        '--entrypoint', 'test', $astrImage, '-f', '/d/data_v4.db')
    if ($probe.Code -ne 0) {
        throw "数据卷 $dataVolume 里没有 data_v4.db，为免起一个空白的大肥鱼，已停止。请先运行 deploy-windows\migrate-data-volume.ps1。"
    }
}

function WaitPanel([string]$Id) {
    # 从实际映射取端口，兼容 .env 与进程环境变量覆盖，不猜默认端口。
    $port = DockerChecked @('port', $Id, '6185/tcp') '无法获取 AstrBot 面板端口'
    if ($port.Text.Trim() -notmatch '^127\.0\.0\.1:(\d+)$') { throw 'AstrBot 面板没有唯一的本机端口映射。' }
    $url = "http://127.0.0.1:$($Matches[1])/"
    Say "等待 AstrBot 面板就绪（最多 $ReadyTimeoutSeconds 秒）"
    $deadline = [DateTime]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
    while ($true) {
        try {
            $null = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            return $url
        } catch {
            if ([DateTime]::UtcNow -ge $deadline) { throw 'AstrBot 面板等待超时，容器与数据已保留，请检查容器日志。' }
            Start-Sleep -Seconds 2
        }
    }
}

function OneBotClients([string]$Id) {
    # 数 /proc/net/tcp(6) 里本地端口 6199（0x1837）、状态 01（ESTABLISHED）的连接，与 watchdog.py 一致。
    $result = DockerResult @('exec', $Id, 'cat', '/proc/net/tcp', '/proc/net/tcp6')
    if ($result.Code -ne 0) { return 0 }
    @($result.Lines | Where-Object {
        $f = $_.Trim() -split '\s+'
        $f.Count -gt 3 -and $f[1] -match ':1837$' -and $f[3] -eq '01'
    }).Count
}

# ---------- OpenQQ ----------

function OpenQQProcess {
    $all = @(Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction Stop)
    @($all | Where-Object { $_.CommandLine -match $openQQStart }) | Select-Object -First 1
}

function OpenQQStatus([string]$Dir) {
    $path = Join-Path $Dir '.runtime\status.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { $status = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return $null }
    $updated = $null
    if ($status.updatedAt -is [DateTime]) {
        # PowerShell 7 的 ConvertFrom-Json 会把 ISO 时间直接转成 DateTime
        $updated = $status.updatedAt.ToUniversalTime()
    } elseif ($status.updatedAt) {
        try {
            $updated = [DateTime]::Parse([string]$status.updatedAt, [Globalization.CultureInfo]::InvariantCulture,
                                         [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
        } catch { $updated = $null }
    }
    [pscustomobject]@{Phase=[string]$status.phase; Updated=$updated}
}

function LaunchOpenQQ([string]$Dir) {
    if (-not (Test-Path -LiteralPath (Join-Path $Dir 'src\main.mjs'))) { throw "OPENQQ_DIR 下没有 src\main.mjs：$Dir" }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw '没有找到 node，无法启动 OpenQQ。' }
    if (@(Get-Process -Name 'QQ' -ErrorAction SilentlyContinue).Count -gt 0) {
        throw 'QQ 正在运行。OpenQQ 要自己拉起 QQ 才能挂接：请先退出所有 QQ（包括主号，托盘里也要退出），再重新运行启动脚本。AstrBot 已在运行，不受影响。'
    }
    $quoted = $Dir.Replace("'", "''")
    $command = "`$Host.UI.RawUI.WindowTitle = 'OpenQQ（大肥鱼协议端，别关）'; Set-Location -LiteralPath '$quoted'; node src/main.mjs start"
    Say '在新窗口启动 OpenQQ（这个窗口要一直开着）'
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoExit', '-NoProfile', '-Command', $command) `
        -WorkingDirectory $Dir -WindowStyle Normal -PassThru
}

function EnsureOpenQQ([string]$AstrId) {
    if ($NoOpenQQ) { Say '按参数跳过 OpenQQ'; return $false }
    $dir = EnvValue 'OPENQQ_DIR'
    if (-not $dir) {
        Write-Warning '.env 没有配置 OPENQQ_DIR，跳过 OpenQQ。QQ 协议端需要自己拉起。'
        return $false
    }
    $since = $null
    $launcher = $null
    $seenProcess = [bool](OpenQQProcess)
    if ($seenProcess) {
        Say 'OpenQQ 已在运行'
    } else {
        $since = [DateTime]::UtcNow.AddSeconds(-2)
        $launcher = LaunchOpenQQ $dir
        Write-Host '    在 OpenQQ 拉起的 QQ 里登录大肥鱼的小号；需要主号的话，等登录完再另开 QQ。'
    }
    Say "等待 QQ 登录完成（最多 $LoginTimeoutSeconds 秒）"
    $deadline = [DateTime]::UtcNow.AddSeconds($LoginTimeoutSeconds)
    # 新 PowerShell 窗口是异步启动的，Node 不会立即出现；启动等待不延长总登录预算。
    $startupDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(30, $LoginTimeoutSeconds))
    while ($true) {
        $status = OpenQQStatus $dir
        $fresh = $status -and ($null -eq $since -or ($status.Updated -and $status.Updated -ge $since))
        if ($fresh -and $openQQFailed.ContainsKey($status.Phase)) {
            throw "$($openQQFailed[$status.Phase])。看一下 OpenQQ 窗口；被踢下线就在它拉起的 QQ 里重新登录，然后重新运行启动脚本。"
        }
        $process = OpenQQProcess
        if ($process) {
            $seenProcess = $true
            # 状态文件不能代替实际进程，避免读取到刚退出的旧实例留下的 ready。
            if ($fresh -and $status.Phase -eq 'session_ready') { break }
        } elseif ($seenProcess -or ($launcher -and $launcher.HasExited)) {
            throw 'OpenQQ 窗口退出了，请查看报错后重新运行启动脚本。'
        } elseif ([DateTime]::UtcNow -ge $startupDeadline) {
            throw '未检测到 OpenQQ 进程启动，请检查新窗口里的 Node 报错后重新运行启动脚本。'
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "$LoginTimeoutSeconds 秒内没等到 QQ 登录完成。登录好之后重新运行启动脚本即可（不会重复拉起 OpenQQ）。"
        }
        Start-Sleep -Seconds 2
    }
    Say '等待 OpenQQ 连上 AstrBot（6199）'
    $deadline = [DateTime]::UtcNow.AddSeconds($ConnectTimeoutSeconds)
    while ((OneBotClients $AstrId) -lt 1) {
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "QQ 已登录，但 OpenQQ $ConnectTimeoutSeconds 秒内没连上 AstrBot。检查 OpenQQ 配置里的反向 WS 是否为 ws://127.0.0.1:6199/ws。"
        }
        Start-Sleep -Seconds 2
    }
    $true
}

# ---------- 启动 / 关闭 ----------

function StartDaily {
    if (-not (EngineReady)) {
        Say '正在启动 Docker Desktop'
        $null = DockerChecked @('desktop', 'start', '--detach') 'Docker Desktop 启动失败，请手动打开后重试'
        $deadline = [DateTime]::UtcNow.AddSeconds($DockerTimeoutSeconds)
        while (-not (EngineReady)) {
            if ([DateTime]::UtcNow -ge $deadline) { throw "Docker 引擎在 $DockerTimeoutSeconds 秒内未就绪。" }
            Start-Sleep -Seconds 2
        }
    }
    StopNapCatIfRunning
    AssertDataVolume
    $astr = ContainerId 'astrbot'
    if ($astr) {
        $state = ContainerState $astr
        if ($state.Paused -or $state.Restarting) { throw "AstrBot 容器状态异常（$($state.Status)），请在 Docker Desktop 中检查后重试。" }
    }
    # up 只在 compose 配置变了时重建 AstrBot（比如改了挂载），否则只是启动/保持运行。
    # --no-deps / --no-build / --pull never：不碰 NapCat、不构建、不联网拉镜像。
    Say '启动 AstrBot（配置有变化时会按新配置重建容器，数据在数据卷里不受影响）'
    $null = DockerChecked ($compose + @('up', '--detach', '--no-deps', '--no-build', '--pull', 'never', 'astrbot')) 'AstrBot 启动失败'
    $astr = ContainerId 'astrbot'
    if (-not $astr) { throw 'AstrBot 容器没有创建出来，请检查 Docker Desktop。' }
    $url = WaitPanel $astr
    $state = ContainerState $astr
    if (-not $state.Running -or $state.Restarting -or $state.Paused) { throw 'AstrBot 容器未稳定运行，不能确认启动完成。' }
    $qq = EnsureOpenQQ $astr
    StartWatchdog
    Say "大肥鱼已启动，AstrBot 面板：$url"
    if ($qq) { Write-Host 'QQ 已登录并连上 AstrBot。OpenQQ 窗口别关，关了她就收不到消息。' }
}

function StopDaily {
    StopWatchdog
    if (-not (EngineReady)) {
        if (@(DesktopProcesses).Count -gt 0) { throw 'Docker Desktop 仍在运行但引擎无法连接，无法确认容器状态；未强行退出 Docker。' }
        Say 'Docker Desktop 已关闭，无需重复停止'
        return
    }
    $issues = @()
    # 只需让 AstrBot 正常退出：退出时 SQLite 会把 WAL 写回主库。NapCat 若被手动开过也一起停。
    foreach ($service in @('astrbot', 'napcat')) {
        $id = ContainerId $service
        if (-not $id) { continue }
        $issue = StopContainer $service $id
        if ($issue) { $issues += $issue }
    }
    if ($issues.Count -gt 0) { throw (($issues -join '；') + '。容器已停止，数据已保留；保留 Docker 以便检查。') }
    Say '大肥鱼已停止，聊天数据、配置、容器和镜像均保留'
    $openQQ = $null
    try { $openQQ = OpenQQProcess } catch { $openQQ = $null }
    if ($openQQ) {
        Write-Host 'OpenQQ 和 QQ 仍保持登录（下次启动不用重新登录）。要彻底下线：在 OpenQQ 窗口按 Ctrl+C，再退出它拉起的 QQ。'
    }
    if ($KeepDocker) { Say '按参数保留 Docker Desktop'; return }
    # 包括暂停/重启中的其他容器：它们仍占用引擎，不能顺带终止。
    $others = DockerChecked @('ps', '--format', '{{.Names}}') '无法确认其他容器状态，保留 Docker'
    if ($others.Text.Trim()) {
        Write-Warning "还有其他容器在运行，保留 Docker Desktop：$($others.Text)"
        return
    }
    Say '正在退出 Docker Desktop'
    $null = DockerChecked @('desktop', 'stop', '--timeout', [string]$DockerTimeoutSeconds) 'Docker Desktop 退出失败'
    $deadline = [DateTime]::UtcNow.AddSeconds($DockerTimeoutSeconds)
    while ((EngineReady) -or @(DesktopProcesses).Count -gt 0) {
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Docker Desktop 尚未完全退出，请检查托盘状态。' }
        Start-Sleep -Seconds 2
    }
    Say 'Docker Desktop 和后台引擎已关闭'
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw '没有找到 Docker CLI，请先安装 Docker Desktop。' }
$lockPath = [IO.Path]::GetFullPath($kit).ToLowerInvariant()
$sha = [Security.Cryptography.SHA256]::Create()
try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($lockPath))).Replace('-', '') }
finally { $sha.Dispose() }
$mutex = New-Object Threading.Mutex($false, ('Local\DafeiyuLifecycle-' + $hash))
$ownsLock = $false
try {
    try { $ownsLock = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsLock = $true }
    if (-not $ownsLock) { throw '另一个大肥鱼启停脚本正在运行，请等它结束后再操作。' }
    AssertLocalDocker
    if ($Action -eq 'Start') { StartDaily } else { StopDaily }
} finally {
    if ($ownsLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
