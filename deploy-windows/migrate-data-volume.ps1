# 一次性迁移：把旧的 .\data（Windows 目录）里的 AstrBot 数据迁进 Docker 数据卷 dafeiyu-astrbot-data。
# 原因见 compose.yaml：SQLite 放在 Windows 绑定挂载上会写坏，对话历史停止保存。
#
# 用法（先停 AstrBot，保留 Docker）：
#   .\安全关闭大肥鱼.ps1 -NoPause -KeepDocker
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy-windows\migrate-data-volume.ps1
#   .\启动大肥鱼.ps1
#
# 做什么：
#   1. 确认 AstrBot 已停、数据卷里还没有数据库（已迁移过就拒绝，不覆盖）。
#   2. 在 AstrBot 镜像里用 migrate_data_volume.py 复制，并逐个检查 SQLite 库完整性。
#   3. 都通过后，把宿主机 .\data 里已经迁走的内容整体移到 .\data\_migrated_to_volume_<时间>\ 留作备份，
#      只留下仍然有效的 plugins\ 和 config\，免得以后误改宿主机上的旧 cmd_config.json。
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
$data = Join-Path $kit 'data'
$volume = 'dafeiyu-astrbot-data'
$image = 'dafeiyu-astrbot:windows'
$compose = @('compose', '--project-directory', $kit, '--file', (Join-Path $kit 'compose.yaml'),
             '--project-name', 'dafeiyu-windows')

function Say([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

function DockerResult([string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    $output = @(& docker @Arguments 2>&1 | ForEach-Object { [string]$_ })
    [pscustomobject]@{Code=$LASTEXITCODE; Lines=$output; Text=($output -join "`n")}
}

function DockerChecked([string[]]$Arguments, [string]$Failure) {
    $result = DockerResult $Arguments
    if ($result.Code -ne 0) { throw "$Failure（Docker 退出码 $($result.Code)）。$($result.Text)" }
    $result
}

if (-not (Test-Path -LiteralPath (Join-Path $data 'data_v4.db'))) {
    throw "没有在 $data 找到 data_v4.db，没有可迁移的数据。"
}
$running = DockerChecked ($compose + @('ps', '--status', 'running', '--quiet', 'astrbot')) '无法查询 AstrBot 容器'
if ($running.Text.Trim()) { throw 'AstrBot 还在运行。先执行 安全关闭大肥鱼.ps1 -KeepDocker 再迁移。' }
$null = DockerChecked @('image', 'inspect', '--format', '{{.Id}}', $image) "找不到镜像 $image"

if ((DockerResult @('volume', 'inspect', $volume)).Code -ne 0) {
    Say "创建数据卷 $volume"
    $null = DockerChecked @('volume', 'create', '--label', 'dafeiyu=astrbot-data', $volume) '数据卷创建失败'
}

Say '复制数据并检查数据库完整性'
$copy = DockerResult @('run', '--rm', '--network', 'none',
    '--mount', "type=bind,source=$data,target=/src,readonly",
    '--mount', "type=bind,source=$(Join-Path $kit 'migrate_data_volume.py'),target=/tools/migrate_data_volume.py,readonly",
    '--mount', "type=volume,source=$volume,target=/dst",
    '--entrypoint', 'python', $image, '/tools/migrate_data_volume.py', '/src', '/dst')
$copy.Lines | ForEach-Object { Write-Host "    $_" }
if ($copy.Code -eq 2) { throw '数据卷里已经有数据库，之前迁移过了；没有覆盖任何东西。' }
if ($copy.Code -ne 0) { throw "迁移失败（退出码 $($copy.Code)）。宿主机 .\data 没有改动，数据卷里可能有半成品，请检查后再试。" }

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backup = Join-Path $data "_migrated_to_volume_$stamp"
Say "把已迁走的旧文件移到 $backup"
New-Item -ItemType Directory -Path $backup | Out-Null
Get-ChildItem -LiteralPath $data -Force |
    Where-Object { $_.Name -notin @('plugins', 'config') -and $_.FullName -ne $backup -and $_.Name -notlike '_migrated_to_volume_*' } |
    ForEach-Object { Move-Item -LiteralPath $_.FullName -Destination $backup }
$note = @"
AstrBot 的数据从 $stamp 起放在 Docker 数据卷 $volume 里，不在这个目录。
这里只有 plugins\ 和 config\ 仍然生效（compose 把它们挂进容器）。
_migrated_to_volume_* 是迁移前的旧文件，仅作备份；改它们不会影响大肥鱼。
改聊天配置请用 AstrBot 面板。
"@
[IO.File]::WriteAllText((Join-Path $data '数据在Docker数据卷里.txt'), $note, (New-Object Text.UTF8Encoding($true)))
Say '迁移完成。运行 启动大肥鱼.ps1 启动（会按新配置重建 AstrBot 容器）。'
