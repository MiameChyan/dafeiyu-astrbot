# -*- coding: utf-8 -*-
"""chat_rhythm 插件回归：私聊连发合并 + 纯表情包按概率不回。

跑法（纯标准库，不需要 AstrBot）：python -m unittest tests.test_chat_rhythm
原理：纯逻辑直接测 rhythm.py；插件入口用空壳 astrbot 模块 + 假时钟驱动。
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugin", "astrbot_plugin_chat_rhythm")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rhythm = _load("chat_rhythm_rhythm", os.path.join(PLUGIN, "rhythm.py"))
S = rhythm.Settings(quiet_seconds=4.0, max_wait_seconds=15.0, typing_ttl_seconds=7.0,
                    stop_grace_seconds=1.0, sticker_skip_probability=0.6)


def entry(text="hi", sticker=False, comps=None):
    return rhythm.Entry(text=text, components=list(comps or [text]), sticker_only=sticker)


# ---------------------------------------------------------------- 纯逻辑
class StateTests(unittest.TestCase):
    def test_first_message_leads_later_ones_follow(self):
        st = rhythm.RhythmState()
        self.assertTrue(st.add_message("u", "E1", entry("a"), now=0))
        self.assertFalse(st.add_message("u", "E2", entry("b"), now=1))
        self.assertEqual([e.text for e in st.take("u").entries], ["a", "b"])
        self.assertTrue(st.add_message("u", "E3", entry("c"), now=2), "取走后下一条重新领头")

    def test_sessions_are_independent(self):
        st = rhythm.RhythmState()
        self.assertTrue(st.add_message("u1", "E1", entry(), now=0))
        self.assertTrue(st.add_message("u2", "E2", entry(), now=0))

    def test_dead_leader_starts_new_burst(self):
        st = rhythm.RhythmState()
        st.add_message("u", "E1", entry("a"), now=0)
        lead = st.add_message("u", "E2", entry("b"), now=1, is_alive=lambda e: e != "E1")
        self.assertTrue(lead)
        self.assertEqual(st.take("u").leader, "E2")

    def test_quiet_window(self):
        st = rhythm.RhythmState()
        st.add_message("u", "E1", entry(), now=0)
        self.assertFalse(st.wait_done("u", 3.9, S))
        self.assertTrue(st.wait_done("u", 4.0, S))

    def test_new_message_restarts_quiet_window(self):
        st = rhythm.RhythmState()
        st.add_message("u", "E1", entry(), now=0)
        st.add_message("u", "E2", entry(), now=3)
        self.assertFalse(st.wait_done("u", 6.9, S))
        self.assertTrue(st.wait_done("u", 7.0, S))

    def test_typing_extends_wait(self):
        st = rhythm.RhythmState()
        st.add_message("u", "E1", entry(), now=0)
        st.on_typing("u", True, now=2)
        self.assertFalse(st.wait_done("u", 6, S), "还在打字，不该结束")
        st.on_typing("u", True, now=7)
        self.assertFalse(st.wait_done("u", 10, S))

    def test_typing_stop_needs_grace(self):
        st = rhythm.RhythmState()
        st.add_message("u", "E1", entry(), now=0)
        st.on_typing("u", True, now=2)
        st.on_typing("u", False, now=8)
        self.assertFalse(st.wait_done("u", 8.5, S))
        self.assertTrue(st.wait_done("u", 9.0, S))

    def test_stale_typing_expires(self):
        st = rhythm.RhythmState()
        st.add_message("u", "E1", entry(), now=0)
        st.on_typing("u", True, now=1)
        self.assertFalse(st.wait_done("u", 7.9, S))
        self.assertTrue(st.wait_done("u", 8.1, S), "打字状态超过 7 秒没刷新就当停了")

    def test_typing_before_message_cleared_by_stop(self):
        st = rhythm.RhythmState()
        st.on_typing("u", True, now=0)
        st.add_message("u", "E1", entry(), now=1)
        st.on_typing("u", False, now=1)
        self.assertTrue(st.wait_done("u", 5, S))

    def test_max_wait_caps_typing(self):
        st = rhythm.RhythmState()
        st.add_message("u", "E1", entry(), now=0)
        for t in range(0, 16, 3):
            st.on_typing("u", True, now=t)
        self.assertTrue(st.wait_done("u", 15, S))

    def test_wait_done_without_burst_is_true(self):
        self.assertTrue(rhythm.RhythmState().wait_done("nobody", 0, S))

    def test_discard_only_by_its_leader(self):
        st = rhythm.RhythmState()
        st.add_message("u", "E1", entry(), now=0)
        st.discard("u", "E2")
        self.assertIsNotNone(st.take("u"))
        st.add_message("u", "E3", entry(), now=0)
        st.discard("u", "E3")
        self.assertIsNone(st.take("u"))


    def test_skipped_stickers_accumulate_and_expire(self):
        st = rhythm.RhythmState()
        st.remember_skipped("u", 1, now=0)
        st.remember_skipped("u", 2, now=10)
        self.assertEqual(st.pop_skipped("u", now=20, ttl=600), 3)
        self.assertEqual(st.pop_skipped("u", now=21, ttl=600), 0, "取过就清掉")
        st.remember_skipped("u", 1, now=0)
        self.assertEqual(st.pop_skipped("u", now=601, ttl=600), 0, "太久以前的不再提")

    def test_skipped_note_wording(self):
        self.assertIn("一张表情包", rhythm.skipped_note(1))
        self.assertIn("3 张表情包", rhythm.skipped_note(3))
        self.assertIn("没有回", rhythm.skipped_note(2))
        self.assertEqual(rhythm.skipped_note(0), "")

class HelperTests(unittest.TestCase):
    def test_merge_keeps_order_and_skips_blank_text(self):
        text, comps = rhythm.merge([entry("先说一句", comps=["P1"]), entry("", sticker=True, comps=["IMG"]),
                                    entry("再补一句", comps=["P2"])])
        self.assertEqual(text, "先说一句\n再补一句")
        self.assertEqual(comps, ["P1", "IMG", "P2"])

    def test_sticker_detection(self):
        img = lambda **d: {"type": "image", "data": d}
        self.assertTrue(rhythm.is_sticker_only([img(sub_type=1)]))
        self.assertTrue(rhythm.is_sticker_only([img(sub_type="1")]))
        self.assertTrue(rhythm.is_sticker_only([img(sub_type=0, summary="[动画表情]")]))
        self.assertTrue(rhythm.is_sticker_only([img(emoji_id="abc")]))
        self.assertTrue(rhythm.is_sticker_only([{"type": "mface", "data": {}}]))
        self.assertTrue(rhythm.is_sticker_only([{"type": "face", "data": {"id": "14"}}]))
        self.assertTrue(rhythm.is_sticker_only([img(sub_type=1), {"type": "text", "data": {"text": "  "}}]))

    def test_not_sticker(self):
        img = lambda **d: {"type": "image", "data": d}
        self.assertFalse(rhythm.is_sticker_only([img(sub_type=0)]), "普通照片要回")
        self.assertFalse(rhythm.is_sticker_only([img(sub_type=1), {"type": "text", "data": {"text": "看"}}]))
        self.assertFalse(rhythm.is_sticker_only([{"type": "record", "data": {}}]))
        self.assertFalse(rhythm.is_sticker_only([]))
        self.assertFalse(rhythm.is_sticker_only(None))
        self.assertFalse(rhythm.is_sticker_only("not a list"))

    def test_should_skip(self):
        one = [entry("", sticker=True)]
        st = [entry("", sticker=True), entry("", sticker=True)]
        self.assertTrue(rhythm.should_skip(one, 0.6, roll=0.59))
        self.assertFalse(rhythm.should_skip(one, 0.6, roll=0.6))
        self.assertFalse(rhythm.should_skip(st + [entry("说话")], 0.6, roll=0.0))
        self.assertFalse(rhythm.should_skip(st, 0.0, roll=0.0))
        self.assertFalse(rhythm.should_skip([], 1.0, roll=0.0))

    def test_more_stickers_less_likely_to_be_ignored(self):
        st = lambda n: [entry("", sticker=True) for _ in range(n)]
        self.assertAlmostEqual(rhythm.skip_probability(st(1), 0.6), 0.6)
        self.assertAlmostEqual(rhythm.skip_probability(st(2), 0.6), 0.3)
        self.assertAlmostEqual(rhythm.skip_probability(st(3), 0.6), 0.15)
        self.assertTrue(rhythm.should_skip(st(2), 0.6, roll=0.29))
        self.assertFalse(rhythm.should_skip(st(2), 0.6, roll=0.31), "连发两张，不回的概率减半")
        self.assertFalse(rhythm.should_skip(st(3), 0.6, roll=0.2))

    def test_parse_typing(self):
        n = lambda text: {"post_type": "notice", "notice_type": "notify", "sub_type": "input_status", "status_text": text}
        self.assertTrue(rhythm.parse_typing(n("对方正在输入...")))
        self.assertFalse(rhythm.parse_typing(n("")))
        self.assertIsNone(rhythm.parse_typing({"post_type": "message"}))
        self.assertIsNone(rhythm.parse_typing({"post_type": "notice", "notice_type": "notify", "sub_type": "poke"}))
        self.assertIsNone(rhythm.parse_typing(None))

    def test_is_command(self):
        for t in ("/reset", ".rd", "。r 1d6", " /help"):
            self.assertTrue(rhythm.is_command(t, ["/", ".", "。"]), t)
        for t in ("你好", "", "…… 嗯"):
            self.assertFalse(rhythm.is_command(t, ["/", ".", "。"]), t)

    def test_settings_from_config_clamps_bad_values(self):
        s = rhythm.Settings.from_config({"quiet_seconds": "abc", "max_wait_seconds": -3, "sticker_skip_probability": 7})
        self.assertEqual(s.quiet_seconds, 4.0)
        self.assertGreaterEqual(s.max_wait_seconds, s.quiet_seconds)
        self.assertEqual(s.sticker_skip_probability, 1.0)
        self.assertEqual(rhythm.Settings.from_config(None).quiet_seconds, 4.0)


# ---------------------------------------------------------------- 插件入口
def _stub(name, **attrs):
    m = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Star:
    def __init__(self, context=None, *a, **k):
        self.context = context


class _Filter:
    class EventMessageType:
        ALL = "all"

    @staticmethod
    def event_message_type(*a, **k):
        return lambda f: f


class _Logger:
    def __init__(self):
        self.lines = []

    def info(self, m, *a):
        self.lines.append(m)

    debug = warning = error = info


LOG = _Logger()
for pkg in ("astrbot", "astrbot.api"):
    _stub(pkg)
_stub("astrbot.api", logger=LOG)
_stub("astrbot.api.event", AstrMessageEvent=object, filter=_Filter)
_stub("astrbot.api.star", Context=object, Star=_Star, register=lambda *a, **k: (lambda c: c))
sys.modules["astrbot.api"].event = sys.modules["astrbot.api.event"]
sys.modules["astrbot.api"].star = sys.modules["astrbot.api.star"]
sys.modules["rhythm"] = rhythm
main = _load("chat_rhythm_main", os.path.join(PLUGIN, "main.py"))


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, dt):
        target = self.t + dt
        while self.t < target - 1e-9:
            await asyncio.sleep(0)


class Obj:
    def __init__(self, raw, comps, ts=0):
        self.raw_message = raw
        self.message = comps
        self.timestamp = ts


class Ev:
    def __init__(self, text, raw_segments=None, umo="default:FriendMessage:10001", private=True, raw=None):
        self.message_str = text
        segs = raw_segments if raw_segments is not None else [{"type": "text", "data": {"text": text}}]
        self.message_obj = Obj(raw if raw is not None else {"post_type": "message", "message": segs}, [f"C:{text}"])
        self.unified_msg_origin = umo
        self._private = private
        self._stopped = False

    def is_private_chat(self):
        return self._private

    def get_platform_name(self):
        return "aiocqhttp"

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped


def notice(text, umo="default:FriendMessage:10001"):
    raw = {"post_type": "notice", "notice_type": "notify", "sub_type": "input_status", "status_text": text}
    return Ev("", raw=raw, umo=umo)


STICKER = [{"type": "image", "data": {"sub_type": 1}}]
PHOTO = [{"type": "image", "data": {"sub_type": 0}}]


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.rolls = []
        self.plugin = main.ChatRhythmPlugin(None, {"sticker_skip_probability": 0.6, "quiet_seconds": 4.0})
        self.plugin._clock = self.clock
        self.plugin._sleep = self.clock.sleep
        self.plugin._random = lambda: self.rolls.pop(0) if self.rolls else 0.99

    async def advance(self, seconds, step=0.1):
        for _ in range(int(round(seconds / step))):
            self.clock.t += step
            for _ in range(5):
                await asyncio.sleep(0)

    def run_async(self, coro):
        return asyncio.run(coro)

    def handle(self, ev):
        return asyncio.ensure_future(self.plugin.on_message(ev))

    def test_single_message_waits_quiet_then_passes_unchanged(self):
        async def go():
            ev = Ev("你好")
            t = self.handle(ev)
            await self.advance(3.8)
            self.assertFalse(t.done(), "4 秒静默前不该放行")
            await self.advance(0.4)
            await t
            return ev
        ev = self.run_async(go())
        self.assertFalse(ev.is_stopped())
        self.assertEqual(ev.message_str, "你好")

    def test_burst_merges_into_first_and_stops_followers(self):
        async def go():
            e1, e2, e3 = Ev("不是"), Ev("[对方发来一张图片或表情包]", STICKER), Ev("我在测试")
            e2.message_str = "[对方发来一张图片或表情包]"
            t1 = self.handle(e1)
            await self.advance(1)
            await self.handle(e2)
            await self.advance(2)
            await self.handle(e3)
            await self.advance(4.2)
            await t1
            return e1, e2, e3
        e1, e2, e3 = self.run_async(go())
        self.assertFalse(e1.is_stopped())
        self.assertTrue(e2.is_stopped() and e3.is_stopped())
        self.assertEqual(e1.message_str, "不是\n[对方发来一张图片或表情包]\n我在测试")
        self.assertEqual(e1.message_obj.message, ["C:不是", "C:[对方发来一张图片或表情包]", "C:我在测试"])

    def test_typing_notice_extends_wait(self):
        async def go():
            e1 = Ev("等下")
            t1 = self.handle(e1)
            await self.advance(1)
            await self.plugin.on_message(notice("对方正在输入..."))
            await self.advance(5)
            self.assertFalse(t1.done(), "对方还在打字")
            await self.plugin.on_message(notice(""))
            await self.advance(1.2)
            await t1
            return e1
        self.assertFalse(self.run_async(go()).is_stopped())

    def test_notice_event_itself_is_not_buffered(self):
        async def go():
            n = notice("对方正在输入...")
            await self.plugin.on_message(n)
            return n
        n = self.run_async(go())
        self.assertIsNone(self.plugin.state.take(n.unified_msg_origin))

    def test_commands_bypass_without_waiting(self):
        async def go():
            ev = Ev("/reset")
            await self.plugin.on_message(ev)
            return ev
        ev = self.run_async(go())
        self.assertFalse(ev.is_stopped())
        self.assertIsNone(self.plugin.state.take(ev.unified_msg_origin))

    def test_group_messages_pass_through(self):
        async def go():
            ev = Ev("大肥鱼 在吗", umo="default:GroupMessage:1", private=False)
            await self.plugin.on_message(ev)
            return ev
        self.assertFalse(self.run_async(go()).is_stopped())

    def test_disabled_passes_through(self):
        self.plugin.settings = rhythm.Settings.from_config({"enable": False})

        async def go():
            ev = Ev("你好")
            await self.plugin.on_message(ev)
            return ev
        self.assertEqual(self.run_async(go()).message_str, "你好")

    def test_leader_interrupted_drops_burst_and_next_starts_fresh(self):
        async def go():
            e1 = Ev("说一半")
            t1 = self.handle(e1)
            await self.advance(1)
            e1.stop_event()
            await self.advance(0.5)
            await t1
            e2 = Ev("新的话")
            t2 = self.handle(e2)
            await self.advance(4.6)  # 4 秒静默 + 一个 0.2 秒检查周期的余量
            await t2
            return e2
        e2 = self.run_async(go())
        self.assertFalse(e2.is_stopped())
        self.assertEqual(e2.message_str, "新的话")

    def test_max_wait_releases_even_while_typing(self):
        async def go():
            e1 = Ev("开头")
            t1 = self.handle(e1)
            for _ in range(6):
                await self.plugin.on_message(notice("对方正在输入..."))
                await self.advance(3)
            await t1
            return e1
        self.assertFalse(self.run_async(go()).is_stopped())
        self.assertGreaterEqual(self.clock.t, 15.0)

    def test_sticker_only_skipped_by_probability(self):
        self.rolls = [0.3]

        async def go():
            ev = Ev("[对方发来一张图片或表情包]", STICKER)
            t = self.handle(ev)
            await self.advance(4.2)
            await t
            return ev
        self.assertTrue(self.run_async(go()).is_stopped())

    def test_sticker_kept_when_roll_high(self):
        self.rolls = [0.7]

        async def go():
            ev = Ev("[对方发来一张图片或表情包]", STICKER)
            t = self.handle(ev)
            await self.advance(4.2)
            await t
            return ev
        self.assertFalse(self.run_async(go()).is_stopped())

    def test_photo_never_skipped(self):
        self.rolls = [0.0]

        async def go():
            ev = Ev("[对方发来一张图片或表情包]", PHOTO)
            t = self.handle(ev)
            await self.advance(4.2)
            await t
            return ev
        self.assertFalse(self.run_async(go()).is_stopped())

    def test_sticker_then_text_is_not_skipped(self):
        self.rolls = [0.0]

        async def go():
            e1, e2 = Ev("[对方发来一张图片或表情包]", STICKER), Ev("哈哈")
            t = self.handle(e1)
            await self.advance(1)
            await self.handle(e2)
            await self.advance(4.2)
            await t
            return e1
        self.assertFalse(self.run_async(go()).is_stopped())

    def _sticker_then_text(self, gap):
        self.rolls = [0.0, 0.99]

        async def go():
            e1 = Ev("[对方发来一张图片或表情包]", STICKER)
            t1 = self.handle(e1)
            await self.advance(4.4)
            await t1
            await self.advance(gap)
            e2 = Ev("你没有掉线罢")
            t2 = self.handle(e2)
            await self.advance(4.6)
            await t2
            return e1, e2
        return self.run_async(go())

    def test_skipped_sticker_is_mentioned_with_next_message(self):
        e1, e2 = self._sticker_then_text(gap=6)
        self.assertTrue(e1.is_stopped())
        self.assertFalse(e2.is_stopped())
        self.assertTrue(e2.message_str.startswith("[刚才对方发来一张表情包"), e2.message_str)
        self.assertTrue(e2.message_str.endswith("你没有掉线罢"))
        self.assertEqual(e2.message_obj.message, ["C:你没有掉线罢"], "只加说明，不把旧图再塞给模型")

    def test_stale_skip_is_not_mentioned(self):
        _, e2 = self._sticker_then_text(gap=700)
        self.assertEqual(e2.message_str, "你没有掉线罢")

    def test_command_does_not_consume_skip_note(self):
        self.rolls = [0.0, 0.99]

        async def go():
            e1 = Ev("[对方发来一张图片或表情包]", STICKER)
            t1 = self.handle(e1)
            await self.advance(4.4)
            await t1
            cmd = Ev("/help")
            await self.plugin.on_message(cmd)
            e2 = Ev("在吗")
            t2 = self.handle(e2)
            await self.advance(4.6)
            await t2
            return cmd, e2
        cmd, e2 = self.run_async(go())
        self.assertEqual(cmd.message_str, "/help")
        self.assertTrue(e2.message_str.startswith("[刚才对方发来一张表情包"))

    def test_handler_errors_never_block_the_message(self):
        async def boom(*a, **k):
            raise RuntimeError("x")
        self.plugin._sleep = boom

        async def go():
            ev = Ev("你好")
            await self.plugin.on_message(ev)
            return ev
        ev = self.run_async(go())
        self.assertFalse(ev.is_stopped())
        self.assertIsNone(self.plugin.state.take(ev.unified_msg_origin))


class WiringTests(unittest.TestCase):
    def test_plugin_files_and_staging(self):
        for f in ("main.py", "rhythm.py", "metadata.yaml", "_conf_schema.json"):
            self.assertTrue(os.path.isfile(os.path.join(PLUGIN, f)), f)
        # 按需安装（不进 deploy-kit 默认清单，免得所有部署悄悄多出等待和跳过）：README 要写清楚怎么装
        with open(os.path.join(ROOT, "deploy-windows", "README.md"), encoding="utf-8") as f:
            self.assertIn("astrbot_plugin_chat_rhythm", f.read())
        with open(os.path.join(ROOT, "deploy-kit", "preflight.py"), encoding="utf-8") as f:
            self.assertNotIn("astrbot_plugin_chat_rhythm", f.read())

    def test_runs_after_face_convert(self):
        with open(os.path.join(PLUGIN, "main.py"), encoding="utf-8") as f:
            self.assertRegex(f.read(), r"priority\s*=\s*-\d+")


if __name__ == "__main__":
    unittest.main()
