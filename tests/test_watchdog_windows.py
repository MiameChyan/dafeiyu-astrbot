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


class WiringTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(KIT, name), encoding="utf-8-sig") as f:
            return f.read()

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
