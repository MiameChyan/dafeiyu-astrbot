"""生成 → 核查 → 修正 循环（记忆规格 §6.3/§7.2/§8，TRPG 规格 §13，日志规格 §5.1）。

一个 Task 描述：
- generate(ctx)：产出候选（可在内部多次调用 ctx.chat）；
- gates：按顺序执行的核查门，代码门与 LLM 门都是 async 函数 (candidate, ctx) -> GateResult；
- 失败的门把原因作为 feedback 交给下一轮 generate；
- 轮数用尽后：允许存疑 → accepted_with_doubt；有 fallback → fallback；否则 failed。

每次运行自动写 run_start / round_* / gate_result / run_end 日志，并按分层规则保存回放包。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from shared.observability import ids, obs

from .chat import Budget, BudgetExceeded, ChatFn, InstrumentedChat, PriceTable


@dataclass
class GateResult:
    gate: str
    passed: bool
    reason: str = ""
    fatal: bool = False  # True：不再重试，直接进入兜底/失败


@dataclass
class RoundContext:
    task: str
    round: int
    feedback: list[GateResult]
    inputs: dict[str, Any]
    _chat: InstrumentedChat

    async def chat(self, *, role: str, system: str, prompt: str, temperature: float | None = None,
                   max_tokens: int | None = None):
        return await self._chat(role=role, system=system, prompt=prompt,
                                temperature=temperature, max_tokens=max_tokens)


Gate = Callable[[Any, RoundContext], Awaitable[GateResult] | GateResult]


@dataclass
class Task:
    name: str
    generate: Callable[[RoundContext], Awaitable[Any]]
    gates: list[Gate]
    inputs: dict[str, Any] = field(default_factory=dict)       # 进回放包，必须可 JSON 序列化
    prompt_ver: dict[str, str] = field(default_factory=dict)   # {"gen": "extract@3+a1b2c3", ...}
    max_rounds: int = 3
    timeout_s: float = 60.0
    allow_doubt: bool = False
    fallback: Callable[[list[GateResult]], Any] | None = None
    actor: str | None = None
    scope: str | None = None
    comp: str = "agentloop"


@dataclass
class Outcome:
    status: str                    # accepted | accepted_with_doubt | fallback | failed | timeout
    output: Any
    run_id: str
    rounds: int
    calls: int
    gate_log: list[dict[str, Any]]
    tokens_total: int
    cost_est: float | None
    dur_ms: float
    error: str | None = None
    replay_path: str | None = None


async def _call_gate(gate: Gate, candidate: Any, ctx: RoundContext) -> GateResult:
    res = gate(candidate, ctx)
    if inspect.isawaitable(res):
        res = await res
    return res


async def run(task: Task, *, chat: ChatFn, provider_id: str = "default",
              budgets: list[Budget] | None = None, prices: PriceTable | None = None) -> Outcome:
    run_id = ids.new_id()
    ichat = InstrumentedChat(chat, provider_id=provider_id, budgets=budgets, prices=prices,
                             task=task.name, comp=task.comp)
    gate_log: list[dict[str, Any]] = []
    rounds_for_replay: list[dict[str, Any]] = []
    state = {"rounds": 0, "last": None, "feedback": []}
    t0 = time.perf_counter()

    async def body() -> tuple[str, Any]:
        feedback: list[GateResult] = []
        for rnd in range(1, task.max_rounds + 1):
            state["rounds"] = rnd
            with ids.bind(round=rnd):
                obs.log(task.comp, "round_start", task=task.name, round=rnd, role="gen")
                n_before = len(ichat.records)
                ctx = RoundContext(task=task.name, round=rnd, feedback=list(feedback),
                                   inputs=task.inputs, _chat=ichat)
                candidate = await task.generate(ctx)
                state["last"] = candidate
                failed: list[GateResult] = []
                fatal = False
                for gate in task.gates:
                    res = await _call_gate(gate, candidate, ctx)
                    entry = {"round": rnd, "gate": res.gate, "pass": res.passed, "reason": res.reason}
                    gate_log.append(entry)
                    obs.log(task.comp, "gate_result", "info" if res.passed else "info",
                            task=task.name, gate=res.gate, passed=res.passed, reason=res.reason or None)
                    obs.metrics.inc("gate_checked", task=task.name, gate=res.gate)
                    if not res.passed:
                        obs.metrics.inc("gate_reject", task=task.name, gate=res.gate)
                        failed.append(res)
                        if res.fatal:
                            fatal = True
                            break
                rounds_for_replay.append({
                    "round": rnd,
                    "calls": [_record_dict(r) for r in ichat.records[n_before:]],
                    "candidate": candidate,
                    "gates": [g for g in gate_log if g["round"] == rnd],
                })
                obs.log(task.comp, "round_end", task=task.name, round=rnd, passed=not failed)
                if not failed:
                    return "accepted", candidate
                feedback = failed
                state["feedback"] = failed
                if fatal:
                    break
        if task.allow_doubt and state["last"] is not None:
            return "accepted_with_doubt", state["last"]
        if task.fallback is not None:
            return "fallback", task.fallback(state["feedback"])
        return "failed", None

    status, output, error = "failed", None, None
    with ids.bind(run_id=run_id), obs.run_clock():
        obs.log(task.comp, "run_start", task=task.name, actor=task.actor, scope=task.scope,
                budget_rounds=task.max_rounds, prompt_ver=task.prompt_ver or None,
                astrbot_ver=obs.env.get("astrbot_ver"), plugin_ver=obs.env.get("plugin_ver"))
        try:
            status, output = await asyncio.wait_for(body(), timeout=task.timeout_s)
        except asyncio.TimeoutError:
            status, error = "timeout", f"timeout after {task.timeout_s}s"
            if task.fallback is not None:
                output = task.fallback(state["feedback"])
        except BudgetExceeded as exc:
            status, error = ("fallback" if task.fallback else "failed"), f"budget:{exc.kind}"
            if task.fallback is not None:
                output = task.fallback(state["feedback"])
        except Exception as exc:  # noqa: BLE001 - 生成或核查自身抛错
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            obs.log(task.comp, "run_error", "error", task=task.name, error=error)

        dur = round((time.perf_counter() - t0) * 1000, 1)
        tokens = sum((r.usage.tokens_in or 0) + (r.usage.tokens_out or 0) for r in ichat.records)
        costs = [c for c in (_cost(r, prices) for r in ichat.records) if c is not None]
        cost = round(sum(costs), 6) if costs else None
        obs.log(task.comp, "run_end", "info" if status == "accepted" else "warn",
                task=task.name, outcome=status, rounds=state["rounds"], calls=len(ichat.records),
                tokens_total=tokens, cost_est=cost, dur_ms=dur, error=error)
        obs.metrics.inc("run_outcome", task=task.name, outcome=status)
        obs.metrics.observe("rounds_per_run", state["rounds"], task=task.name)
        obs.metrics.observe("run_ms", dur, task=task.name)

        bundle = {
            "run_id": run_id, "task": task.name, "actor": task.actor, "scope": task.scope,
            "turn_id": ids.turn_id.get(),
            "prompt_ver": task.prompt_ver,
            "env": {**obs.env, "provider_id": provider_id},
            "inputs": task.inputs,
            "rounds": rounds_for_replay,
            "final": {"outcome": status, "output": output, "error": error},
            "expected": None,
        }
        path = obs.save_replay(bundle)

    return Outcome(status=status, output=output, run_id=run_id, rounds=state["rounds"],
                   calls=len(ichat.records), gate_log=gate_log, tokens_total=tokens, cost_est=cost,
                   dur_ms=dur, error=error, replay_path=str(path) if path else None)


def _record_dict(r) -> dict[str, Any]:
    return {"call_id": r.call_id, "role": r.role, "system": r.system, "prompt": r.prompt,
            "output": r.output, "model": r.model, "latency_ms": r.latency_ms, "status": r.status,
            "tokens": {"in": r.usage.tokens_in, "out": r.usage.tokens_out, "cached": r.usage.tokens_cached}}


def _cost(r, prices):
    from .chat import estimate_cost
    return estimate_cost(r.model, r.usage, prices)
