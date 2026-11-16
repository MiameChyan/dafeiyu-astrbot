# -*- coding: utf-8 -*-
"""聊天节奏的纯逻辑（不依赖 AstrBot，便于测试）。

- 连发合并：同一私聊里第一条消息领头等待，后来的消息交给它；对方停下（静默够久、
  且不在打字）或等满上限后，领头消息带着全部内容只走一次大模型。
- 表情包兜底：这一批全是表情包时，按概率直接不回；没回的会在对方下一条消息前加一行
  说明，让她知道自己看到了但没回（不会再说“以为你走了”）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Settings:
    enable: bool = True
    quiet_seconds: float = 4.0          # 最后一条消息后静默多久才回
    max_wait_seconds: float = 15.0      # 从第一条算起最多等多久（一直在打字也不再等）
    typing_ttl_seconds: float = 7.0     # “正在输入”多久没刷新就当停了（QQ 约 5 秒刷新一次）
    stop_grace_seconds: float = 1.0     # 收到“停止输入”后再等一下，接住刚好发出的消息
    sticker_skip_probability: float = 0.6
    skipped_note_ttl_seconds: float = 600.0  # 没回的表情包多久内还在下一条里提一句
    command_prefixes: list = field(default_factory=lambda: ["/", ".", "。"])
    tick_seconds: float = 0.2

    @classmethod
    def from_config(cls, cfg) -> "Settings":
        cfg = cfg if hasattr(cfg, "get") else {}
        d = cls()

        def num(key, default, lo, hi):
            try:
                v = float(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            if v != v:  # NaN
                return default
            return min(hi, max(lo, v))

        quiet = num("quiet_seconds", d.quiet_seconds, 0.5, 30.0)
        prefixes = cfg.get("command_prefixes", d.command_prefixes)
        if not isinstance(prefixes, list):
            prefixes = d.command_prefixes
        return cls(
            enable=bool(cfg.get("enable", True)),
            quiet_seconds=quiet,
            max_wait_seconds=max(quiet, num("max_wait_seconds", d.max_wait_seconds, 0.5, 60.0)),
            typing_ttl_seconds=num("typing_ttl_seconds", d.typing_ttl_seconds, 1.0, 30.0),
            stop_grace_seconds=num("stop_grace_seconds", d.stop_grace_seconds, 0.0, 10.0),
            sticker_skip_probability=num("sticker_skip_probability", d.sticker_skip_probability, 0.0, 1.0),
            skipped_note_ttl_seconds=num("skipped_note_ttl_seconds", d.skipped_note_ttl_seconds, 0.0, 86400.0),
            command_prefixes=[str(p) for p in prefixes if str(p)],
        )


@dataclass
class Entry:
    text: str
    components: list
    sticker_only: bool = False


@dataclass
class Burst:
    leader: object
    started_at: float
    last_at: float
    entries: list


class RhythmState:
    """每个会话一批待回的消息 + 对方的打字状态。单线程 asyncio 下使用，无需加锁。"""

    def __init__(self) -> None:
        self.bursts: dict[str, Burst] = {}
        self.typing: dict[str, tuple[bool, float]] = {}
        self.skipped: dict[str, tuple[int, float]] = {}  # 没回的表情包张数、最近一次时间

    def on_typing(self, key, active: bool, now: float) -> None:
        self.typing[key] = (bool(active), now)

    def add_message(self, key, leader, entry: Entry, now: float, is_alive=None) -> bool:
        """加入当前批次；返回 True 表示这条消息领头（由它负责等待和回复）。"""
        burst = self.bursts.get(key)
        if burst is not None and (is_alive is None or is_alive(burst.leader)):
            burst.entries.append(entry)
            burst.last_at = now
            return False
        self.bursts[key] = Burst(leader=leader, started_at=now, last_at=now, entries=[entry])
        return True

    def wait_done(self, key, now: float, s: Settings) -> bool:
        burst = self.bursts.get(key)
        if burst is None:
            return True
        if now - burst.started_at >= s.max_wait_seconds:
            return True
        state = self.typing.get(key)
        if state is not None:
            active, at = state
            if active and now - at <= s.typing_ttl_seconds:
                return False
            if not active and at >= burst.last_at and now - at < s.stop_grace_seconds:
                return False
        return now - burst.last_at >= s.quiet_seconds

    def remember_skipped(self, key, count: int, now: float) -> None:
        prev, _ = self.skipped.get(key, (0, now))
        self.skipped[key] = (prev + max(0, int(count)), now)

    def pop_skipped(self, key, now: float, ttl: float) -> int:
        count, at = self.skipped.pop(key, (0, now))
        return count if now - at <= ttl else 0

    def take(self, key):
        return self.bursts.pop(key, None)

    def discard(self, key, leader) -> None:
        burst = self.bursts.get(key)
        if burst is not None and burst.leader == leader:
            del self.bursts[key]


def skipped_note(count: int) -> str:
    if count <= 0:
        return ""
    if count == 1:
        return "[刚才对方发来一张表情包，你看到了，没有回]"
    return f"[刚才对方连着发来 {count} 张表情包，你看到了，没有回]"


def merge(entries) -> tuple[str, list]:
    text = "\n".join(e.text for e in entries if e.text and e.text.strip())
    comps: list = []
    for e in entries:
        comps.extend(e.components or [])
    return text, comps


def _image_is_sticker(data: dict) -> bool:
    sub_type = data.get("sub_type", data.get("subType"))
    if sub_type in (1, "1"):
        return True
    if "表情" in str(data.get("summary") or ""):
        return True
    return bool(data.get("emoji_id") or data.get("emoji_package_id"))


def is_sticker_only(segments) -> bool:
    """OneBot 消息段是否只有表情包 / QQ 表情（普通照片、文字、语音都不算）。"""
    if not isinstance(segments, list) or not segments:
        return False
    found = False
    for seg in segments:
        if not isinstance(seg, dict):
            return False
        kind = seg.get("type")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if kind == "text":
            if str(data.get("text", "")).strip():
                return False
        elif kind in ("face", "mface", "marketface"):
            found = True
        elif kind == "image":
            if not _image_is_sticker(data):
                return False
            found = True
        elif kind in ("reply", "at"):
            continue
        else:
            return False
    return found


def skip_probability(entries, probability: float) -> float:
    """连发越多张，越像在等人接话：每多一张，不回的概率减半（1 张 p，2 张 p/2，3 张 p/4）。"""
    if not entries or probability <= 0 or not all(e.sticker_only for e in entries):
        return 0.0
    return probability * (0.5 ** (len(entries) - 1))


def should_skip(entries, probability: float, roll: float) -> bool:
    return roll < skip_probability(entries, probability)


def parse_typing(raw):
    """NapCat 输入状态通知：True=正在输入，False=停止输入，None=不是这类通知。"""
    if not hasattr(raw, "get"):
        return None
    if raw.get("post_type") != "notice" or raw.get("notice_type") != "notify":
        return None
    if raw.get("sub_type") != "input_status":
        return None
    return bool(str(raw.get("status_text") or "").strip())


def is_command(text: str, prefixes) -> bool:
    t = (text or "").lstrip()
    return bool(t) and any(t.startswith(p) for p in prefixes)
