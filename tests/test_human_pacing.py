# -*- coding: utf-8 -*-
"""拟人节奏的纯逻辑回归（不依赖 AstrBot）。

跑法：python -m unittest tests.test_human_pacing  或  python tests/test_human_pacing.py
"""
import asyncio
import importlib.util
import os
import random
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACING = os.path.join(ROOT, "deploy", "patches", "human_pacing.py")


def load_pacing():
    spec = importlib.util.spec_from_file_location("human_pacing", PACING)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hp = load_pacing()

CST = timezone(timedelta(hours=8))
DAY = datetime(2026, 11, 15, 15, 0, tzinfo=CST).timestamp()  # 下午三点
NIGHT = datetime(2026, 11, 15, 3, 0, tzinfo=CST).timestamp()  # 凌晨三点


class MidRng:
    """uniform 永远取中点，让结果可预期。"""

    def uniform(self, a, b):
        return (a + b) / 2


class LowRng:
    def uniform(self, a, b):
        return a


class HighRng:
    def uniform(self, a, b):
        return b


class PureFunctionTests(unittest.TestCase):
    def test_night_window_is_1_to_7_beijing_time(self):
        self.assertFalse(hp.is_night(DAY))
        self.assertTrue(hp.is_night(NIGHT))
        at = lambda h, m=0: datetime(2026, 11, 15, h, m, tzinfo=CST).timestamp()
        self.assertTrue(hp.is_night(at(1)))
        self.assertTrue(hp.is_night(at(6, 59)))
        self.assertFalse(hp.is_night(at(7)))
        self.assertFalse(hp.is_night(at(0, 59)))

    def test_text_len_ignores_whitespace(self):
        self.assertEqual(hp.text_len("唔姆 好的\n"), 4)
        self.assertEqual(hp.text_len(""), 0)
        self.assertEqual(hp.text_len(None), 0)

    def test_typing_scales_with_length_and_caps(self):
        short = hp.typing_seconds(6, night=False, rng=MidRng())
        longer = hp.typing_seconds(30, night=False, rng=MidRng())
        self.assertAlmostEqual(short, 1.0)  # 6 字 / 6 字每秒
        self.assertAlmostEqual(longer, 5.0)
        self.assertEqual(hp.typing_seconds(10_000, night=False, rng=MidRng()), 12.0)
        self.assertEqual(hp.typing_seconds(0, night=False, rng=MidRng()), 0.0)

    def test_typing_speed_jitter_is_about_20_percent(self):
        slow = hp.typing_seconds(48, night=False, rng=LowRng())  # 4.8 字/秒
        fast = hp.typing_seconds(48, night=False, rng=HighRng())  # 7.2 字/秒
        self.assertAlmostEqual(slow, 10.0)
        self.assertAlmostEqual(fast, 48 / 7.2)

    def test_reading_scales_and_caps(self):
        self.assertAlmostEqual(hp.reading_seconds(0, night=False), 0.6)
        self.assertAlmostEqual(hp.reading_seconds(50, night=False), 2.6)
        self.assertEqual(hp.reading_seconds(10_000, night=False), 4.0)

    def test_night_is_slower_everywhere(self):
        self.assertGreater(hp.reading_seconds(50, night=True), hp.reading_seconds(50, night=False))
        self.assertGreater(hp.thinking_seconds(night=True, rng=MidRng()), hp.thinking_seconds(night=False, rng=MidRng()))
        self.assertGreater(hp.typing_seconds(30, night=True, rng=MidRng()), hp.typing_seconds(30, night=False, rng=MidRng()))
        self.assertEqual(hp.typing_seconds(10_000, night=True, rng=MidRng()), 18.0)

    def test_first_delay_subtracts_time_already_spent(self):
        # 读 0.6+25/25=1.6，想 1.25，打 30/6=5 → 共 7.85
        fresh = hp.first_delay(25, 30, received_at=DAY, now=DAY, rng=MidRng())
        self.assertAlmostEqual(fresh, 7.85)
        later = hp.first_delay(25, 30, received_at=DAY - 5, now=DAY, rng=MidRng())
        self.assertAlmostEqual(later, 2.85)

    def test_first_delay_floor_when_llm_was_slow(self):
        self.assertEqual(hp.first_delay(25, 30, received_at=DAY - 60, now=DAY, rng=MidRng()), 0.4)

    def test_first_delay_cap_day_and_night(self):
        # 各段都封顶后：白天 4+2+12=18，夜里 7.2+8+18=33.2，总上限只是兜底
        day = hp.first_delay(10_000, 10_000, received_at=DAY, now=DAY, rng=HighRng())
        night = hp.first_delay(10_000, 10_000, received_at=NIGHT, now=NIGHT, rng=HighRng())
        self.assertAlmostEqual(day, 18.0)
        self.assertAlmostEqual(night, 33.2)
        self.assertLessEqual(day, hp.DAY["first_cap"])
        self.assertLessEqual(night, hp.NIGHT["first_cap"])

    def test_first_delay_bad_timestamps_count_as_just_received(self):
        expect = hp.first_delay(25, 30, received_at=DAY, now=DAY, rng=MidRng())
        for bad in (None, 0, -5, "x", DAY + 3600):
            self.assertAlmostEqual(hp.first_delay(25, 30, received_at=bad, now=DAY, rng=MidRng()), expect, msg=repr(bad))

    def test_bubble_gap_follows_typing_with_floor(self):
        self.assertAlmostEqual(hp.bubble_gap(30, now=DAY, rng=MidRng()), 5.0 + 0.5)
        self.assertEqual(hp.bubble_gap(1, now=DAY, rng=LowRng()), 1.0)
        self.assertGreater(hp.bubble_gap(30, now=NIGHT, rng=MidRng()), hp.bubble_gap(30, now=DAY, rng=MidRng()))

    def test_default_rng_stays_in_range(self):
        rng = random.Random(7)
        for _ in range(200):
            d = hp.first_delay(40, 60, received_at=DAY, now=DAY, rng=rng)
            self.assertGreaterEqual(d, 0.4)
            self.assertLessEqual(d, 20.0)


