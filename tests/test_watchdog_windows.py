# -*- coding: utf-8 -*-
"""deploy-windows/watchdog.py 的纯逻辑回归（不连 NapCat、不弹窗）。

跑法：python -m unittest tests.test_watchdog_windows
"""
import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "deploy-windows")


def load():
    spec = importlib.util.spec_from_file_location("dafeiyu_watchdog", os.path.join(KIT, "watchdog.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wd = load()
CST = timezone(timedelta(hours=8))


def at(h, m=0, day=15):
    return datetime(2026, 11, day, h, m, tzinfo=CST)


def healthy(**over):
    obs = {
        "webui": True,
        "is_login": True,
        "is_offline": False,
        "login_error": "",
        "online": True,
        "rkey_ok": True,
        "received": 12,
        "silence_applicable": True,
    }
    obs.update(over)
    return obs


class HelperTests(unittest.TestCase):
    def test_mask_hides_long_numbers(self):
        self.assertEqual(wd.mask("QQ 1234567890 已失效"), "QQ <num> 已失效")
        self.assertEqual(wd.mask("端口 6099"), "端口 6099")
        self.assertEqual(wd.mask(None), "")

    def test_count_received_only_counts_incoming(self):
        log = "\n".join([
            "napcat-1  | 11-15 20:16:35 [info] 伽拉忒鱼 | 接收 <- 私聊 (123456) [图片]",
            "napcat-1  | 11-15 20:16:38 [info] 伽拉忒鱼 | 发送 -> 私聊 (123456) 明白……",
            "napcat-1  | 11-15 20:08:40 [info] 伽拉忒鱼 | 接收 <- 群聊 [杨镇(1)] [仿舫(2)] [图片]",
        ])
        self.assertEqual(wd.count_received(log), 2)
        self.assertEqual(wd.count_received(""), 0)

    def test_parse_env_ignores_comments_and_blanks(self):
        env = wd.parse_env("# x\nNAPCAT_PORT=6100\n\nWATCHDOG_SILENCE_HOURS = 4 # 注释\nEMPTY=\n")
        self.assertEqual(env["NAPCAT_PORT"], "6100")
        self.assertEqual(env["WATCHDOG_SILENCE_HOURS"], "4")
        self.assertEqual(env["EMPTY"], "")

    def test_parse_docker_time_handles_nanoseconds(self):
        t = wd.parse_docker_time("2026-11-15T11:44:30.123456789Z")
        self.assertEqual(t, datetime(2026, 11, 15, 11, 44, 30, 123456, tzinfo=timezone.utc))
        self.assertIsNone(wd.parse_docker_time(""))
        self.assertIsNone(wd.parse_docker_time("0001-01-01T00:00:00Z garbage"))


class SilenceWindowTests(unittest.TestCase):
    def test_applies_in_the_afternoon_after_long_uptime(self):
        self.assertTrue(wd.silence_applicable(at(15), at(8), hours=3))

    def test_not_when_window_reaches_into_the_night(self):
        self.assertFalse(wd.silence_applicable(at(11), at(1), hours=3))  # 窗口从 8 点开始
        self.assertFalse(wd.silence_applicable(at(3), at(1, day=14), hours=3))
        self.assertTrue(wd.silence_applicable(at(12), at(1), hours=3))  # 窗口正好从 9 点开始

    def test_not_right_after_napcat_restart(self):
        self.assertFalse(wd.silence_applicable(at(15), at(13), hours=3))
        self.assertFalse(wd.silence_applicable(at(15), None, hours=3))

    def test_custom_active_hours(self):
        self.assertFalse(wd.silence_applicable(at(23), at(8), hours=3, active=(9, 22)))
        self.assertTrue(wd.silence_applicable(at(21), at(8), hours=3, active=(9, 22)))


class ClassifyTests(unittest.TestCase):
    def test_all_good(self):
        self.assertEqual(wd.classify(healthy())[0], "ok")

    def test_each_hard_failure_means_down(self):
        for over in (
            {"webui": False},
            {"is_login": False},
            {"is_offline": True},
            {"online": False},
            {"rkey_ok": False},
        ):
            level, reasons = wd.classify(healthy(**over))
            self.assertEqual(level, "down", over)
            self.assertTrue(reasons, over)

    def test_unknown_values_are_not_failures(self):
        self.assertEqual(wd.classify(healthy(online=None, rkey_ok=None, received=None))[0], "ok")

    def test_webui_down_skips_other_checks(self):
        level, reasons = wd.classify(healthy(webui=False, is_login=None, online=None, rkey_ok=None))
        self.assertEqual(level, "down")
        self.assertEqual(len(reasons), 1)

    def test_silence_is_only_suspect(self):
        level, reasons = wd.classify(healthy(received=0))
        self.assertEqual(level, "suspect")
        self.assertIn("3", "".join(reasons) + "3")

    def test_silence_ignored_outside_window(self):
        self.assertEqual(wd.classify(healthy(received=0, silence_applicable=False))[0], "ok")

    def test_down_wins_over_suspect(self):
        self.assertEqual(wd.classify(healthy(received=0, rkey_ok=False))[0], "down")

    def test_login_error_is_masked_in_reasons(self):
        _, reasons = wd.classify(healthy(is_login=False, login_error="QQ 1234567890 用户身份已失效"))
        text = " ".join(reasons)
        self.assertIn("用户身份已失效", text)
        self.assertNotIn("1234567890", text)


class ConfirmTests(unittest.TestCase):
    def test_blip_is_rechecked_and_cleared(self):
        seq = iter([healthy(rkey_ok=False), healthy()])
        slept = []
        level, _ = wd.confirm(lambda: next(seq), slept.append, recheck_seconds=60)
        self.assertEqual(level, "ok")
        self.assertEqual(slept, [60])

    def test_persistent_failure_is_down(self):
        seq = iter([healthy(online=False), healthy(online=False)])
        level, _ = wd.confirm(lambda: next(seq), lambda s: None)
        self.assertEqual(level, "down")

    def test_ok_and_suspect_are_not_rechecked(self):
        calls = []

        def observe():
            calls.append(1)
            return healthy(received=0)

        level, _ = wd.confirm(observe, lambda s: self.fail("不该等待"))
        self.assertEqual(level, "suspect")
        self.assertEqual(len(calls), 1)


class DecideTests(unittest.TestCase):
    def test_first_run_ok_is_silent(self):
        state, msg = wd.decide(None, "ok", [], now=1000)
        self.assertIsNone(msg)
        self.assertEqual(state["level"], "ok")

    def test_first_run_down_alerts(self):
        state, msg = wd.decide(None, "down", ["NapCat 认为自己离线"], now=1000)
        self.assertIsNotNone(msg)
        self.assertIn("掉线", msg[0])
        self.assertEqual(state["last_notified"], 1000)

    def test_ok_to_down_alerts_once_then_reminds_every_2h(self):
        state, msg = wd.decide({"level": "ok", "since": 0, "last_notified": 0}, "down", ["x"], now=1000)
        self.assertIsNotNone(msg)
        state, msg = wd.decide(state, "down", ["x"], now=1000 + 600)
        self.assertIsNone(msg)
        state, msg = wd.decide(state, "down", ["x"], now=1000 + 7200)
        self.assertIsNotNone(msg)
        self.assertIn("仍", msg[0])
        self.assertEqual(state["since"], 1000)

    def test_recovery_notifies(self):
        prev = {"level": "down", "since": 1000, "last_notified": 1000}
        state, msg = wd.decide(prev, "ok", [], now=5000)
        self.assertIsNotNone(msg)
        self.assertIn("恢复", msg[0])
        self.assertEqual(state["level"], "ok")

    def test_suspect_alert_and_escalation(self):
        state, msg = wd.decide({"level": "ok", "since": 0, "last_notified": 0}, "suspect", ["3 小时没收到消息"], now=1000)
        self.assertIn("假在线", msg[0])
        state, msg = wd.decide(state, "down", ["rkey 失败"], now=1600)
        self.assertIn("掉线", msg[0])

    def test_messages_never_contain_long_numbers(self):
        _, msg = wd.decide(None, "down", ["登录失败：QQ 1234567890"], now=1)
        self.assertNotIn("1234567890", msg[0] + msg[1])


PROC_NET = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   2: 00000000:1837 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 21449 1 0 100 0 0 10 0
   6: 020012AC:1837 010012AC:AD6E 01 00000000:00000000 00:00000000 00000000     0        0 137503 1 0 21 4 28 10 -1
   7: 020012AC:AD70 010012AC:1837 01 00000000:00000000 00:00000000 00000000     0        0 137504 1 0 21 4 28 10 -1
   8: 020012AC:1837 010012AC:AD72 06 00000000:00000000 00:00000000 00000000     0        0 0 3 0
  sl  local_address                         remote_address                        st tx_queue rx_queue
   0: 00000000000000000000000000000000:1837 00000000000000000000000000000000:0000 0A 00000000:00000000
   1: 0000000000000000FFFF0000020012AC:1837 0000000000000000FFFF0000010012AC:AD80 01 00000000:00000000
"""

ASTRBOT_LOG = "\n".join([
    "astrbot-1  | [18:14:45.862] [Core] [INFO] [core.event_bus:79]: [default] [default(aiocqhttp)] 甲/1234567890: [图片]",
    "astrbot-1  | [18:14:46.001] [Core] [INFO] [respond.stage:183]: 发送消息",
    "astrbot-1  | [18:14:51.661] [Core] [INFO] [core.event_bus:79]: [default] [default(aiocqhttp)] 乙/1234567891: 诶",
])


def healthy_openqq(**over):
    obs = {
        "protocol": "openqq",
        "openqq_running": True,
        "phase": "session_ready",
        "qq_alive": True,
        "astrbot_running": True,
        "ws_connections": 1,
        "received": 5,
        "silence_applicable": True,
    }
    obs.update(over)
    return obs


class ProtocolChoiceTests(unittest.TestCase):
    def test_explicit_setting_wins(self):
        self.assertEqual(wd.choose_protocol("napcat", openqq_running=True, napcat_running=False), "napcat")
        self.assertEqual(wd.choose_protocol("openqq", openqq_running=False, napcat_running=True), "openqq")

    def test_auto_prefers_openqq_then_napcat(self):
        self.assertEqual(wd.choose_protocol("auto", openqq_running=True, napcat_running=True), "openqq")
        self.assertEqual(wd.choose_protocol("auto", openqq_running=False, napcat_running=True), "napcat")
        self.assertEqual(wd.choose_protocol("auto", openqq_running=False, napcat_running=False), "none")

    def test_unknown_setting_falls_back_to_auto(self):
        self.assertEqual(wd.choose_protocol("", openqq_running=True, napcat_running=False), "openqq")
        self.assertEqual(wd.choose_protocol("LLBot", openqq_running=False, napcat_running=True), "napcat")


class OpenQQHelperTests(unittest.TestCase):
    def test_count_established_only_counts_server_side_6199(self):
        # LISTEN、对端端口是 6199 的出站连接、TIME_WAIT 都不算；tcp6 的映射地址要算
        self.assertEqual(wd.count_established(PROC_NET), 2)
        self.assertEqual(wd.count_established(PROC_NET, port=6100), 0)
        self.assertEqual(wd.count_established(""), 0)
        self.assertEqual(wd.count_established(None), 0)

    def test_count_astrbot_messages(self):
        self.assertEqual(wd.count_astrbot_messages(ASTRBOT_LOG), 2)
        self.assertEqual(wd.count_astrbot_messages(""), 0)

    def test_find_openqq_matches_the_start_command_only(self):
        procs = [
            {"pid": 1, "cmd": r'"C:\Program Files\x\node.exe" "C:\x\host-daemon.mjs"', "started": "a"},
            {"pid": 2, "cmd": r'"C:\Program Files\nodejs\node.exe" src/main.mjs status', "started": "b"},
            {"pid": 3, "cmd": r'"C:\Program Files\nodejs\node.exe" src/main.mjs start', "started": "c"},
        ]
        self.assertEqual(wd.find_openqq(procs)["pid"], 3)
        quoted = [{"pid": 4, "cmd": r'node "D:\OpenQQ\src\main.mjs" start --verbose', "started": ""}]
        self.assertEqual(wd.find_openqq(quoted)["pid"], 4)
        self.assertIsNone(wd.find_openqq(procs[:2]))
        self.assertIsNone(wd.find_openqq([]))
        self.assertIsNone(wd.find_openqq([{"pid": 5, "cmd": None}]))

    def test_parse_processes_tolerates_powershell_shapes(self):
        one = wd.parse_processes('{"pid":3,"cmd":"node src/main.mjs start","started":"2026-11-16T10:09:30.1234567Z"}')
        self.assertEqual([p["pid"] for p in one], [3])
        many = wd.parse_processes('[{"pid":1,"cmd":"x"},{"pid":2,"cmd":"y"}]')
        self.assertEqual(len(many), 2)
        for junk in ("", "   ", "not json", "42", "[1, 2]"):
            self.assertEqual(wd.parse_processes(junk), [], junk)

    def test_parse_openqq_status(self):
        self.assertEqual(wd.parse_openqq_status('{"phase":"offline","pid":42}')["phase"], "offline")
        for junk in ("", "{", "[]", "null"):
            self.assertEqual(wd.parse_openqq_status(junk), {}, junk)

    def test_powershell_start_time_parses(self):
        t = wd.parse_docker_time("2026-11-16T10:09:30.1234567Z")
        self.assertEqual(t, datetime(2026, 11, 16, 10, 9, 30, 123456, tzinfo=timezone.utc))


class OpenQQClassifyTests(unittest.TestCase):
    def test_all_good(self):
        self.assertEqual(wd.classify(healthy_openqq()), ("ok", []))

    def test_each_hard_failure_means_down(self):
        for over in (
            {"phase": "offline"},
            {"phase": "hook_error"},
            {"phase": "hook_installed"},
            {"phase": "detached"},
            {"phase": "something_new"},
            {"qq_alive": False},
            {"astrbot_running": False},
            {"ws_connections": 0},
        ):
            level, reasons = wd.classify(healthy_openqq(**over))
            self.assertEqual(level, "down", over)
            self.assertTrue(reasons, over)

    def test_kicked_offline_reason_is_specific(self):
        _, reasons = wd.classify(healthy_openqq(phase="offline"))
        self.assertIn("下线", "".join(reasons))

    def test_not_running_skips_other_checks(self):
        level, reasons = wd.classify(healthy_openqq(openqq_running=False, phase=None, qq_alive=None, ws_connections=0))
        self.assertEqual(level, "down")
        self.assertEqual(len(reasons), 1)
        self.assertIn("OpenQQ", reasons[0])

    def test_astrbot_down_does_not_also_blame_the_connection(self):
        _, reasons = wd.classify(healthy_openqq(astrbot_running=False, ws_connections=0))
        self.assertEqual(len(reasons), 1)

    def test_unknown_values_are_not_failures(self):
        obs = healthy_openqq(phase=None, qq_alive=None, astrbot_running=None, ws_connections=None, received=None)
        self.assertEqual(wd.classify(obs)[0], "ok")

    def test_silence_is_only_suspect_and_down_wins(self):
        self.assertEqual(wd.classify(healthy_openqq(received=0))[0], "suspect")
        self.assertEqual(wd.classify(healthy_openqq(received=0, silence_applicable=False))[0], "ok")
        self.assertEqual(wd.classify(healthy_openqq(received=0, ws_connections=0))[0], "down")

    def test_no_protocol_running_is_down(self):
        level, reasons = wd.classify({"protocol": "none"})
        self.assertEqual(level, "down")
        self.assertIn("OpenQQ", reasons[0])
        self.assertIn("NapCat", reasons[0])

    def test_napcat_is_still_the_default(self):
        self.assertEqual(wd.classify(healthy())[0], "ok")
        self.assertIn("NapCat", wd.classify(healthy(webui=False))[1][0])


class DecideWordingTests(unittest.TestCase):
    def test_openqq_down_points_at_openqq_not_webui(self):
        _, msg = wd.decide(None, "down", ["x"], now=1, protocol="openqq")
        self.assertIn("OpenQQ", msg[1])
        self.assertNotIn("WebUI", msg[1])

    def test_napcat_wording_unchanged_by_default(self):
        _, msg = wd.decide(None, "down", ["x"], now=1)
        self.assertIn("NapCat WebUI", msg[1])

    def test_none_protocol_tells_to_start_something(self):
        _, msg = wd.decide(None, "down", ["x"], now=1, protocol="none")
        self.assertNotIn("WebUI", msg[1])


class FakeSystem:
    def __init__(self, **over):
        self.procs = [{"pid": 3, "cmd": "node src/main.mjs start", "started": "2026-11-15T00:00:00Z"}]  # 北京 8 点
        self.status = {"phase": "session_ready", "pid": 42}
        self.alive = True
        self.running = {"astrbot"}
        self.started = {"astrbot": datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)}
        self.net = PROC_NET
        self.log = ASTRBOT_LOG
        self.log_calls = []
        self.alive_calls = []
        self.__dict__.update(over)

    def node_processes(self):
        return self.procs

    def openqq_status(self):
        return self.status

    def pid_alive(self, pid):
        self.alive_calls.append(pid)
        return self.alive

    def service_running(self, name):
        return name in self.running

    def started_at(self, name):
        return self.started.get(name)

    def proc_net(self):
        return self.net

    def logs(self, name, minutes):
        self.log_calls.append((name, minutes))
        return self.log


SETTINGS = {"protocol": "auto", "port": 6099, "silence_hours": 3, "active": (9, 24)}


class ObserveOpenQQTests(unittest.TestCase):
    """真实入口：observe → classify → decide，只把系统调用换成假的。"""

    def run_chain(self, system, settings=SETTINGS, now=at(15)):
        obs = wd.observe(settings, system=system, now=now)
        level, reasons = wd.classify(obs)
        _, msg = wd.decide(None, level, reasons, now=1, protocol=obs["protocol"])
        return obs, level, reasons, msg

    def test_healthy_openqq(self):
        system = FakeSystem()
        obs, level, reasons, msg = self.run_chain(system)
        self.assertEqual(obs["protocol"], "openqq")
        self.assertEqual(obs["ws_connections"], 2)
        self.assertEqual(obs["received"], 2)
        self.assertEqual((level, msg), ("ok", None))
        self.assertEqual(system.log_calls, [("astrbot", 180)])
        self.assertEqual(system.alive_calls, [42])

    def test_kicked_offline_alerts_with_openqq_hint(self):
        obs, level, reasons, msg = self.run_chain(FakeSystem(status={"phase": "offline", "pid": 42}))
        self.assertEqual(level, "down")
        self.assertIn("OpenQQ", msg[1])

    def test_lost_ws_connection_is_down(self):
        _, level, reasons, _ = self.run_chain(FakeSystem(net=PROC_NET.replace(" 01 ", " 06 ")))
        self.assertEqual(level, "down")
        self.assertIn("6199", "".join(reasons))

    def test_qq_process_gone_is_down(self):
        _, level, reasons, _ = self.run_chain(FakeSystem(alive=False))
        self.assertEqual(level, "down")

    def test_status_without_pid_does_not_probe(self):
        system = FakeSystem(status={"phase": "session_ready"})
        _, level, _, _ = self.run_chain(system)
        self.assertEqual(level, "ok")
        self.assertEqual(system.alive_calls, [])

    def test_astrbot_stopped_skips_container_probes(self):
        system = FakeSystem(running=set())
        _, level, reasons, _ = self.run_chain(system)
        self.assertEqual(level, "down")
        self.assertIn("AstrBot", "".join(reasons))
        self.assertEqual(system.log_calls, [])

    def test_nothing_running_in_auto_mode(self):
        _, level, _, msg = self.run_chain(FakeSystem(procs=[], running={"astrbot"}))
        self.assertEqual(level, "down")
        self.assertNotIn("WebUI", msg[1])

    def test_explicit_openqq_reports_it_missing(self):
        settings = dict(SETTINGS, protocol="openqq")
        obs, level, reasons, _ = self.run_chain(FakeSystem(procs=[], running={"astrbot", "napcat"}), settings)
        self.assertEqual(obs["protocol"], "openqq")
        self.assertEqual(level, "down")
        self.assertIn("OpenQQ", reasons[0])

    def test_daytime_silence_is_suspect(self):
        _, level, _, msg = self.run_chain(FakeSystem(log="astrbot-1  | 没有消息"))
        self.assertEqual(level, "suspect")
        self.assertIn("假在线", msg[0])

    def test_recent_astrbot_restart_suppresses_silence(self):
        system = FakeSystem(log="", started={"astrbot": datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc)})  # 北京 14 点
        obs, level, _, _ = self.run_chain(system)
        self.assertFalse(obs["silence_applicable"])
        self.assertEqual(level, "ok")
        self.assertEqual(system.log_calls, [])

    def test_recent_openqq_restart_suppresses_silence(self):
        procs = [{"pid": 3, "cmd": "node src/main.mjs start", "started": "2026-11-15T06:30:00Z"}]
        obs, level, _, _ = self.run_chain(FakeSystem(procs=procs, log=""))
        self.assertFalse(obs["silence_applicable"])
        self.assertEqual(level, "ok")

    def test_night_skips_silence(self):
        obs, level, _, _ = self.run_chain(FakeSystem(log=""), now=at(10))
        self.assertFalse(obs["silence_applicable"])
        self.assertEqual(level, "ok")

    def test_probe_errors_are_unknown_not_down(self):
        class Broken(FakeSystem):
            def proc_net(self):
                raise RuntimeError("docker exec 卡住")

            def logs(self, name, minutes):
                raise RuntimeError("docker logs 卡住")

        obs, level, _, _ = self.run_chain(Broken())
        self.assertIsNone(obs["ws_connections"])
        self.assertEqual(level, "ok")


class WiringTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(KIT, name), encoding="utf-8-sig") as f:
            return f.read()

    def test_env_example_documents_protocol_settings(self):
        env = self.read(".env.example")
        for key in ("WATCHDOG_PROTOCOL", "OPENQQ_DIR"):
            self.assertIn(key, env)

    def test_readme_no_longer_says_watchdog_is_napcat_only(self):
        self.assertNotIn("目前只认 NapCat", self.read("README.md"))

    def test_start_launches_and_stop_kills_watchdog(self):
        start, stop = self.read("start.ps1"), self.read("stop.ps1")
        self.assertIn("watchdog.py", start)
        self.assertIn("-NoWatchdog", start)
        self.assertIn("watchdog.pid", stop)

    def test_env_example_documents_watchdog_settings(self):
        env = self.read(".env.example")
        for key in ("WATCHDOG_INTERVAL_MIN", "WATCHDOG_SILENCE_HOURS", "WATCHDOG_ACTIVE_HOURS"):
            self.assertIn(key, env)

    def test_runtime_files_are_gitignored(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            self.assertIn("deploy-windows/logs/", f.read())


if __name__ == "__main__":
    unittest.main()
