# -*- coding: utf-8 -*-
"""记忆系统挂钩点（规格 §16.1）的回归测试，不依赖 AstrBot。

- dafeiyu_hooks.py：注册表本身（send_origin、入站观察、出站回调、取消、吞异常）；
- aiocqhttp_message_event.py（P-M1）：发送成功后才发出站事件，带上发送来源；
- waking_check_stage.py（入站观察）：分流判定之前、对每条非自身消息调用一次。

两个核心文件的测试用桩模块顶替 AstrBot 与 aiocqhttp，只跑补丁关心的那段逻辑。

跑法：python -m unittest tests.test_dafeiyu_hooks
"""
import asyncio
import importlib.util
import os
import re
import sys
import types
import unittest
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES = os.path.join(ROOT, "deploy", "patches")


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass 需要能按模块名找到自己
    spec.loader.exec_module(module)
    return module


def load_hooks():
    return load_file("dafeiyu_hooks_under_test", os.path.join(PATCHES, "dafeiyu_hooks.py"))


def run(coro):
    return asyncio.run(coro)


async def settle():
    """让 emit_outbound 排出去的后台任务跑完。"""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------- 注册表


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.hooks = load_hooks()

    def test_send_origin_defaults_to_self(self):
        self.assertEqual(self.hooks.send_origin.get(), "self")

    def test_inbound_observer_is_called_synchronously_with_the_event(self):
        seen = []
        self.hooks.register_inbound_observer(seen.append)
        event = object()
        self.hooks.emit_inbound(event)
        self.assertEqual(seen, [event])

    def test_unregister_stops_further_calls(self):
        seen = []
        undo = self.hooks.register_inbound_observer(seen.append)
        undo()
        undo()  # 重复取消不报错
        self.hooks.emit_inbound(object())
        self.assertEqual(seen, [])

    def test_inbound_observer_errors_are_swallowed_and_others_still_run(self):
        seen = []

        def broken(_event):
            raise RuntimeError("boom")

        self.hooks.register_inbound_observer(broken)
        self.hooks.register_inbound_observer(seen.append)
        with self.assertLogs(level="WARNING"):
            self.hooks.emit_inbound("e")
        self.assertEqual(seen, ["e"])

    def test_outbound_callback_runs_in_background_with_origin(self):
        got = []

        async def on_out(info):
            got.append(info)

        self.hooks.register_outbound(on_out)

        async def go():
            token = self.hooks.send_origin.set("command")
            try:
                self.hooks.emit_outbound(is_group=True, session_id="1234567890", message_chain="chain")
            finally:
                self.hooks.send_origin.reset(token)
            self.assertEqual(got, [], "回调必须是后台任务，不能拖慢发送")
            await settle()

        run(go())
        self.assertEqual(len(got), 1)
        info = got[0]
        self.assertTrue(info.is_group)
        self.assertEqual(info.session_id, "1234567890")
        self.assertEqual(info.message_chain, "chain")
        self.assertEqual(info.origin, "command")

    def test_outbound_accepts_sync_callbacks(self):
        got = []
        self.hooks.register_outbound(got.append)

        async def go():
            self.hooks.emit_outbound(is_group=False, session_id="1234567890", message_chain="c")
            await settle()

        run(go())
        self.assertEqual([i.origin for i in got], ["self"])

    def test_outbound_errors_are_swallowed(self):
        got = []

        async def broken(_info):
            raise RuntimeError("boom")

        self.hooks.register_outbound(broken)
        self.hooks.register_outbound(got.append)

        async def go():
            self.hooks.emit_outbound(is_group=False, session_id="1", message_chain="c")
            await settle()

        with self.assertLogs(level="WARNING"):
            run(go())
        self.assertEqual(len(got), 1)

    def test_outbound_without_running_loop_does_not_raise(self):
        async def on_out(_info):
            raise AssertionError("没有事件循环时不应调用")

        self.hooks.register_outbound(on_out)
        self.hooks.emit_outbound(is_group=False, session_id="1", message_chain="c")

    def test_no_callbacks_is_a_noop(self):
        self.hooks.emit_inbound(object())
        self.hooks.emit_outbound(is_group=False, session_id="1", message_chain="c")


# ---------------------------------------------------------------- 桩模块


