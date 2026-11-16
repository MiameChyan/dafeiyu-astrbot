# -*- coding: utf-8 -*-
"""私聊连发合并 + 纯表情包按概率不回，让回复节奏更像真人。

- 对方连着发几条时，第一条领头等待：最后一条之后静默够久、且对方不在打字（NapCat
  “输入状态”通知），才带着全部文字和图片只回一次；一直在打字也最多等 max_wait 秒。
- 这一批全是表情包（不含普通照片）时，按 sticker_skip_probability 直接不回；对方下一条
  消息前会加一行说明，让她知道看到了但没回。
- 指令（/、.、。开头）和群聊不受影响；插件自身出错时一律放行原消息。
优先级低于 face_convert（它先把表情、图片翻译成文字），所以合并的是翻译后的文本。
"""
import asyncio
import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from . import rhythm as R
except ImportError:  # 测试里按文件加载
    import rhythm as R


@register(
    "astrbot_plugin_chat_rhythm",
    "dafeiyu-tools",
    "私聊连发合并、纯表情包按概率不回，让回复节奏更像真人",
    "1.0.0",
)
class ChatRhythmPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.settings = R.Settings.from_config(config)
        self.state = R.RhythmState()
        self._clock = time.monotonic
        self._sleep = asyncio.sleep
        self._random = random.random

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def on_message(self, event: AstrMessageEvent):
        try:
            await self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[chat_rhythm] 处理出错，放行原消息：{type(e).__name__}: {e}")
            self._release(event)

    async def _handle(self, event) -> None:
        raw = getattr(event.message_obj, "raw_message", None)
        key = event.unified_msg_origin
        typing = R.parse_typing(raw)
        if typing is not None:
            self.state.on_typing(key, typing, self._clock())
            return
        s = self.settings
        if not s.enable or not event.is_private_chat():
            return
        if not hasattr(raw, "get") or raw.get("post_type") != "message":
            return
        text = (event.message_str or "").strip()
        comps = list(getattr(event.message_obj, "message", None) or [])
        if not text and not comps:
            return
        if R.is_command(text, s.command_prefixes):
            return

        entry = R.Entry(text=text, components=comps, sticker_only=R.is_sticker_only(raw.get("message")))
        leads = self.state.add_message(key, event, entry, self._clock(),
                                       is_alive=lambda ev: not ev.is_stopped())
        if not leads:
            event.stop_event()  # 内容已交给领头消息
            return

        while not self.state.wait_done(key, self._clock(), s):
            if event.is_stopped():
                self.state.discard(key, event)
                logger.info("[chat_rhythm] 等待中被打断，这一批不再回复")
                return
            await self._sleep(s.tick_seconds)
        if event.is_stopped():
            self.state.discard(key, event)
            return

        burst = self.state.take(key)
        if burst is None or burst.leader != event:
            return
        self._absorb(event, burst)
        now = self._clock()
        if R.should_skip(burst.entries, s.sticker_skip_probability, self._random()):
            self.state.remember_skipped(key, len(burst.entries), now)
            event.stop_event()
            logger.info(f"[chat_rhythm] 只有表情包（{len(burst.entries)} 条），这次不回")
            return
        note = R.skipped_note(self.state.pop_skipped(key, now, s.skipped_note_ttl_seconds))
        if note:
            # 只加一行说明，不把旧图再交给模型：她知道自己看到了没回，但不会回头评论那张图
            event.message_str = note + "\n" + event.message_str
            logger.info(f"[chat_rhythm] 带上没回的表情包说明：{note}")
            if hasattr(event.message_obj, "message_str"):
                event.message_obj.message_str = event.message_str

    def _absorb(self, event, burst) -> None:
        if len(burst.entries) < 2:
            return
        text, comps = R.merge(burst.entries)
        event.message_str = text
        event.message_obj.message = comps
        if hasattr(event.message_obj, "message_str"):
            event.message_obj.message_str = text
        logger.info(f"[chat_rhythm] 合并了 {len(burst.entries)} 条连发消息，一次回复")

    def _release(self, event) -> None:
        """出错兜底：若本条是领头消息，把已收下的内容并进来后放行，不丢消息。"""
        try:
            burst = self.state.bursts.get(event.unified_msg_origin)
            if burst is not None and burst.leader == event:
                self.state.take(event.unified_msg_origin)
                self._absorb(event, burst)
        except Exception:
            pass
