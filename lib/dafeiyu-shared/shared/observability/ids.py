"""关联 ID 链（日志规格 §3）：turn_id → run_id → round → call_id，另有 job_id。

ID 通过 contextvars 随协程自动传播。AstrBot 4.29.4 源码确认：
- 每个消息事件在独立 task 中处理（event_bus.create_task），事件间互不串号；
- 插件钩子按顺序 await（context_utils.call_event_hook），钩子里 set 的值对后续流水线可见；
- 工具调用经 asyncio.create_task 执行，创建时复制上下文，能继承 turn_id。
作为兜底（日志规格 §11、V1），begin_turn 同时把 turn_id 挂到 event 的 extra 上。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from ulid import ULID

EVENT_EXTRA_KEY = "_obs_turn_id"

turn_id: ContextVar[str | None] = ContextVar("obs_turn_id", default=None)
run_id: ContextVar[str | None] = ContextVar("obs_run_id", default=None)
round_no: ContextVar[int | None] = ContextVar("obs_round", default=None)
job_id: ContextVar[str | None] = ContextVar("obs_job_id", default=None)
call_id: ContextVar[str | None] = ContextVar("obs_call_id", default=None)

_VARS: dict[str, ContextVar] = {
    "turn_id": turn_id,
    "run_id": run_id,
    "round": round_no,
    "job_id": job_id,
    "call_id": call_id,
}


def new_id() -> str:
    """ULID：时间有序、全局唯一。"""
    return str(ULID())


def current() -> dict[str, Any]:
    """当前上下文中非空的关联 ID。"""
    out = {}
    for name, var in _VARS.items():
        value = var.get()
        if value is not None:
            out[name] = value
    return out


@contextmanager
def bind(**ids: Any) -> Iterator[None]:
    """在 with 块内临时设置若干 ID，退出时恢复。"""
    tokens = []
    try:
        for name, value in ids.items():
            var = _VARS[name]
            tokens.append((var, var.set(value)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def begin_turn(event: Any = None) -> str:
    """一次触发的开始：生成 turn_id，写入当前上下文（不 reset，随事件 task 结束而消失）。

    如果传入 AstrBot 事件，同时写入 event.set_extra，供上下文丢失时兜底读取。
    已有 turn_id 的事件（同一事件多次进入钩子）复用原值。
    """
    existing = turn_from_event(event)
    tid = existing or new_id()
    turn_id.set(tid)
    if event is not None and existing is None and hasattr(event, "set_extra"):
        event.set_extra(EVENT_EXTRA_KEY, tid)
    return tid


def turn_from_event(event: Any) -> str | None:
    if event is None or not hasattr(event, "get_extra"):
        return None
    value = event.get_extra(EVENT_EXTRA_KEY)
    return value if isinstance(value, str) else None


def ensure_turn(event: Any = None) -> str:
    """优先用上下文中的 turn_id，其次 event 上的，最后新建。"""
    return turn_id.get() or turn_from_event(event) or begin_turn(event)
