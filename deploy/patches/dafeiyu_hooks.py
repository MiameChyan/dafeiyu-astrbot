"""大肥鱼挂钩点注册表（记忆系统规格 §16.1，TRPG 规格 §5.1）。

挂载到 /AstrBot/astrbot/core/dafeiyu_hooks.py。核心补丁只在关键位置调用 emit_*；
判断与业务逻辑都在插件里，插件用 `from astrbot.core import dafeiyu_hooks` 注册回调。

本文件不依赖 AstrBot 其他模块，回调抛出的异常一律吞掉并记日志，不影响发送与流水线。

接口：
    send_origin: ContextVar[str]          发送来源，默认 "self"；发送前由调用方设置
                                          （command / npc / narration / dice / dice_hidden）
    register_inbound_observer(fn) -> 取消函数
        fn(event) 同步调用：在唤醒/分流判定之前，对每条非机器人自身的消息调用一次。
        回调要轻量，写库请自己放到后台任务。
    register_outbound(fn) -> 取消函数
        fn(info) 在 QQ 发送成功后以后台任务调用；info 有 is_group、session_id、
        message_chain、origin 四个属性。发送失败时不调用。

    register_predispatch(fn) -> 取消函数
        同步返回 None / Verdict.PASS、Verdict.BLOCK 或 Verdict.WAKE(message_str=None)。
    predispatch(event) -> Verdict
        按注册顺序取第一个非放行判定；异常、异步结果与非法值按放行处理。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, ClassVar

logger = logging.getLogger("astrbot")

send_origin: ContextVar[str] = ContextVar("dafeiyu_send_origin", default="self")

# 每次发送的可选回执容器；ContextVar 防止并发群聊互相拿错消息 ID。
send_receipts: ContextVar[list | None] = ContextVar("dafeiyu_send_receipts", default=None)


def capture_send_result(result: Any) -> None:
    receipts = send_receipts.get()
    if receipts is None or not isinstance(result, dict):
        return
    mid = result.get("message_id")
    if mid is not None:
        receipts.append(mid)


_inbound: list[Callable[[Any], Any]] = []
_outbound: list[Callable[[Any], Any]] = []
_tasks: set[asyncio.Task] = set()  # 持有引用，防止后台任务被回收


@dataclass(frozen=True)
class OutboundInfo:
    is_group: bool
    session_id: str
    message_chain: Any
    origin: str


def _register(bucket: list, fn: Callable) -> Callable[[], None]:
    bucket.append(fn)

    def undo() -> None:
        try:
            bucket.remove(fn)
        except ValueError:
            pass

    return undo


def register_inbound_observer(fn: Callable[[Any], Any]) -> Callable[[], None]:
    return _register(_inbound, fn)


def register_outbound(fn: Callable[[Any], Any]) -> Callable[[], None]:
    return _register(_outbound, fn)


def _name(fn: Callable) -> str:
    return getattr(fn, "__qualname__", None) or repr(fn)


def emit_inbound(event: Any) -> None:
    for fn in tuple(_inbound):
        try:
            result = fn(event)
            if inspect.isawaitable(result):
                _spawn(fn, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dafeiyu_hooks] 入站观察回调 {_name(fn)} 出错，已忽略：{exc}")


def emit_outbound(*, is_group: bool, session_id: Any, message_chain: Any) -> None:
    if not _outbound:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    info = OutboundInfo(
        is_group=bool(is_group),
        session_id="" if session_id is None else str(session_id),
        message_chain=message_chain,
        origin=send_origin.get(),
    )
    for fn in tuple(_outbound):
        _spawn(fn, _call(fn, info))


async def _call(fn: Callable, info: OutboundInfo) -> None:
    result = fn(info)
    if inspect.isawaitable(result):
        await result


def _spawn(fn: Callable, awaitable: Any) -> None:
    async def guarded() -> None:
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dafeiyu_hooks] 回调 {_name(fn)} 出错，已忽略：{exc}")

    try:
        task = asyncio.get_running_loop().create_task(guarded())
    except RuntimeError:
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@dataclass(frozen=True)
class Verdict:
    """稳定的同步分流判定；只有 WAKE 可以携带消息改写。"""

    kind: str
    message_str: str | None = None
    PASS: ClassVar[Verdict]
    BLOCK: ClassVar[Verdict]

    @classmethod
    def WAKE(cls, message_str: str | None = None) -> Verdict:
        return cls("wake", message_str)


Verdict.PASS = Verdict("pass")
Verdict.BLOCK = Verdict("block")
_predispatch: list[Callable[[Any], Any]] = []


def register_predispatch(fn: Callable[[Any], Any]) -> Callable[[], None]:
    """注册轻量同步判定，返回可重复调用的取消函数。"""
    return _register(_predispatch, fn)


def predispatch(event: Any) -> Verdict:
    """按快照顺序取首个非放行判定；回调失败不影响其余回调。"""
    for fn in tuple(_predispatch):
        try:
            verdict = fn(event)
            if inspect.isawaitable(verdict):
                if inspect.iscoroutine(verdict):
                    verdict.close()
                logger.warning("[dafeiyu_hooks] 分流回调返回异步结果，已按放行处理")
                continue
            if verdict is None or verdict == Verdict.PASS:
                continue
            if isinstance(verdict, Verdict) and (
                verdict == Verdict.BLOCK
                or (verdict.kind == "wake" and (
                    verdict.message_str is None or isinstance(verdict.message_str, str)
                ))
            ):
                return verdict
            logger.warning("[dafeiyu_hooks] 分流回调返回非法判定，已按放行处理")
        except Exception:
            # 不记录异常消息和事件，避免回调把账号、密钥带入日志。
            logger.warning("[dafeiyu_hooks] 分流回调抛出异常，已按放行继续处理")
    return Verdict.PASS