class FakeClock:
    """假时钟：sleep 只推进时间，不真等。"""

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += max(0.0, seconds)
        await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


class LimiterTests(unittest.TestCase):
    def make(self, **kw):
        clock = FakeClock()
        lim = hp.OutboundLimiter(clock=clock, sleep=clock.sleep, rng=kw.pop("rng", LowRng()), **kw)
        return lim, clock

    def test_first_message_goes_immediately(self):
        lim, clock = self.make()
        run(lim.acquire())
        self.assertEqual(clock.slept, [])

    def test_min_gap_between_messages(self):
        lim, clock = self.make()

        async def go():
            stamps = []
            for _ in range(3):
                await lim.acquire()
                stamps.append(clock())
            return stamps

        stamps = run(go())
        self.assertAlmostEqual(stamps[1] - stamps[0], 0.8)
        self.assertAlmostEqual(stamps[2] - stamps[1], 0.8)

    def test_no_wait_when_gap_already_passed(self):
        lim, clock = self.make()

        async def go():
            await lim.acquire()
            clock.t += 5
            await lim.acquire()

        run(go())
        self.assertEqual(clock.slept, [])

    def test_window_cap_blocks_the_16th_message(self):
        lim, clock = self.make(min_gap=(0.0, 0.0))

        async def go():
            stamps = []
            for _ in range(16):
                await lim.acquire()
                stamps.append(clock())
            return stamps

        stamps = run(go())
        self.assertTrue(all(s == stamps[0] for s in stamps[:15]), "前 15 条不该等")
        self.assertAlmostEqual(stamps[15] - stamps[0], 60.0)

    def test_concurrent_callers_queue_in_order(self):
        lim, clock = self.make()
        order = []

        async def sender(name):
            await lim.acquire()
            order.append((name, clock()))

        async def go():
            await asyncio.gather(*(sender(n) for n in "abcd"))

        run(go())
        self.assertEqual([n for n, _ in order], list("abcd"))
        times = [t for _, t in order]
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, 0.8 - 1e-9)

    def test_cancelled_waiter_does_not_consume_a_slot(self):
        lim, clock = self.make(min_gap=(0.0, 0.0), max_per_window=1)

        async def go():
            await lim.acquire()
            task = asyncio.ensure_future(lim.acquire())
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # 被取消的那次不算数：窗口里仍只有 1 条
            return len(lim._sent)

        self.assertEqual(run(go()), 1)

    def test_shared_limiter_is_a_singleton(self):
        self.assertIs(hp.get_limiter(), hp.get_limiter())


class WiringTests(unittest.TestCase):
    """补丁文件和挂载点要对得上：respond_stage / aiocqhttp 都按这个路径导入。"""

    def test_windows_compose_mounts_pacing_patches(self):
        with open(os.path.join(ROOT, "deploy-windows", "compose.yaml"), encoding="utf-8") as f:
            compose = f.read()
        self.assertIn(
            "../deploy/patches/human_pacing.py:/AstrBot/astrbot/core/utils/human_pacing.py:ro",
            compose,
        )
        self.assertIn(
            "../deploy/patches/aiocqhttp_message_event.py:"
            "/AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py:ro",
            compose,
        )

    def test_patches_import_the_mounted_module_with_fallback(self):
        for name in ("respond_stage.py", "aiocqhttp_message_event.py"):
            with open(os.path.join(ROOT, "deploy", "patches", name), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("from astrbot.core.utils", src, name)
            self.assertIn("human_pacing", src, name)
            self.assertIn("except ImportError", src, name)

    def test_every_outbound_path_is_gated(self):
        with open(os.path.join(ROOT, "deploy", "patches", "aiocqhttp_message_event.py"), encoding="utf-8") as f:
            src = f.read()
        # _dispatch_send 一处 + 群/私聊合并转发各一处
        self.assertEqual(src.count("await _outbound_gate()"), 3)


if __name__ == "__main__":
    unittest.main()
