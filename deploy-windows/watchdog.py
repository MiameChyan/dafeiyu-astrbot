# -*- coding: utf-8 -*-
"""大肥鱼 · QQ 掉线 / 假在线监控（Windows 本机，只用标准库）。

start.ps1 会在后台用 pythonw 拉起它；stop.ps1 会停掉。也可以手动：
    python watchdog.py --once          # 查一次，打印结果
    python watchdog.py --test-notify   # 只弹一个测试通知

每隔 WATCHDOG_INTERVAL_MIN 分钟检查：
  硬信号（任一失败，60 秒后复查仍失败 → 掉线）
    - NapCat WebUI 连得上
    - WebUI 登录状态：已登录、未离线
    - OneBot get_status：online
    - OneBot nc_get_rkey：要真的去 QQ 服务器取，会话失效时会失败
  软信号（→ 疑似假在线）
    - 白天（北京时间 WATCHDOG_ACTIVE_HOURS）连续 WATCHDOG_SILENCE_HOURS 小时一条消息都没收到
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


def classify(obs: dict):
    """返回 (level, reasons)；level 为 ok / suspect / down。None 表示没查到，不算失败。"""
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
    if obs.get("silence_applicable") and obs.get("received") == 0:
        hours = obs.get("silence_hours", 3)
        return "suspect", [f"已经 {hours:g} 小时一条消息都没收到"]
    return "ok", []


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


def decide(prev, level: str, reasons, now: float, remind_seconds: float = REMIND_SECONDS):
    """根据上次状态决定是否通知。返回 (新状态, (标题, 正文) 或 None)。"""
    prev_level = prev.get("level") if prev else None
    detail = mask("；".join(reasons))
    if prev_level != level:
        state = {"level": level, "since": now, "last_notified": prev.get("last_notified", 0) if prev else 0}
        msg = None
        if level == "down":
            msg = ("大肥鱼 QQ 掉线了", f"{detail}。打开 NapCat WebUI 看看，多半要重新扫码。")
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


def napcat_started_at():
    cid = _docker("ps", "-q", "napcat").strip()
    if not cid:
        return None
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", cid],
        capture_output=True, timeout=30, creationflags=NO_WINDOW,
    )
    return parse_docker_time(out.stdout.decode("utf-8", errors="replace"))


def observe(settings: dict) -> dict:
    obs = {"webui": False, "is_login": None, "is_offline": None, "login_error": "",
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
        now = datetime.now(BEIJING)
        hours = settings["silence_hours"]
        obs["silence_applicable"] = silence_applicable(now, napcat_started_at(), hours, settings["active"])
        if obs["silence_applicable"]:
            obs["received"] = count_received(_docker("logs", "--no-color", "--since", f"{int(hours * 60)}m", "napcat", timeout=60))
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
    level, reasons = confirm(lambda: observe(settings), time.sleep)
    state, msg = decide(state, level, reasons, time.time())
    _save_state(state)
    log(f"{level} {'；'.join(reasons)}".strip())
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
    log(f"监控启动：每 {settings['interval'] / 60:g} 分钟查一次，静默阈值 {settings['silence_hours']:g} 小时，活跃时段 {settings['active'][0]}-{settings['active'][1]} 点")
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
