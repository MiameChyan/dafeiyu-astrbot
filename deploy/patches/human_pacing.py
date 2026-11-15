# -*- coding: utf-8 -*-
"""拟人节奏：让回复的时机像人在看消息、想一想、再打字。

纯逻辑，不依赖 AstrBot，挂载到容器里的 astrbot/core/utils/human_pacing.py。

- first_delay：收到消息后第一条回复前还要再等多久（扣掉模型已经花掉的时间）。
- bubble_gap：多个气泡之间的间隔，按下一个气泡的字数模拟打字。
- OutboundLimiter：全局出站限速，所有会话共用；只排队，不丢消息。

时间都按北京时间算；凌晨 1–7 点整体放慢。
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
NIGHT_START_HOUR = 1
NIGHT_END_HOUR = 7

CHARS_PER_SECOND = 6.0
SPEED_JITTER = 0.2

READ_BASE = 0.6
READ_CHARS_PER_SECOND = 25.0
READ_CAP = 4.0

DAY = {"read_mul": 1.0, "think": (0.5, 2.0), "cps_mul": 1.0, "typing_cap": 12.0, "first_cap": 20.0}
NIGHT = {"read_mul": 1.8, "think": (2.0, 8.0), "cps_mul": 0.7, "typing_cap": 18.0, "first_cap": 35.0}

FIRST_FLOOR = 0.4
BUBBLE_FLOOR = 1.0
BUBBLE_PAUSE = (0.2, 0.8)


def is_night(now: float) -> bool:
    hour = datetime.fromtimestamp(now, BEIJING).hour
    return NIGHT_START_HOUR <= hour < NIGHT_END_HOUR


def _profile(night: bool) -> dict:
    return NIGHT if night else DAY


def text_len(text) -> int:
    if not text:
        return 0
    return sum(1 for c in str(text) if not c.isspace())


def reading_seconds(incoming_len: int, night: bool) -> float:
    base = min(READ_CAP, READ_BASE + max(0, incoming_len) / READ_CHARS_PER_SECOND)
    return base * _profile(night)["read_mul"]


def thinking_seconds(night: bool, rng=random) -> float:
    lo, hi = _profile(night)["think"]
    return rng.uniform(lo, hi)


def typing_seconds(reply_len: int, night: bool, rng=random) -> float:
    if reply_len <= 0:
        return 0.0
    p = _profile(night)
    cps = CHARS_PER_SECOND * rng.uniform(1 - SPEED_JITTER, 1 + SPEED_JITTER) * p["cps_mul"]
    return min(p["typing_cap"], reply_len / cps)


def _elapsed(received_at, now: float) -> float:
    """收到消息到现在过了多久；时间戳缺失、非法或在未来时当作刚收到。"""
    try:
        received = float(received_at)
    except (TypeError, ValueError):
        return 0.0
    if received <= 0 or received > now:
        return 0.0
    return now - received


def first_delay(incoming_len: int, reply_len: int, received_at, now: float | None = None, rng=random) -> float:
    if now is None:
        now = time.time()
    night = is_night(now)
    total = (
        reading_seconds(incoming_len, night)
        + thinking_seconds(night, rng)
        + typing_seconds(reply_len, night, rng)
    )
    wait = total - _elapsed(received_at, now)
    return min(_profile(night)["first_cap"], max(FIRST_FLOOR, wait))


def bubble_gap(reply_len: int, now: float | None = None, rng=random) -> float:
    if now is None:
        now = time.time()
    night = is_night(now)
    gap = typing_seconds(reply_len, night, rng) + rng.uniform(*BUBBLE_PAUSE)
    return max(BUBBLE_FLOOR, gap)


class OutboundLimiter:
    """全局出站限速：相邻两条至少隔 min_gap 秒，任意 window 秒内至多 max_per_window 条。

    超出时排队等待而不是丢弃；等待中被取消不会占用名额。
    """

    def __init__(
        self,
        min_gap: tuple[float, float] = (0.8, 1.6),
        max_per_window: int = 15,
        window: float = 60.0,
        clock=time.monotonic,
        sleep=asyncio.sleep,
        rng=random,
    ):
        self.min_gap = min_gap
        self.max_per_window = max_per_window
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._lock: asyncio.Lock | None = None
        self._sent: deque[float] = deque()
        self._last: float | None = None
        self._gap = 0.0
        self.waited_total = 0.0

    def _wait_needed(self, now: float) -> float:
        while self._sent and self._sent[0] <= now - self.window:
            self._sent.popleft()
        wait = 0.0
        if self._last is not None:
            wait = max(wait, self._last + self._gap - now)
        if len(self._sent) >= self.max_per_window:
            wait = max(wait, self._sent[0] + self.window - now)
        return wait

    async def acquire(self) -> float:
        """等到可以发送为止，返回等了多久（秒）。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        waited = 0.0
        async with self._lock:
            while True:
                wait = self._wait_needed(self._clock())
                if wait <= 0:
                    break
                await self._sleep(wait)
                waited += wait
            now = self._clock()
            self._sent.append(now)
            self._last = now
            self._gap = self._rng.uniform(*self.min_gap)
        self.waited_total += waited
        return waited


_LIMITER: OutboundLimiter | None = None


def get_limiter() -> OutboundLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = OutboundLimiter()
    return _LIMITER
