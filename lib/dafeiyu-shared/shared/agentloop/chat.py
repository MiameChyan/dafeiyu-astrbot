"""模型调用抽象与埋点（日志规格 §4.2）。

ChatFn 是与平台无关的调用约定；`from_astrbot_provider` 把 AstrBot 的 Provider 包成 ChatFn。
`InstrumentedChat` 在每次调用前扣预算、调用后写 provider_call 日志与指标，并把这一轮的调用记录交给回放。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from shared.observability import ids, obs


@dataclass
class Usage:
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None  # None = Provider 未返回（不是 0）


@dataclass
class Reply:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str | None = None
    raw: Any = None


class ChatFn(Protocol):
    async def __call__(self, *, system: str, prompt: str, temperature: float | None = None,
                       max_tokens: int | None = None) -> Reply: ...


class BudgetExceeded(RuntimeError):
    def __init__(self, kind: str, remaining: int) -> None:
        super().__init__(f"budget exceeded: {kind}")
        self.kind = kind
        self.remaining = remaining


class Budget:
    """调用次数预算；charge 在每次 Provider 调用前执行，超额抛 BudgetExceeded。"""

    def __init__(self, max_calls: int, kind: str = "calls") -> None:
        self.max_calls = max_calls
        self.kind = kind
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)

    def charge(self) -> None:
        if self.used >= self.max_calls:
            raise BudgetExceeded(self.kind, self.remaining)
        self.used += 1


PriceTable = dict[str, dict[str, float]]  # model -> {"in": 元/百万, "in_cached": …, "out": …}


def estimate_cost(model: str | None, usage: Usage, prices: PriceTable | None) -> float | None:
    if not prices or not model or model not in prices:
        return None
    p = prices[model]
    cached = usage.tokens_cached or 0
    uncached = max(0, (usage.tokens_in or 0) - cached)
    return round((uncached * p.get("in", 0) + cached * p.get("in_cached", p.get("in", 0))
                  + (usage.tokens_out or 0) * p.get("out", 0)) / 1_000_000, 6)


@dataclass
class CallRecord:
    call_id: str
    role: str
    system: str
    prompt: str
    output: str
    model: str | None
    usage: Usage
    latency_ms: float
    status: str


class InstrumentedChat:
    def __init__(self, fn: ChatFn, *, provider_id: str, budgets: list[Budget] | None = None,
                 prices: PriceTable | None = None, task: str = "", comp: str = "agentloop") -> None:
        self.fn = fn
        self.provider_id = provider_id
        self.budgets = budgets or []
        self.prices = prices
        self.task = task
        self.comp = comp
        self.records: list[CallRecord] = []

    async def __call__(self, *, role: str, system: str, prompt: str,
                       temperature: float | None = None, max_tokens: int | None = None) -> Reply:
        for b in self.budgets:
            try:
                b.charge()
            except BudgetExceeded as exc:
                obs.log(self.comp, "budget_block", "warn", task=self.task, kind=exc.kind,
                        remaining=exc.remaining)
                raise
        cid = ids.new_id()
        t0 = time.perf_counter()
        status = "ok"
        reply: Reply | None = None
        with ids.bind(call_id=cid):
            try:
                reply = await self.fn(system=system, prompt=prompt, temperature=temperature,
                                      max_tokens=max_tokens)
                return reply
            except TimeoutError:
                status = "timeout"
                raise
            except Exception:
                status = "error"
                raise
            finally:
                latency = round((time.perf_counter() - t0) * 1000, 1)
                usage = reply.usage if reply else Usage()
                model = reply.model if reply else None
                cost = estimate_cost(model, usage, self.prices)
                obs.log(self.comp, "provider_call", "info" if status == "ok" else "warn",
                        task=self.task, provider_id=self.provider_id, model=model, role=role,
                        tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                        tokens_cached=usage.tokens_cached, cost_est=cost, latency_ms=latency,
                        status=status, prompt_sha=_prefix_sha(system))
                obs.metrics.observe("latency_ms", latency, comp=self.comp, task=self.task, role=role)
                obs.metrics.inc("provider_call", task=self.task, role=role, status=status, model=model)
                if usage.tokens_in:
                    obs.metrics.observe("tokens_in", usage.tokens_in, task=self.task, model=model)
                if usage.tokens_cached is not None and usage.tokens_in:
                    obs.metrics.observe("cache_hit_pct", round(100 * usage.tokens_cached / usage.tokens_in),
                                        task=self.task, model=model)
                if cost is not None:
                    obs.metrics.inc("cost_est", cost, task=self.task, model=model)
                self.records.append(CallRecord(cid, role, system, prompt,
                                               reply.text if reply else "", model, usage, latency, status))


def _prefix_sha(system: str) -> str:
    import hashlib
    return hashlib.sha256(system[:2000].encode("utf-8")).hexdigest()[:8]


# ---------------- AstrBot 适配 ----------------

def _cached_from_raw(raw: Any) -> int | None:
    """DeepSeek 在 usage 顶层返回 prompt_cache_hit_tokens；AstrBot 4.29.4 只读 prompt_tokens_details.cached_tokens。"""
    usage = getattr(raw, "usage", None)
    if usage is None:
        return None
    for name in ("prompt_cache_hit_tokens",):
        value = getattr(usage, name, None)
        if value is None:
            extra = getattr(usage, "model_extra", None) or {}
            value = extra.get(name)
        if isinstance(value, int):
            return value
    return None


def usage_from_astrbot(resp: Any) -> Usage:
    """从 AstrBot LLMResponse 提取用量。缓存数优先取原始返回里的 DeepSeek 字段，其次 TokenUsage.input_cached。

    AstrBot 在 Provider 未返回缓存信息时把 input_cached 置 0，无法区分"没命中"和"不支持"，
    因此只有原始字段存在、或 input_cached > 0 时才填数值，否则记 None（日志规格 §4.2）。
    """
    tu = getattr(resp, "usage", None)
    tokens_in = getattr(tu, "input", None) if tu is not None else None
    tokens_out = getattr(tu, "output", None) if tu is not None else None
    cached = _cached_from_raw(getattr(resp, "raw_completion", None))
    if cached is None and tu is not None and getattr(tu, "input_cached", 0):
        cached = tu.input_cached
    return Usage(tokens_in=tokens_in, tokens_out=tokens_out, tokens_cached=cached)


def from_astrbot_provider(provider: Any, *, model: str | None = None) -> ChatFn:
    """把 AstrBot Provider（provider.text_chat）包成 ChatFn。contexts 固定为空：子 agent 不带人格与对话历史。"""

    async def call(*, system: str, prompt: str, temperature: float | None = None,
                   max_tokens: int | None = None) -> Reply:
        kwargs: dict[str, Any] = {"prompt": prompt, "system_prompt": system, "contexts": []}
        if model:
            kwargs["model"] = model
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        resp = await provider.text_chat(**kwargs)
        raw = getattr(resp, "raw_completion", None)
        return Reply(text=(getattr(resp, "completion_text", "") or ""),
                     usage=usage_from_astrbot(resp),
                     model=getattr(raw, "model", None) or model,
                     raw=resp)

    return call


ChatFactory = Callable[[], Awaitable[ChatFn]]
