"""Run the real PowerShell entry points with a simulated Docker / OpenQQ boundary.

No live containers, QQ connections, or Windows processes are changed: ``docker``,
process queries and ``Start-Process`` are replaced by PowerShell functions that
record what the scripts asked for.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("powershell") or shutil.which("pwsh")
ASTR = "a" * 64
NAP = "b" * 64

HARNESS = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$cfg = Get-Content -LiteralPath $env:DF_TEST_CONFIG -Raw -Encoding UTF8 | ConvertFrom-Json
$global:DFTestEngine = [bool]$cfg.engine
$global:DFTestDesktop = [bool]$cfg.desktop
$global:DFTestAstrRunning = [bool]$cfg.running
$global:DFTestAstrExists = -not ($cfg.missing -eq 'astrbot')
$global:DFTestNapRunning = [bool]$cfg.napRunning
$global:DFTestStoppedAstr = $false
$global:DFTestOpenQQ = [bool]$cfg.openqqRunning
$global:DFTestOpenQQLaunched = $false
$global:DFTestOpenQQProbes = 0
$statusPath = Join-Path $env:DF_TEST_OPENQQ '.runtime\status.json'
function Record($kind, $value) {
    @{kind=$kind; value=$value} | ConvertTo-Json -Compress -Depth 6 |
        Add-Content -LiteralPath $env:DF_TEST_EVENTS -Encoding UTF8
}
function WriteStatus($phase, $ageSeconds = 0) {
    New-Item -ItemType Directory -Force -Path (Split-Path $statusPath) | Out-Null
    @{phase=$phase; updatedAt=[DateTime]::UtcNow.AddSeconds(-$ageSeconds).ToString('o')} | ConvertTo-Json |
        Set-Content -LiteralPath $statusPath -Encoding UTF8
}
if ($cfg.statusPhase) { WriteStatus $cfg.statusPhase ([int]$cfg.statusAgeSeconds) }
function docker {
    $a = @($args | ForEach-Object { [string]$_ })
    Record 'docker' $a
    $global:LASTEXITCODE = 0
    $joined = $a -join ' '
    if ($joined -like 'context inspect*') { $cfg.endpoint; return }
    if ($a[0] -eq 'info') {
        if ($global:DFTestEngine) { '28.0.1' }
        elseif (-not $cfg.infoFalseSuccess) { $global:LASTEXITCODE = 1 }
        return
    }
    if ($joined -like 'desktop start*') {
        if ($cfg.startFail) { $global:LASTEXITCODE = 1; return }
        $global:DFTestDesktop = $true
        $global:DFTestEngine = -not $cfg.engineTimeout
        return
    }
    if ($joined -like 'desktop stop*') {
        if ($cfg.desktopStopFail) { $global:LASTEXITCODE = 1; return }
        if (-not $cfg.desktopStillRunning) {
            $global:DFTestEngine = $false; $global:DFTestDesktop = $false
        }
        return
    }
    if ($a[0] -eq 'volume' -and $a[1] -eq 'inspect') {
        if ($cfg.noVolume) { $global:LASTEXITCODE = 1 }
        return
    }
    if ($a[0] -eq 'run') {
        if ($cfg.emptyVolume) { $global:LASTEXITCODE = 1 }
        return
    }
    if ($a[0] -eq 'exec') {
        if (-not $cfg.noClient) {
            '  sl  local_address rem_address   st'
            '   0: 0100007F:1837 0100007F:C350 01 00000000:00000000'
        }
        return
    }
    if ($a[0] -eq 'compose') {
        if ($a -contains 'ps') {
            $service = $a[-1]
            if ($service -eq 'astrbot' -and $global:DFTestAstrExists) { 'a' * 64 }
            if ($service -eq 'napcat' -and $cfg.napExists) { 'b' * 64 }
            return
        }
        if ($a -contains 'up') {
            if ($cfg.composeFail) { $global:LASTEXITCODE = 1; return }
            $global:DFTestAstrExists = $true; $global:DFTestAstrRunning = $true; return
        }
    }
    if ($a[0] -eq 'inspect') {
        if ($cfg.inspectFail) { $global:LASTEXITCODE = 1; return }
        $isAstr = $a[-1] -eq ('a' * 64)
        $running = if ($isAstr) { $global:DFTestAstrRunning } else { $global:DFTestNapRunning }
        $exitCode = if ($global:DFTestStoppedAstr -and $isAstr -and $cfg.killed) { 137 } else { 0 }
        @{Running=$running; Paused=[bool]$cfg.paused; Restarting=$false;
          Status=$(if ($running) {'running'} else {'exited'});
          ExitCode=$exitCode; OOMKilled=$false} | ConvertTo-Json -Compress
        return
    }
    if ($a[0] -eq 'port') { '127.0.0.1:16185'; return }
    if ($a[0] -eq 'stop') {
        if ($cfg.stopFail) { $global:LASTEXITCODE = 1; return }
        if ($a[-1] -eq ('a' * 64)) {
            $global:DFTestStoppedAstr = $true
            if (-not $cfg.stillRunning) { $global:DFTestAstrRunning = $false }
        } else { $global:DFTestNapRunning = $false }
        return
    }
    if ($a[0] -eq 'ps') {
        if ($cfg.listFail) { $global:LASTEXITCODE = 1; return }
        if ($cfg.other) { 'other-app' }
        return
    }
    throw "Unexpected Docker command: $joined"
}
function Get-Command {
    param($Name, $ErrorAction)
    if ($Name -eq 'docker') { return [pscustomobject]@{Source='docker'} }
    if ($Name -eq 'node') { return [pscustomobject]@{Source='node.exe'} }
    if ($Name -eq 'pythonw' -and $cfg.python) {
        return [pscustomobject]@{Source='pythonw.exe'}
    }
}
function Get-Process {
    param($Name, $ErrorAction)
    if ($Name -contains 'QQ') {
        if ($cfg.qqRunning) { [pscustomobject]@{Id=777; ProcessName='QQ'} }
        return
    }
    if ($global:DFTestDesktop) { [pscustomobject]@{Id=123; ProcessName='Docker Desktop'} }
}
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    if ($Filter -like "Name = 'node.exe'*") {
        if ($global:DFTestOpenQQLaunched) {
            $global:DFTestOpenQQProbes++
            if ($cfg.openqqStartDelayQueries -and $global:DFTestOpenQQProbes -ge $cfg.openqqStartDelayQueries) {
                $global:DFTestOpenQQ = $true
                if ($cfg.loginPhase) { WriteStatus $cfg.loginPhase }
            }
            if ($cfg.openqqExitAfterQueries -and $global:DFTestOpenQQProbes -ge $cfg.openqqExitAfterQueries) {
                $global:DFTestOpenQQ = $false
            }
        }
        Record 'openqq-probe' @{present=$global:DFTestOpenQQ; afterLaunch=$global:DFTestOpenQQLaunched}
        if ($global:DFTestOpenQQ) {
            [pscustomobject]@{ProcessId=5555; CommandLine='"node.exe" src/main.mjs start'}
        }
        [pscustomobject]@{ProcessId=5556; CommandLine='"node.exe" other-tool.js'}
        return
    }
    if ($cfg.cimFail) { throw 'Process query denied' }
    if ($cfg.watchdog -eq 'missing') { return }
    $command = if ($cfg.watchdog -eq 'foreign') { 'pythonw.exe C:\other\watchdog.py' }
               else { 'pythonw.exe "' + (Join-Path $PSScriptRoot 'deploy-windows\watchdog.py') + '"' }
    [pscustomobject]@{ProcessId=4242; CommandLine=$command}
}
function Stop-Process {
    param($Id, [switch]$Force, $ErrorAction)
    Record 'stop-process' $Id
    if ($cfg.watchdogStopFail) { throw 'Process stop denied' }
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru)
    Record 'start-process' @{path=$FilePath; args=@($ArgumentList); style=$WindowStyle; cwd=$WorkingDirectory}
    if ($FilePath -eq 'powershell.exe') {
        $global:DFTestOpenQQLaunched = $true
        $global:DFTestOpenQQProbes = 0
        $global:DFTestOpenQQ = -not ($cfg.openqqDies -or $cfg.openqqNeverStarts -or $cfg.openqqStartDelayQueries)
        if ($cfg.loginPhase -and ($global:DFTestOpenQQ -or $cfg.earlyReady)) { WriteStatus $cfg.loginPhase }
        return [pscustomobject]@{Id=4242; HasExited=[bool]$cfg.openqqDies}
    }
    [pscustomobject]@{Id=4242}
}
function Invoke-WebRequest {
    param($Uri, [switch]$UseBasicParsing, $TimeoutSec, $ErrorAction)
    Record 'http' ([string]$Uri)
    if ($cfg.panelFail) { throw 'Panel unavailable' }
    [pscustomobject]@{StatusCode=200}
}
function Start-Sleep { param($Seconds, $Milliseconds); [System.Threading.Thread]::Sleep(25) }
$entry = Join-Path $PSScriptRoot $cfg.entry
$callArgs = @{DockerTimeoutSeconds=1}
if ($cfg.entry -like '*daily.ps1') { $callArgs.Action = $cfg.action } else { $callArgs.NoPause = $true }
if ($cfg.action -eq 'Start') {
    $callArgs.ReadyTimeoutSeconds = 1
    $callArgs.LoginTimeoutSeconds = 1
    $callArgs.NoWatchdog = -not $cfg.python
    if ($cfg.noOpenQQ) { $callArgs.NoOpenQQ = $true }
    if ($cfg.entry -like '*daily.ps1') { $callArgs.ConnectTimeoutSeconds = 1 }
} else {
    $callArgs.StopTimeoutSeconds = 120
    $callArgs.KeepDocker = [bool]$cfg.keepDocker
}
if ($cfg.busy) {
    # An independent process holds the real named mutex for this fixture.
    $lockPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'deploy-windows')).ToLowerInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($lockPath))).Replace('-', '')
    $mutex = New-Object Threading.Mutex($false, ('Local\DafeiyuLifecycle-' + $hash))
    [void]$mutex.WaitOne()
    $callArgs | ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot 'call.json')
    $child = 'try { $p = Get-Content -Raw $env:DF_CHILD_ARGS | ConvertFrom-Json; $h=@{}; $p.psobject.Properties | ForEach-Object {$h[$_.Name]=$_.Value}; & $env:DF_CHILD_ENTRY @h } catch { Write-Output $_; exit 1 }'
    $env:DF_CHILD_ARGS = Join-Path $PSScriptRoot 'call.json'
    $env:DF_CHILD_ENTRY = $entry
    & ([Diagnostics.Process]::GetCurrentProcess().MainModule.FileName) -NoProfile -ExecutionPolicy Bypass -Command $child
    exit $LASTEXITCODE
}
try { & $entry @callArgs } catch { Write-Output "ERROR: $($_.Exception.Message)"; exit 1 }
exit $LASTEXITCODE
'''


@unittest.skipUnless(SHELL, "PowerShell is required")
class WindowsLifecycleTests(unittest.TestCase):
    def run_case(self, action="Stop", **overrides):
        cfg = dict(action=action, engine=True, desktop=True, running=True,
                   endpoint="npipe:////./pipe/docker_engine", watchdog="missing",
                   entry="启动大肥鱼.ps1" if action == "Start" else "安全关闭大肥鱼.ps1")
        if action == "Start":
            # 默认：OpenQQ 已在跑且已登录，客户端已连上 6199
            cfg.update(openqqRunning=True, statusPhase="session_ready")
        cfg.update(overrides)
        with tempfile.TemporaryDirectory(prefix="dafeiyu lifecycle ") as temp:
            folder = Path(temp)
            kit = folder / "deploy-windows"
            kit.mkdir()
            openqq = folder / "OpenQQ"
            (openqq / "src").mkdir(parents=True)
            (openqq / "src" / "main.mjs").write_text("// stub", encoding="utf-8")
            for name in ("启动大肥鱼.ps1", "安全关闭大肥鱼.ps1"):
                shutil.copy2(ROOT / name, folder / name)
            for name in ("daily.ps1", "watchdog.py", "compose.yaml"):
                shutil.copy2(ROOT / "deploy-windows" / name, kit / name)
            if not cfg.get("noOpenQQDir"):
                (kit / ".env").write_text(f"TZ=Asia/Shanghai\nOPENQQ_DIR={openqq}\n", encoding="utf-8")
            if cfg["watchdog"] != "missing":
                (kit / "logs").mkdir()
                (kit / "logs" / "watchdog.pid").write_text("4242", encoding="utf-8")
            config = folder / "config.json"
            config.write_text(json.dumps(cfg), encoding="utf-8")
            harness = folder / "harness.ps1"
            harness.write_text(HARNESS, encoding="utf-8-sig")
            events_path = folder / "events.jsonl"
            env = dict(os.environ, DF_TEST_CONFIG=str(config), DF_TEST_EVENTS=str(events_path),
                       DF_TEST_OPENQQ=str(openqq))
            # These tests must never inherit a real remote Docker override or OpenQQ location.
            env.pop("DOCKER_HOST", None)
            env.pop("OPENQQ_DIR", None)
            if cfg.get("remoteEnv"):
                env["DOCKER_HOST"] = "tcp://example.invalid:2375"
            result = subprocess.run([SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                                    capture_output=True, encoding="utf-8", errors="replace", env=env, timeout=30)
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8-sig").splitlines()] if events_path.exists() else []
            return result, events

    def commands(self, events):
        return [event["value"] for event in events if event["kind"] == "docker"]

    def launches(self, events, path):
        return [e["value"] for e in events if e["kind"] == "start-process" and e["value"]["path"] == path]

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_failed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    # ---------- 关闭 ----------

    def test_stop_drains_brain_then_desktop(self):
        result, events = self.run_case(watchdog="ours")
        self.assert_ok(result)
        commands = self.commands(events)
        stops = [cmd for cmd in commands if cmd[0] == "stop"]
        self.assertEqual([cmd[-1] for cmd in stops], [ASTR])
        self.assertTrue(all("120" in cmd for cmd in stops))
        self.assertLess(next(i for i, e in enumerate(events) if e["kind"] == "stop-process"),
                        next(i for i, e in enumerate(events) if e["kind"] == "docker" and e["value"][0] == "stop"))
        self.assertTrue(any(cmd[:2] == ["desktop", "stop"] for cmd in commands))
        self.assertFalse(any("--force" in cmd or "down" in cmd or "rm" in cmd or "volume" in cmd for cmd in commands))

    def test_stop_also_stops_manually_started_napcat_after_astrbot(self):
        result, events = self.run_case(napExists=True, napRunning=True)
        self.assert_ok(result)
        self.assertEqual([cmd[-1] for cmd in self.commands(events) if cmd[0] == "stop"], [ASTR, NAP])

    def test_stop_leaves_openqq_logged_in(self):
        result, events = self.run_case(openqqRunning=True)
        self.assert_ok(result)
        self.assertFalse(any(e["kind"] == "stop-process" for e in events))
        self.assertIn("OpenQQ", result.stdout)

    def test_other_containers_keep_docker_running(self):
        result, events = self.run_case(other=True)
        self.assert_ok(result)
        self.assertFalse(any(cmd[:2] == ["desktop", "stop"] for cmd in self.commands(events)))
        self.assertIn("other-app", result.stdout)

    def test_keep_docker_option(self):
        result, events = self.run_case(keepDocker=True)
        self.assert_ok(result)
        self.assertFalse(any(cmd[:2] == ["desktop", "stop"] for cmd in self.commands(events)))

    def test_already_off_is_idempotent_without_starting_docker(self):
        result, events = self.run_case(engine=False, desktop=False, running=False)
        self.assert_ok(result)
        self.assertFalse(any(cmd[0] in ("stop", "compose", "desktop") for cmd in self.commands(events)))

    def test_docker_info_zero_exit_with_empty_version_is_not_ready(self):
        # Docker 28 info --format returns 0 even when its daemon is absent.
        result, events = self.run_case(engine=False, desktop=False, running=False, infoFalseSuccess=True)
        self.assert_ok(result)
        self.assertFalse(any(cmd[0] in ("stop", "compose", "desktop") for cmd in self.commands(events)))

    def test_already_stopped_containers_are_not_stopped_again(self):
        result, events = self.run_case(running=False)
        self.assert_ok(result)
        self.assertFalse(any(cmd[0] == "stop" for cmd in self.commands(events)))

    def test_stop_failure_keeps_desktop_and_reports_error(self):
        for scenario in ({"stopFail": True}, {"stillRunning": True}, {"inspectFail": True}, {"listFail": True}):
            with self.subTest(scenario=scenario):
                result, events = self.run_case(**scenario)
                self.assert_failed(result)
                self.assertFalse(any(cmd[:2] == ["desktop", "stop"] for cmd in self.commands(events)))

    def test_forced_kill_is_reported_and_keeps_docker(self):
        result, events = self.run_case(killed=True, napExists=True, napRunning=True)
        self.assert_failed(result)
        self.assertIn("137", result.stdout + result.stderr)
        self.assertEqual([cmd[-1] for cmd in self.commands(events) if cmd[0] == "stop"], [ASTR, NAP])
        self.assertFalse(any(cmd[:2] == ["desktop", "stop"] for cmd in self.commands(events)))

    def test_unreachable_running_desktop_is_not_treated_as_stopped(self):
        result, events = self.run_case(engine=False, desktop=True)
        self.assert_failed(result)
        self.assertFalse(any(cmd[:2] == ["desktop", "stop"] for cmd in self.commands(events)))

    def test_foreign_watchdog_pid_is_never_killed(self):
        result, events = self.run_case(watchdog="foreign")
        self.assert_ok(result)
        self.assertFalse(any(event["kind"] == "stop-process" for event in events))

    def test_watchdog_stop_and_query_errors_block_shutdown(self):
        for scenario in ({"cimFail": True}, {"watchdogStopFail": True}):
            with self.subTest(scenario=scenario):
                result, events = self.run_case(watchdog="ours", **scenario)
                self.assert_failed(result)
                self.assertFalse(any(cmd[0] == "stop" for cmd in self.commands(events)))

    def test_desktop_failure_or_timeout_is_not_success(self):
        for scenario in ({"desktopStopFail": True}, {"desktopStillRunning": True}):
            with self.subTest(scenario=scenario):
                result, _ = self.run_case(**scenario)
                self.assert_failed(result)

    # ---------- 启动：容器与数据卷 ----------

    def test_start_boots_docker_and_brings_up_only_astrbot(self):
        result, events = self.run_case("Start", engine=False, desktop=False, running=False)
        self.assert_ok(result)
        commands = self.commands(events)
        self.assertTrue(any(cmd[:2] == ["desktop", "start"] for cmd in commands))
        ups = [cmd for cmd in commands if cmd[0] == "compose" and "up" in cmd]
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0][-1], "astrbot")
        for flag in ("--no-deps", "--no-build", "--detach"):
            self.assertIn(flag, ups[0])
        self.assertEqual(ups[0][ups[0].index("--pull") + 1], "never")
        self.assertFalse(any("napcat" in cmd and ("up" in cmd or "start" in cmd) for cmd in commands))
        self.assertFalse(any(cmd[0] in ("build", "pull") or "build" == cmd[-1] for cmd in commands))
        self.assertEqual([e["value"] for e in events if e["kind"] == "http"], ["http://127.0.0.1:16185/"])

    def test_start_checks_data_volume_before_up(self):
        result, events = self.run_case("Start")
        self.assert_ok(result)
        commands = self.commands(events)
        probe = next(i for i, c in enumerate(commands) if c[0] == "run")
        up = next(i for i, c in enumerate(commands) if c[0] == "compose" and "up" in c)
        self.assertLess(probe, up)
        run = commands[probe]
        self.assertIn("type=volume,source=dafeiyu-astrbot-data,target=/d,readonly", run)
        self.assertIn("/d/data_v4.db", run)

    def test_missing_or_empty_volume_refuses_to_start_blank_instance(self):
        for scenario in ({"noVolume": True}, {"emptyVolume": True}):
            with self.subTest(scenario=scenario):
                result, events = self.run_case("Start", **scenario)
                self.assert_failed(result)
                self.assertIn("migrate-data-volume.ps1", result.stdout + result.stderr)
                self.assertFalse(any(c[0] == "compose" and "up" in c for c in self.commands(events)))

    def test_running_napcat_is_stopped_before_astrbot_comes_up(self):
        result, events = self.run_case("Start", napExists=True, napRunning=True)
        self.assert_ok(result)
        commands = self.commands(events)
        stop = next(i for i, c in enumerate(commands) if c[0] == "stop" and c[-1] == NAP)
        up = next(i for i, c in enumerate(commands) if c[0] == "compose" and "up" in c)
        self.assertLess(stop, up)

    def test_stopped_napcat_is_left_alone(self):
        result, events = self.run_case("Start", napExists=True, napRunning=False)
        self.assert_ok(result)
        self.assertFalse(any(c[0] == "stop" for c in self.commands(events)))

    def test_start_creates_missing_astrbot_container_from_compose(self):
        result, events = self.run_case("Start", missing="astrbot", running=False)
        self.assert_ok(result)
        self.assertTrue(any(c[0] == "compose" and "up" in c for c in self.commands(events)))

    def test_start_errors_do_not_report_readiness(self):
        for scenario in ({"startFail": True, "engine": False}, {"engineTimeout": True, "engine": False},
                         {"composeFail": True, "running": False}, {"panelFail": True}, {"paused": True}):
            with self.subTest(scenario=scenario):
                result, events = self.run_case("Start", python=True, **scenario)
                self.assert_failed(result)
                self.assertNotIn("大肥鱼已启动", result.stdout)
                self.assertFalse(self.launches(events, "pythonw.exe"))

    # ---------- 启动：OpenQQ ----------

    def test_running_openqq_is_reused_not_relaunched(self):
        result, events = self.run_case("Start")
        self.assert_ok(result)
        self.assertFalse(self.launches(events, "powershell.exe"))
        self.assertTrue(any(c[0] == "exec" for c in self.commands(events)))
        self.assertIn("大肥鱼已启动", result.stdout)

    def test_openqq_is_launched_in_visible_window_and_waits_for_login(self):
        result, events = self.run_case("Start", python=True, openqqRunning=False, statusPhase="session_ready",
                                       loginPhase="session_ready")
        self.assert_ok(result)
        launches = self.launches(events, "powershell.exe")
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0]["style"], "Normal")
        self.assertIn("-NoExit", launches[0]["args"])
        self.assertTrue(any("node src/main.mjs start" in a for a in launches[0]["args"]))
        self.assertTrue(launches[0]["cwd"].endswith("OpenQQ"))
        # 监控在登录完成之后才启动
        kinds = [(e["kind"], e["value"].get("path") if isinstance(e["value"], dict) else None) for e in events]
        self.assertLess(kinds.index(("start-process", "powershell.exe")), kinds.index(("start-process", "pythonw.exe")))

    def test_delayed_node_start_reaches_login_and_starts_monitor_once(self):
        # 窗口已经创建，前三次查询后 Node 才出现；真实入口不能立即误报退出。
        result, events = self.run_case("Start", python=True, openqqRunning=False,
                                       statusPhase="starting", openqqStartDelayQueries=3,
                                       loginPhase="session_ready")
        self.assert_ok(result)
        self.assertEqual(len(self.launches(events, "powershell.exe")), 1)
        self.assertEqual(len(self.launches(events, "pythonw.exe")), 1)
        probes = [e["value"]["present"] for e in events
                  if e["kind"] == "openqq-probe" and e["value"]["afterLaunch"]]
        # 状态读取与进程查询不是原子操作，允许 ready 在下一轮才被读取。
        self.assertEqual(probes[:3], [False, False, True])
        self.assertTrue(all(probes[2:]))

    def test_fresh_ready_status_still_requires_running_openqq_process(self):
        # 新状态文件不能代替进程已启动；监控必须等到进程实际出现。
        result, events = self.run_case("Start", python=True, openqqRunning=False,
                                       statusPhase="starting", openqqStartDelayQueries=3,
                                       loginPhase="session_ready", earlyReady=True)
        self.assert_ok(result)
        probes = [e["value"]["present"] for e in events if e["kind"] == "openqq-probe"]
        self.assertTrue(probes[-1])
        self.assertEqual(len(self.launches(events, "pythonw.exe")), 1)

    def test_never_started_node_times_out_without_claiming_window_exited(self):
        result, events = self.run_case("Start", python=True, openqqRunning=False,
                                       openqqNeverStarts=True, statusPhase="starting")
        self.assert_failed(result)
        self.assertIn("未检测到 OpenQQ 进程启动", result.stdout + result.stderr)
        self.assertNotIn("OpenQQ 窗口退出了", result.stdout + result.stderr)
        self.assertFalse(self.launches(events, "pythonw.exe"))

    def test_observed_node_exiting_before_login_is_failure(self):
        result, events = self.run_case("Start", python=True, openqqRunning=False,
                                       statusPhase="starting", openqqExitAfterQueries=2,
                                       loginPhase="hook_installed")
        self.assert_failed(result)
        self.assertIn("OpenQQ 窗口退出了", result.stdout + result.stderr)
        probes = [e["value"]["present"] for e in events
                  if e["kind"] == "openqq-probe" and e["value"]["afterLaunch"]]
        self.assertEqual(probes, [True, False])
        self.assertFalse(self.launches(events, "pythonw.exe"))

    def test_stale_ready_status_from_previous_run_is_not_trusted(self):
        # status.json 还停在上次的 session_ready，新拉起的 OpenQQ 一直没写状态 → 应等到超时
        result, events = self.run_case("Start", python=True, openqqRunning=False, statusPhase="session_ready",
                                       statusAgeSeconds=3600)
        self.assert_failed(result)
        self.assertIn("登录", result.stdout + result.stderr)
        self.assertFalse(self.launches(events, "pythonw.exe"))

    def test_qq_already_running_blocks_openqq_launch(self):
        result, events = self.run_case("Start", python=True, openqqRunning=False, qqRunning=True)
        self.assert_failed(result)
        self.assertIn("退出所有 QQ", result.stdout + result.stderr)
        self.assertFalse(self.launches(events, "powershell.exe"))
        self.assertFalse(self.launches(events, "pythonw.exe"))
        # AstrBot 照样起来了
        self.assertTrue(any(c[0] == "compose" and "up" in c for c in self.commands(events)))

    def test_openqq_failure_phases_are_reported(self):
        for phase in ("offline", "failed", "hook_error"):
            with self.subTest(phase=phase):
                result, _ = self.run_case("Start", openqqRunning=True, statusPhase=phase)
                self.assert_failed(result)
                self.assertNotIn("大肥鱼已启动", result.stdout)

    def test_login_timeout_is_not_success(self):
        result, _ = self.run_case("Start", openqqRunning=True, statusPhase="hook_installed")
        self.assert_failed(result)
        self.assertIn("没等到 QQ 登录完成", result.stdout + result.stderr)

    def test_openqq_window_exiting_is_reported(self):
        result, _ = self.run_case("Start", openqqRunning=False, openqqDies=True, loginPhase="starting")
        self.assert_failed(result)
        self.assertIn("OpenQQ 窗口退出了", result.stdout + result.stderr)

    def test_no_onebot_connection_is_not_success(self):
        result, _ = self.run_case("Start", entry="deploy-windows/daily.ps1", noClient=True)
        self.assert_failed(result)
        self.assertIn("6199", result.stdout + result.stderr)

    def test_missing_openqq_dir_or_opt_out_skips_openqq(self):
        for scenario in ({"noOpenQQDir": True}, {"noOpenQQ": True}):
            with self.subTest(scenario=scenario):
                result, events = self.run_case("Start", openqqRunning=False, **scenario)
                self.assert_ok(result)
                self.assertFalse(self.launches(events, "powershell.exe"))
                self.assertFalse(any(c[0] == "exec" for c in self.commands(events)))

    # ---------- 监控 / 安全 ----------

    def test_monitor_is_started_hidden_and_existing_one_is_reused(self):
        result, events = self.run_case("Start", python=True)
        self.assert_ok(result)
        starts = self.launches(events, "pythonw.exe")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["style"], "Hidden")
        result, events = self.run_case("Start", python=True, watchdog="ours")
        self.assert_ok(result)
        self.assertFalse(self.launches(events, "pythonw.exe"))

    def test_remote_docker_context_or_environment_is_refused(self):
        for scenario in ({"endpoint": "ssh://example.invalid"}, {"remoteEnv": True}):
            with self.subTest(scenario=scenario):
                result, events = self.run_case(**scenario)
                self.assert_failed(result)
                self.assertFalse(any(cmd[0] == "stop" for cmd in self.commands(events)))

    def test_concurrent_operation_is_rejected_before_touching_docker(self):
        result, events = self.run_case(busy=True)
        self.assert_failed(result)
        self.assertFalse(self.commands(events))
        self.assertIn("另一个", result.stdout + result.stderr)



class ComposeShutdownTests(unittest.TestCase):
    """AstrBot runs as PID 1 and only handles SIGINT; Docker's default SIGTERM is ignored
    and every `docker stop` ended in a forced kill (137). Keep the stop signal pinned."""

    def compose(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is required")
        return yaml.safe_load((ROOT / "deploy-windows" / "compose.yaml").read_text(encoding="utf-8"))

    def test_astrbot_stops_with_sigint_and_a_grace_period(self):
        service = self.compose()["services"]["astrbot"]
        self.assertEqual(service.get("stop_signal"), "SIGINT")
        self.assertIn("stop_grace_period", service)

    def test_napcat_profile_and_external_data_volume(self):
        compose = self.compose()
        self.assertEqual(compose["services"]["napcat"].get("profiles"), ["napcat"])
        self.assertTrue(compose["volumes"]["astrbot-data"].get("external"))


if __name__ == "__main__":
    unittest.main()