@contextmanager
def stub_modules(mapping):
    saved = {name: sys.modules.get(name) for name in mapping}
    sys.modules.update(mapping)
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


class _Seg:
    def __init__(self, text=""):
        self.text = text

    async def to_dict(self):
        return {"type": type(self).__name__.lower(), "data": {"text": self.text}}

    def toDict(self):
        return {"type": type(self).__name__.lower(), "data": {"text": self.text}}


def make_segment_classes():
    names = ["At", "BaseMessageComponent", "File", "Image", "Node", "Nodes", "Plain", "Record", "Video"]
    classes = {n: type(n, (_Seg,), {}) for n in names}
    return classes


class FakeChain:
    def __init__(self, chain):
        self.chain = chain


class FakeBot:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def send_group_msg(self, **kw):
        if self.fail:
            raise RuntimeError("send failed")
        self.calls.append(("group", kw))

    async def send_private_msg(self, **kw):
        if self.fail:
            raise RuntimeError("send failed")
        self.calls.append(("private", kw))


class AiocqhttpPatchTests(unittest.TestCase):
    """P-M1：所有 QQ 出站汇于 AiocqhttpMessageEvent.send_message。"""

    def load(self, hooks):
        seg = make_segment_classes()
        self.seg = seg
        stubs = {
            "aiocqhttp": module("aiocqhttp", CQHttp=object, Event=dict),
            "astrbot": module("astrbot"),
            "astrbot.api": module("astrbot.api", logger=__import__("logging").getLogger("astrbot")),
            "astrbot.api.event": module("astrbot.api.event", AstrMessageEvent=object, MessageChain=FakeChain),
            "astrbot.api.message_components": module("astrbot.api.message_components", **seg),
            "astrbot.api.platform": module("astrbot.api.platform", Group=object, MessageMember=object),
            "astrbot.core": module("astrbot.core", dafeiyu_hooks=hooks),
            "astrbot.core.dafeiyu_hooks": hooks,
        }
        with stub_modules(stubs):
            return load_file("aiocqhttp_patch_under_test", os.path.join(PATCHES, "aiocqhttp_message_event.py"))

    def setUp(self):
        self.hooks = load_hooks()
        self.mod = self.load(self.hooks)
        self.cls = self.mod.AiocqhttpMessageEvent
        self.got = []
        self.hooks.register_outbound(self.got.append)

    def send(self, bot, chain, **kw):
        async def go():
            await self.cls.send_message(bot, chain, **kw)
            await settle()

        run(go())

    def test_successful_send_emits_once_with_chain_and_origin(self):
        chain = FakeChain([self.seg["Plain"]("在的")])
        bot = FakeBot()
        self.send(bot, chain, is_group=True, session_id="1234567890")
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(len(self.got), 1)
        info = self.got[0]
        self.assertIs(info.message_chain, chain)
        self.assertTrue(info.is_group)
        self.assertEqual(info.session_id, "1234567890")
        self.assertEqual(info.origin, "self")

    def test_origin_set_by_caller_is_carried(self):
        chain = FakeChain([self.seg["Plain"]("记忆状态")])

        async def go():
            token = self.hooks.send_origin.set("command")
            try:
                await self.cls.send_message(FakeBot(), chain, is_group=False, session_id="1234567890")
            finally:
                self.hooks.send_origin.reset(token)
            await settle()

        run(go())
        self.assertEqual([i.origin for i in self.got], ["command"])

    def test_failed_send_does_not_emit(self):
        chain = FakeChain([self.seg["Plain"]("在的")])
        with self.assertRaises(RuntimeError):
            self.send(FakeBot(fail=True), chain, is_group=True, session_id="1234567890")
        self.assertEqual(self.got, [])

    def test_empty_chain_sends_nothing_and_does_not_emit(self):
        self.send(FakeBot(), FakeChain([self.seg["Plain"]("   ")]), is_group=True, session_id="1234567890")
        self.assertEqual(self.got, [])

    def test_patch_still_works_without_hooks_module(self):
        """dafeiyu_hooks 没挂载时（只用基础补丁）照常发送。"""
        stubs_missing = module("astrbot.core")  # 没有 dafeiyu_hooks 属性
        seg = make_segment_classes()
        stubs = {
            "aiocqhttp": module("aiocqhttp", CQHttp=object, Event=dict),
            "astrbot": module("astrbot"),
            "astrbot.api": module("astrbot.api", logger=__import__("logging").getLogger("astrbot")),
            "astrbot.api.event": module("astrbot.api.event", AstrMessageEvent=object, MessageChain=FakeChain),
            "astrbot.api.message_components": module("astrbot.api.message_components", **seg),
            "astrbot.api.platform": module("astrbot.api.platform", Group=object, MessageMember=object),
            "astrbot.core": stubs_missing,
            "astrbot.core.dafeiyu_hooks": None,
        }
        with stub_modules(stubs):
            mod = load_file("aiocqhttp_patch_nohooks", os.path.join(PATCHES, "aiocqhttp_message_event.py"))
        bot = FakeBot()

        async def go():
            await mod.AiocqhttpMessageEvent.send_message(bot, FakeChain([seg["Plain"]("在的")]),
                                                          is_group=False, session_id="1234567890")

        run(go())
        self.assertEqual(len(bot.calls), 1)


