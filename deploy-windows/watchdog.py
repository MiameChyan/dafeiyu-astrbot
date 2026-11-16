# -*- coding: utf-8 -*-
"""大肥鱼 · QQ 掉线 / 假在线监控（Windows 本机，只用标准库）。

start.ps1 会在后台用 pythonw 拉起它；stop.ps1 会停掉。也可以手动：
    python watchdog.py --once          # 查一次，打印结果
    python watchdog.py --test-notify   # 只弹一个测试通知

协议端由 WATCHDOG_PROTOCOL 决定：napcat / openqq / auto（默认：OpenQQ 在跑就查 OpenQQ，否则 NapCat 容器在跑就查 NapCat）。
每隔 WATCHDOG_INTERVAL_MIN 分钟检查：
  硬信号（任一失败，60 秒后复查仍失败 → 掉线）
    NapCat：
    - NapCat WebUI 连得上
    - WebUI 登录状态：已登录、未离线
    - OneBot get_status：online
    - OneBot nc_get_rkey：要真的去 QQ 服务器取，会话失效时会失败
    OpenQQ（宿主机上的 node src/main.mjs start）：
    - OpenQQ 进程在跑
    - OPENQQ_DIR/.runtime/status.json 的 phase 是 session_ready（被踢下线时 OpenQQ 会写成 offline）
    - 挂接的 QQ 进程还活着
    - AstrBot 容器在跑，且 6199 上有已建立的连接
  软信号（→ 疑似假在线）
    - 白天（北京时间 WATCHDOG_ACTIVE_HOURS）连续 WATCHDOG_SILENCE_HOURS 小时一条消息都没收到
      （NapCat 看自己的「接收 <-」日志；OpenQQ 看 AstrBot 的 core.event_bus 日志）
状态变化时弹 Windows 通知；没恢复就每 2 小时提醒；恢复时再通知一次。
只通知，不重启任何东西。日志写在 logs/，长数字（QQ 号、群号）一律打码。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

KIT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(KIT, "logs")
RECHECK_SECONDS = 60
REMIND_SECONDS = 2 * 3600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 「白天」按北京时间算：群友作息和拟人节奏的深夜时段都是北京时间，与本机时区无关
BEIJING = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 纯逻辑
def mask(text) -> str:
    if not text:
        return ""
    return re.sub(r"\d{5,}", "<num>", str(text))


def count_received(log_text: str) -> int:
    return sum(1 for line in (log_text or "").splitlines() if "接收 <-" in line)


def parse_env(text: str) -> dict:
    env = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*([^#]*)", line)
        if m:
            env[m.group(1)] = m.group(2).strip()
    return env


def parse_docker_time(value: str):
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", (value or "").strip())
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    try:
        base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return base.replace(microsecond=int(frac), tzinfo=timezone.utc)


def silence_applicable(now: datetime, started_at, hours: float, active=(9, 24)) -> bool:
    """整个静默窗口都落在今天的活跃时段里、且 NapCat 已经跑满一个窗口，才看静默。"""
    if started_at is None:
        return False
    window_start = now - timedelta(hours=hours)
    if started_at > window_start:
        return False
    local_start = window_start.astimezone(now.tzinfo)
    if local_start.date() != now.date():
        return False
    return local_start.hour >= active[0] and now.hour < active[1]


PROTOCOLS = ("napcat", "openqq")
OPENQQ_START = re.compile(r'main\.mjs"?\s+start\b')
OPENQQ_PHASES = {
    "offline": "QQ 账号被迫下线（OpenQQ 报告已掉线）",
    "hook_error": "OpenQQ 接入 QQ 出错",
    "hook_installed": "QQ 还没登录（OpenQQ 在等登录）",
    "starting": "OpenQQ 还没接上 QQ",
    "detached": "OpenQQ 已解除挂接",
    "failed": "OpenQQ 启动失败",
}
DOWN_HINTS = {
    "napcat": "打开 NapCat WebUI 看看，多半要重新扫码。",
    "openqq": "看看 OpenQQ 终端：被踢下线就在它拉起的 QQ 里重新登录，终端关了就退出所有 QQ 后重新 node src/main.mjs start。",
    "none": "按 README 把 OpenQQ 或 NapCat 拉起来。",
}


def choose_protocol(setting, openqq_running: bool, napcat_running: bool) -> str:
    """显式设置优先；auto 时 OpenQQ（手动开的终端，更能代表本意）优先于 NapCat 容器。"""
    if setting in PROTOCOLS:
        return setting
    if openqq_running:
        return "openqq"
    if napcat_running:
        return "napcat"
    return "none"


def find_openqq(procs):
    for proc in procs or []:
        if OPENQQ_START.search(str(proc.get("cmd") or "")):
            return proc
    return None


def parse_processes(text: str) -> list:
    """PowerShell ConvertTo-Json 的输出：一个对象、对象数组或空。"""
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict)]


def parse_openqq_status(text: str) -> dict:
    try:
        data = json.loads(text or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def count_established(proc_text: str, port: int = 6199) -> int:
    """数 /proc/net/tcp(6) 里本地端口为 port、状态 01（ESTABLISHED）的连接，即连进 AstrBot 的客户端。"""
    want = f"{port:04X}"
    n = 0
    for line in (proc_text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or ":" not in parts[1]:
            continue
        if parts[1].rsplit(":", 1)[1].upper() == want and parts[3] == "01":
            n += 1
    return n


def count_astrbot_messages(log_text: str) -> int:
    return sum(1 for line in (log_text or "").splitlines() if "[core.event_bus" in line)


def _silence(obs: dict):
    if obs.get("silence_applicable") and obs.get("received") == 0:
        hours = obs.get("silence_hours", 3)
        return "suspect", [f"已经 {hours:g} 小时一条消息都没收到"]
    return "ok", []


def _classify_openqq(obs: dict):
    if obs.get("openqq_running") is False:
        return "down", ["OpenQQ 没在运行（终端被关了或崩了）"]
    reasons = []
    phase = obs.get("phase")
    if phase and phase != "session_ready":
        reasons.append(OPENQQ_PHASES.get(phase, f"OpenQQ 状态异常：{mask(phase)[:40]}"))
    if obs.get("qq_alive") is False:
        reasons.append("OpenQQ 挂接的 QQ 进程已经退出")
    if obs.get("astrbot_running") is False:
        reasons.append("AstrBot 容器没在运行")
    elif obs.get("ws_connections") == 0:
        reasons.append("OpenQQ 没连上 AstrBot（6199 端口没有连接）")
    if reasons:
        return "down", reasons
    return _silence(obs)


def classify(obs: dict):
    """返回 (level, reasons)；level 为 ok / suspect / down。None 表示没查到，不算失败。"""
    protocol = obs.get("protocol", "napcat")
    if protocol == "none":
        return "down", ["OpenQQ 和 NapCat 都没在运行"]
    if protocol == "openqq":
        return _classify_openqq(obs)
    if not obs.get("webui"):
        return "down", ["连不上 NapCat WebUI（容器没起来、崩了，或 Docker 没开）"]
    reasons = []
    err = mask(obs.get("login_error"))
    if obs.get("is_login") is False:
        reasons.append("NapCat 显示未登录" + (f"：{err}" if err else ""))
    if obs.get("is_offline") is True:
        reasons.append("NapCat 报告账号已离线" + (f"：{err}" if err and obs.get("is_login") is not False else ""))
    if obs.get("online") is False:
        reasons.append("OneBot get_status 显示离线")
    if obs.get("rkey_ok") is False:
        reasons.append("向 QQ 服务器取 rkey 失败（会话可能已失效）")
    if reasons:
        return "down", reasons
    return _silence(obs)


def confirm(observe, sleep, recheck_seconds: float = RECHECK_SECONDS):
    """硬失败先等一会儿再查一次，避免网络抖动误报。"""
    level, reasons = classify(observe())
    if level != "down":
        return level, reasons
    sleep(recheck_seconds)
    return classify(observe())


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes / 60:.1f} 小时"


def decide(prev, level: str, reasons, now: float, remind_seconds: float = REMIND_SECONDS, protocol: str = "napcat"):
    """根据上次状态决定是否通知。返回 (新状态, (标题, 正文) 或 None)。"""
    prev_level = prev.get("level") if prev else None
    detail = mask("；".join(reasons))
    if prev_level != level:
        state = {"level": level, "since": now, "last_notified": prev.get("last_notified", 0) if prev else 0}
        msg = None
        if level == "down":
            msg = ("大肥鱼 QQ 掉线了", f"{detail}。{DOWN_HINTS.get(protocol, DOWN_HINTS['napcat'])}")
        elif level == "suspect":
            msg = ("大肥鱼可能假在线", f"{detail}。私聊她一句试试回不回。")
        elif prev_level in ("down", "suspect"):
            msg = ("大肥鱼 QQ 恢复了", f"异常持续了约 {_duration(now - prev.get('since', now))}。")
        if msg:
            state["last_notified"] = now
        return state, msg
    state = dict(prev)
    if level in ("down", "suspect") and now - prev.get("last_notified", 0) >= remind_seconds:
        state["last_notified"] = now
        title = "大肥鱼 QQ 仍然掉线" if level == "down" else "大肥鱼仍疑似假在线"
        return state, (title, f"已持续 {_duration(now - prev.get('since', now))}：{detail}")
    return state, None


# ---------------------------------------------------------------- 外部交互
def load_settings() -> dict:
    env = {}
    path = os.path.join(KIT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            env = parse_env(f.read())

    def num(key, default):
        try:
            return float(env.get(key) or default)
        except ValueError:
            return default

    active = (9, 24)
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", env.get("WATCHDOG_ACTIVE_HOURS", ""))
    if m and 0 <= int(m.group(1)) < int(m.group(2)) <= 24:
        active = (int(m.group(1)), int(m.group(2)))
    return {
        "port": int(num("NAPCAT_PORT", 6099)),
        "interval": max(1.0, num("WATCHDOG_INTERVAL_MIN", 10)) * 60,
        "silence_hours": max(0.5, num("WATCHDOG_SILENCE_HOURS", 3)),
        "active": active,
        "protocol": (env.get("WATCHDOG_PROTOCOL") or "auto").strip().lower(),
        "openqq_dir": env.get("OPENQQ_DIR") or None,
    }


class WebUI:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}/api"
        self.cred = None

    def _post(self, path, body, timeout=10):
        headers = {"Content-Type": "application/json"}
        if self.cred:
            headers["Authorization"] = "Bearer " + self.cred
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    def login(self):
        with open(os.path.join(KIT, "napcat", "config", "webui.json"), encoding="utf-8") as f:
            token = json.load(f)["token"]
        digest = hashlib.sha256((token + ".napcat").encode()).hexdigest()
        self.cred = (self._post("/auth/login", {"hash": digest}).get("data") or {}).get("Credential")
        if not self.cred:
            raise RuntimeError("WebUI 登录失败")

    def login_status(self) -> dict:
        return self._post("/QQLogin/CheckLoginStatus", {}).get("data") or {}

    def call(self, action, timeout=20):
        """经 WebUI 调试接口调 OneBot 动作，返回内层 OneBot 响应；失败返回 None。"""
        r = self._post("/Debug/call", {"action": action, "params": {}}, timeout=timeout)
        if r.get("code") != 0 or not isinstance(r.get("data"), dict):
            return None
        return r["data"]


def _docker(*args, timeout=30) -> str:
    out = subprocess.run(
        ["docker", "compose", *args],
        cwd=KIT, capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
    )
    return out.stdout.decode("utf-8", errors="replace")


def _powershell(script: str, timeout=30) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
    )
    return out.stdout.decode("utf-8", errors="replace")


NODE_PROCESSES = """
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$p = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | ForEach-Object {
  $t = ''
  if ($_.CreationDate) { $t = $_.CreationDate.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'") }
  [pscustomobject]@{ pid = $_.ProcessId; cmd = $_.CommandLine; started = $t }
})
ConvertTo-Json -InputObject $p -Compress
"""


class System:
    """observe 用到的所有系统调用；测试里换成假的。"""

    def __init__(self, settings: dict):
        self.settings = settings

    def node_processes(self) -> list:
        text = _powershell(NODE_PROCESSES).strip()
        if not text.startswith(("[", "{")):
            raise RuntimeError("列不出 node 进程")
        return parse_processes(text)

    def openqq_status(self) -> dict:
        root = self.settings.get("openqq_dir")
        if not root:
            return {}
        try:
            with open(os.path.join(root, ".runtime", "status.json"), encoding="utf-8") as f:
                return parse_openqq_status(f.read())
        except OSError:
            return {}

    def pid_alive(self, pid: int):
        """进程还在且确实是 QQ.exe 才算活着（防 pid 被复用）；查不了返回 None。"""
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None if ctypes.get_last_error() == 5 else False  # 5 = 拒绝访问：进程在，但看不了
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # 259 = STILL_ACTIVE
                return False
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return os.path.basename(buf.value).lower() == "qq.exe"
        finally:
            k32.CloseHandle(handle)

    def service_running(self, name: str) -> bool:
        return bool(_docker("ps", "-q", name).strip())

    def started_at(self, name: str):
        cid = _docker("ps", "-q", name).strip()
        if not cid:
            return None
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", cid],
            capture_output=True, timeout=30, creationflags=NO_WINDOW,
        )
        return parse_docker_time(out.stdout.decode("utf-8", errors="replace"))

    def proc_net(self) -> str:
        return _docker("exec", "-T", "astrbot", "cat", "/proc/net/tcp", "/proc/net/tcp6")

    def logs(self, name: str, minutes: int) -> str:
        return _docker("logs", "--no-color", "--since", f"{minutes}m", name, timeout=60)


def observe(settings: dict, system=None, now=None) -> dict:
    system = system or System(settings)
    now = now or datetime.now(BEIJING)
    setting = settings.get("protocol", "auto")
    proc, listed = None, False
    if setting != "napcat":
        try:
            proc = find_openqq(system.node_processes())
            listed = True
        except Exception:
            pass
    napcat_running = False
    if setting not in PROTOCOLS and proc is None:
        try:
            napcat_running = system.service_running("napcat")
        except Exception:
            pass
    # 列进程失败时不知道 OpenQQ 在不在：NapCat 也没在跑就按 OpenQQ 查，别直接报「都没在运行」
    protocol = choose_protocol(setting, proc is not None or (not listed and not napcat_running), napcat_running)
    if protocol == "openqq":
        return _observe_openqq(settings, system, now, proc, True if proc else (False if listed else None))
    if protocol == "napcat":
        return _observe_napcat(settings, system, now)
    return {"protocol": "none"}


def _observe_openqq(settings, system, now, proc, running) -> dict:
    hours = settings["silence_hours"]
    obs = {"protocol": "openqq", "openqq_running": running, "phase": None, "qq_alive": None,
           "astrbot_running": None, "ws_connections": None, "received": None,
           "silence_applicable": False, "silence_hours": hours}
    if running is False:
        return obs
    try:
        status = system.openqq_status()
    except Exception:
        status = {}
    if isinstance(status.get("phase"), str):
        obs["phase"] = status["phase"]
    pid = status.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        try:
            obs["qq_alive"] = system.pid_alive(pid)
        except Exception:
            pass
    try:
        obs["astrbot_running"] = system.service_running("astrbot")
    except Exception:
        return obs
    if not obs["astrbot_running"]:
        return obs
    try:
        obs["ws_connections"] = count_established(system.proc_net())
    except Exception:
        pass
    try:
        starts = [parse_docker_time((proc or {}).get("started") or ""), system.started_at("astrbot")]
        started = max(starts) if all(starts) else None
        obs["silence_applicable"] = silence_applicable(now, started, hours, settings["active"])
        if obs["silence_applicable"]:
            obs["received"] = count_astrbot_messages(system.logs("astrbot", int(hours * 60)))
    except Exception:
        obs["silence_applicable"] = False
        obs["received"] = None
    return obs


def _observe_napcat(settings, system, now) -> dict:
    obs = {"protocol": "napcat", "webui": False, "is_login": None, "is_offline": None, "login_error": "",
           "online": None, "rkey_ok": None, "received": None,
           "silence_applicable": False, "silence_hours": settings["silence_hours"]}
    ui = WebUI(settings["port"])
    try:
        ui.login()
        status = ui.login_status()
    except Exception:
        return obs
    obs["webui"] = True
    obs["is_login"] = status.get("isLogin")
    obs["is_offline"] = status.get("isOffline")
    obs["login_error"] = status.get("loginError") or ""
    if obs["is_login"]:
        try:
            r = ui.call("get_status", timeout=10)
            if r and r.get("retcode") == 0:
                obs["online"] = bool((r.get("data") or {}).get("online"))
        except Exception:
            pass
        try:
            r = ui.call("nc_get_rkey", timeout=20)
            obs["rkey_ok"] = bool(r and r.get("retcode") == 0 and r.get("data"))
        except Exception:
            obs["rkey_ok"] = False
    try:
        hours = settings["silence_hours"]
        obs["silence_applicable"] = silence_applicable(now, system.started_at("napcat"), hours, settings["active"])
        if obs["silence_applicable"]:
            obs["received"] = count_received(system.logs("napcat", int(hours * 60)))
    except Exception:
        obs["silence_applicable"] = False
    return obs


def _xml(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def notify(title: str, body: str) -> bool:
    """Windows 通知；toast 不可用时退回托盘气泡。"""
    title, body = _xml(mask(title)), _xml(mask(body))
    script = f"""
