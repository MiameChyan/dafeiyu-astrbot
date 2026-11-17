# -*- coding: utf-8 -*-
"""限流补丁 rate_limit_stage.py 的回归测试，不依赖 AstrBot。

起因（2026-11-17）：OpenQQ 把“对方正在输入”作为 notice 事件推进流水线（chat_rhythm
靠它判断对方打完没有）。原版限流把这些通知也算进“每分钟 N 条”，私聊里一边打字一边发，
名额很快被通知占满，真正的消息跟着 stall 排队，回复延迟 6～76 秒。

补丁只改一件事：OneBot 的 notice / request / meta_event 不计数、也不排队；
真正的消息（post_type=message）和拿不到 post_type 的事件照旧限流。

跑法：python -m unittest tests.test_rate_limit_stage
"""
import asyncio
import enum
import importlib.util
import os
import sys
import types
import unittest
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH = os.path.join(ROOT, "deploy", "patches", "rate_limit_stage.py")


class RateLimitStrategy(enum.Enum):
    STALL = "stall"
    DISCARD = "discard"


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **k):
        self.lines.append(msg)

    debug = warning = info


@contextmanager
def stub_modules(stubs):
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


def load_stage(logger):
    pkg = "astrbot.core.pipeline"
    stubs = {
        "astrbot": module("astrbot", __path__=[]),
        "astrbot.core": module("astrbot.core", __path__=[], logger=logger),
        "astrbot.core.config": module("astrbot.core.config", __path__=[]),
        "astrbot.core.config.astrbot_config": module(
            "astrbot.core.config.astrbot_config", RateLimitStrategy=RateLimitStrategy),
        "astrbot.core.platform": module("astrbot.core.platform", __path__=[]),
        "astrbot.core.platform.astr_message_event": module(
            "astrbot.core.platform.astr_message_event", AstrMessageEvent=object),
        pkg: module(pkg, __path__=[]),
        pkg + ".context": module(pkg + ".context", PipelineContext=object),
        pkg + ".stage": module(pkg + ".stage", Stage=object, register_stage=lambda c: c),
        pkg + ".rate_limit_check": module(pkg + ".rate_limit_check", __path__=[]),
    }
    with stub_modules(stubs):
        spec = importlib.util.spec_from_file_location(pkg + ".rate_limit_check.stage", PATCH)
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = pkg + ".rate_limit_check"
        spec.loader.exec_module(mod)
    return mod


class FakeEvent:
    def __init__(self, raw, umo="default:FriendMessage:42"):
        self.unified_msg_origin = umo
        self.message_obj = types.SimpleNamespace(raw_message=raw)
        self.stopped = False

    def stop_event(self):
        self.stopped = True


def message(text="hi"):
    return {"post_type": "message", "message_type": "private", "raw_message": text}


def typing_notice():
    return {"post_type": "notice", "notice_type": "notify", "sub_type": "input_status",
            "status_text": "对方正在输入..."}


class RateLimitPatchTests(unittest.TestCase):
    def make(self, count=2, seconds=60, strategy="stall"):
        self.logger = FakeLogger()
        mod = load_stage(self.logger)
        stage = mod.RateLimitStage()
        cfg = {"platform_settings": {"rate_limit": {"count": count, "time": seconds,
                                                      "strategy": strategy}}}
        asyncio.run(stage.initialize(types.SimpleNamespace(astrbot_config=cfg)))
        self.sleeps = []

        async def fake_sleep(sec):
            self.sleeps.append(sec)
            raise _Stalled()

        mod.asyncio = types.SimpleNamespace(sleep=fake_sleep, Lock=asyncio.Lock)
        return stage

    def process(self, stage, event):
        try:
            asyncio.run(stage.process(event))
        except _Stalled:
            return "stalled"
        return "stopped" if event.stopped else "passed"

    # -- 原问题：通知占满名额

    def test_typing_notices_do_not_use_up_the_quota(self):
        stage = self.make(count=2)
        for _ in range(5):
            self.assertEqual(self.process(stage, FakeEvent(typing_notice())), "passed")
        self.assertEqual(self.process(stage, FakeEvent(message("a"))), "passed")
        self.assertEqual(self.process(stage, FakeEvent(message("b"))), "passed")
        self.assertEqual(self.sleeps, [])

    def test_notices_pass_even_when_messages_are_over_the_limit(self):
        stage = self.make(count=1)
        self.assertEqual(self.process(stage, FakeEvent(message())), "passed")
        self.assertEqual(self.process(stage, FakeEvent(typing_notice())), "passed")
        self.assertEqual(self.sleeps, [])

    def test_request_and_meta_events_are_not_counted_either(self):
        stage = self.make(count=1)
        for raw in ({"post_type": "request", "request_type": "friend"},
                    {"post_type": "meta_event", "meta_event_type": "heartbeat"}):
            self.assertEqual(self.process(stage, FakeEvent(raw)), "passed")
        self.assertEqual(self.process(stage, FakeEvent(message())), "passed")

    # -- 真正的消息照旧限流

    def test_messages_over_the_limit_still_stall(self):
        stage = self.make(count=2)
        self.process(stage, FakeEvent(message("1")))
        self.process(stage, FakeEvent(message("2")))
        self.assertEqual(self.process(stage, FakeEvent(message("3"))), "stalled")
        self.assertEqual(len(self.sleeps), 1)

    def test_discard_strategy_still_drops_excess_messages(self):
        stage = self.make(count=1, strategy="discard")
        self.process(stage, FakeEvent(message("1")))
        self.assertEqual(self.process(stage, FakeEvent(message("2"))), "stopped")

    def test_limits_are_per_session(self):
        stage = self.make(count=1)
        self.process(stage, FakeEvent(message(), umo="a"))
        self.assertEqual(self.process(stage, FakeEvent(message(), umo="b")), "passed")

    # -- 拿不到 post_type 时保守按消息处理（其他平台、旧适配器）

    def test_events_without_a_dict_raw_message_are_still_counted(self):
        stage = self.make(count=1)
        self.process(stage, FakeEvent(object()))
        self.assertEqual(self.process(stage, FakeEvent(None)), "stalled")

    def test_dict_without_post_type_is_still_counted(self):
        stage = self.make(count=1)
        self.process(stage, FakeEvent({"foo": 1}))
        self.assertEqual(self.process(stage, FakeEvent({})), "stalled")

    def test_missing_message_obj_does_not_crash_and_is_counted(self):
        stage = self.make(count=1)
        ev = FakeEvent(message())
        ev.message_obj = None
        self.assertEqual(self.process(stage, ev), "passed")
        self.assertEqual(self.process(stage, FakeEvent(message())), "stalled")

    def test_disabled_limit_passes_everything(self):
        stage = self.make(count=0)
        for _ in range(3):
            self.assertEqual(self.process(stage, FakeEvent(message())), "passed")


class _Stalled(Exception):
    """替代真实 sleep：一旦要 stall 就抛出，测试据此判断“被限流排队”。"""


class WiringTests(unittest.TestCase):
    def test_windows_compose_mounts_rate_limit_patch(self):
        with open(os.path.join(ROOT, "deploy-windows", "compose.yaml"), encoding="utf-8") as f:
            compose = f.read()
        self.assertIn(
            "../deploy/patches/rate_limit_stage.py:/AstrBot/astrbot/core/pipeline/rate_limit_check/stage.py:ro",
            compose,
        )


if __name__ == "__main__":
    unittest.main()