# ---------------------------------------------------------------- 入站观察


class FakeEvent:
    def __init__(self, sender="1234567890", self_id="1111111111", text="大家好"):
        self.sender = sender
        self.self_id = self_id
        self.message_str = text
        self.stopped = False
        self.extras = {}
        self.message_obj = types.SimpleNamespace(type="GroupMessage", raw_message=None)
        self.session_id = "s"
        self.role = "member"
        self.is_wake = False
        self.is_at_or_wake_command = False
        self.plugins_name = None

    def get_self_id(self):
        return self.self_id

    def get_sender_id(self):
        return self.sender

    def stop_event(self):
        self.stopped = True

    def set_extra(self, k, v):
        self.extras[k] = v

    def get_extra(self, k=None, default=None):
        if k is None:
            return self.extras
        return self.extras.get(k, default)

    @property
    def _extras(self):
        return self.extras

    def get_platform_name(self):
        return "aiocqhttp"

    def get_messages(self):
        return [object()]

    def is_private_chat(self):
        return False

    def get_group_id(self):
        return "2222222222"


class WakingPatchTests(unittest.TestCase):
    """入站观察：在唤醒/分流判定之前、对每条非自身消息调用。"""

    def load(self, hooks):
        class Registry:
            @staticmethod
            def get_handlers_by_event_type(*a, **kw):
                return []

        class SessionPluginManager:
            @staticmethod
            async def filter_handlers_by_session(event, handlers):
                return handlers

        class Recorder:
            def __init__(self, *a):
                pass

            def schedule(self, event):
                pass

        def register_stage(cls):
            return cls

        pkg = "astrbot.core.pipeline"
        stubs = {
            "astrbot": module("astrbot", logger=__import__("logging").getLogger("astrbot")),
            "astrbot.core": module("astrbot.core", dafeiyu_hooks=hooks),
            "astrbot.core.dafeiyu_hooks": hooks,
            "astrbot.core.message": module("astrbot.core.message"),
            "astrbot.core.message.components": module("astrbot.core.message.components",
                                                      At=type("At", (), {}), AtAll=type("AtAll", (), {}),
                                                      Reply=type("Reply", (), {})),
            "astrbot.core.message.message_event_result": module("astrbot.core.message.message_event_result",
                                                                MessageChain=FakeChain, MessageEventResult=object),
            "astrbot.core.platform": module("astrbot.core.platform"),
            "astrbot.core.platform.astr_message_event": module("astrbot.core.platform.astr_message_event",
                                                               AstrMessageEvent=object),
            "astrbot.core.platform.message_type": module("astrbot.core.platform.message_type",
                                                         MessageType=types.SimpleNamespace(GROUP_MESSAGE="GroupMessage")),
            "astrbot.core.star": module("astrbot.core.star"),
            "astrbot.core.star.filter": module("astrbot.core.star.filter"),
            "astrbot.core.star.filter.command_group": module("astrbot.core.star.filter.command_group",
                                                             CommandGroupFilter=type("CGF", (), {})),
            "astrbot.core.star.filter.permission": module("astrbot.core.star.filter.permission",
                                                          PermissionTypeFilter=type("PTF", (), {})),
            "astrbot.core.star.session_plugin_manager": module("astrbot.core.star.session_plugin_manager",
                                                               SessionPluginManager=SessionPluginManager),
            "astrbot.core.star.star": module("astrbot.core.star.star", star_map={}),
            "astrbot.core.star.star_handler": module("astrbot.core.star.star_handler",
                                                     EventType=types.SimpleNamespace(AdapterMessageEvent="a"),
                                                     star_handlers_registry=Registry()),
            pkg: module(pkg, __path__=[]),
            pkg + ".context": module(pkg + ".context", PipelineContext=object),
            pkg + ".stage": module(pkg + ".stage", Stage=object, register_stage=register_stage),
            pkg + ".waking_check": module(pkg + ".waking_check", __path__=[]),
            pkg + ".waking_check.umo_auto_name": module(pkg + ".waking_check.umo_auto_name",
                                                        UmoAutoNameRecorder=Recorder),
        }
        path = os.path.join(PATCHES, "waking_check_stage.py")
        with stub_modules(stubs):
            spec = importlib.util.spec_from_file_location(pkg + ".waking_check.stage", path)
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = pkg + ".waking_check"
            spec.loader.exec_module(mod)
        return mod

    def make_stage(self, ignore_self=False):
        mod = self.load(self.hooks)
        stage = mod.WakingCheckStage()
        config = {
            "platform_settings": {"ignore_bot_self_message": ignore_self},
            "admins_id": [],
            "wake_prefix": [],
        }
        stage.ctx = types.SimpleNamespace(astrbot_config=config, db_helper=None, astrbot_config_id="x")
        run(stage.initialize(stage.ctx))
        return stage

    def setUp(self):
        self.hooks = load_hooks()
        self.seen = []
        self.hooks.register_inbound_observer(lambda e: self.seen.append((e, e.is_wake, e.stopped)))

    def test_observer_sees_messages_that_do_not_wake_her(self):
        stage = self.make_stage()
        event = FakeEvent()
        run(stage.process(event))
        self.assertTrue(event.stopped, "普通群聊不唤醒")
        self.assertEqual(len(self.seen), 1)
        seen_event, was_wake, was_stopped = self.seen[0]
        self.assertIs(seen_event, event)
        self.assertFalse(was_wake, "观察必须发生在唤醒判定之前")
        self.assertFalse(was_stopped)

    def test_own_messages_are_not_observed_even_if_not_ignored(self):
        stage = self.make_stage(ignore_self=False)
        run(stage.process(FakeEvent(sender="1111111111", self_id="1111111111")))
        self.assertEqual(self.seen, [])

    def test_own_messages_ignored_by_config_are_not_observed(self):
        stage = self.make_stage(ignore_self=True)
        event = FakeEvent(sender="1111111111", self_id="1111111111")
        run(stage.process(event))
        self.assertTrue(event.stopped)
        self.assertEqual(self.seen, [])