$ErrorActionPreference = 'Stop'
try {{
  [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
  $x = New-Object Windows.Data.Xml.Dom.XmlDocument
  $x.LoadXml('<toast scenario="reminder"><visual><binding template="ToastGeneric"><text>{title}</text><text>{body}</text></binding></visual><actions><action content="知道了" arguments="dismiss" activationType="system"/></actions></toast>')
  $app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
}} catch {{
  Add-Type -AssemblyName System.Windows.Forms
  $n = New-Object System.Windows.Forms.NotifyIcon
  $n.Icon = [System.Drawing.SystemIcons]::Warning
  $n.Visible = $true
  $n.ShowBalloonTip(15000, [System.Net.WebUtility]::HtmlDecode('{title}'), [System.Net.WebUtility]::HtmlDecode('{body}'), 'Warning')
  Start-Sleep 16
  $n.Dispose()
}}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=60, creationflags=NO_WINDOW,
        )
        return True
    except Exception as e:
        log(f"通知失败：{e}")
        return False


def log(line: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "watchdog.log")
    try:
        if os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{stamp} {mask(line)}\n")


def _load_state():
    try:
        with open(os.path.join(LOG_DIR, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_state(state) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def check_once(settings: dict, state):
    last = {"protocol": "napcat"}

    def observe_once():
        obs = observe(settings)
        last["protocol"] = obs.get("protocol", "napcat")
        return obs

    level, reasons = confirm(observe_once, time.sleep)
    state, msg = decide(state, level, reasons, time.time(), protocol=last["protocol"])
    _save_state(state)
    log(f"[{last['protocol']}] {level} {'；'.join(reasons)}".strip())
    if msg:
        log(f"通知：{msg[0]} | {msg[1]}")
        notify(*msg)
    return level, reasons


def main(argv) -> int:
    if "--test-notify" in argv:
        notify("大肥鱼监控测试", "看到这条说明掉线通知能正常弹出。")
        return 0
    settings = load_settings()
    if "--once" in argv:
        level, reasons = check_once(settings, _load_state())
        print(level, "；".join(reasons))
        return 0 if level == "ok" else 1
    os.makedirs(LOG_DIR, exist_ok=True)
    pid_path = os.path.join(LOG_DIR, "watchdog.pid")
    with open(pid_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    log(f"监控启动（协议 {settings['protocol']}）：每 {settings['interval'] / 60:g} 分钟查一次，静默阈值 {settings['silence_hours']:g} 小时，活跃时段 {settings['active'][0]}-{settings['active'][1]} 点")
    state = _load_state()
    try:
        while True:
            try:
                check_once(settings, state)
                state = _load_state()
            except Exception as e:
                log(f"检查出错：{type(e).__name__}: {e}")
            time.sleep(settings["interval"])
    finally:
        try:
            os.remove(pid_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
