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

TRPG 分流器 register_predispatch 留到 TRPG 骰子插件实现时再加（它需要能改变流水线走向）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("astrbot")

send_origin: ContextVar[str] = ContextVar("dafeiyu_send_origin", default="self")

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