# ---------------------------------------------------------------- 挂载


class WiringTests(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_windows_compose_mounts_hooks_and_waking_patch(self):
        compose = self.read("deploy-windows", "compose.yaml")
        self.assertIn("../deploy/patches/dafeiyu_hooks.py:/AstrBot/astrbot/core/dafeiyu_hooks.py:ro", compose)
        self.assertIn(
            "../deploy/patches/waking_check_stage.py:/AstrBot/astrbot/core/pipeline/waking_check/stage.py:ro",
            compose,
        )

    def test_windows_compose_puts_memory_files_on_host(self):
        """记忆文件（Markdown + git）放宿主机方便查看和修改；SQLite 库留在数据卷（不挂整个插件数据目录）。"""
        compose = self.read("deploy-windows", "compose.yaml")
        self.assertIn(
            "${MEMORY_DIR:-./memory}:/AstrBot/data/plugin_data/astrbot_plugin_memory_core/memory", compose)
        self.assertNotRegex(compose, r"(?m):/AstrBot/data/plugin_data/astrbot_plugin_memory_core(:ro)?\s*$")
        self.assertIn("deploy-windows/memory/", self.read(".gitignore").splitlines())
        self.assertIn("MEMORY_DIR=", self.read("deploy-windows", ".env.example"))

    def test_hooks_module_has_no_astrbot_dependency(self):
        src = self.read("deploy", "patches", "dafeiyu_hooks.py")
        self.assertIsNone(re.search(r"^\s*(from|import)\s+astrbot", src, re.M))


if __name__ == "__main__":
    unittest.main()
