import asyncio
import json
from types import SimpleNamespace

import pytest

from shared.agentloop import (Budget, GateResult, Reply, Task, Usage, estimate_cost,
                              from_astrbot_provider, run, usage_from_astrbot)
from shared.observability import Observability, ids, replay
import shared.observability as obs_mod
import shared.agentloop.chat as chat_mod
import shared.agentloop.loop as loop_mod


@pytest.fixture
def ob(tmp_path, monkeypatch):
    o = Observability()
    o.configure(tmp_path / "obs", env={"astrbot_ver": "4.29.4", "plugin_ver": "0.0.1"},
                replay_sample_rate=0.0)
    # agentloop 模块各自 import 了全局 obs；测试中替换为隔离实例
    monkeypatch.setattr(chat_mod, "obs", o)
    monkeypatch.setattr(loop_mod, "obs", o)
    yield o
    o.close()


def logs(o):
    o.close()
    rows = []
    for f in (o.root / "logs").rglob("*.jsonl"):
        rows += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x]
    return rows


def scripted_chat(outputs, *, model="deepseek-chat", cached=1000, delay=0.0):
    """按顺序返回预设输出的假模型。"""
    seq = list(outputs)
    calls = []

    async def chat(*, system, prompt, temperature=None, max_tokens=None):
        calls.append({"system": system, "prompt": prompt})
        if delay:
            await asyncio.sleep(delay)
        text = seq.pop(0) if seq else ""
        return Reply(text=text, usage=Usage(tokens_in=1500, tokens_out=50, tokens_cached=cached), model=model)

    chat.calls = calls
    return chat


EVIDENCE = [{"ep": "ep#01", "speaker": "qq:10001", "text": "我刚吃完晚饭"}]


def dinner_task(**overrides):
    """晚饭事故：生成器第一轮写反事实，一致性门必须拦住；第二轮按反馈改正。"""

    async def generate(ctx):
        fb = "；".join(g.reason for g in ctx.feedback)
        r = await ctx.chat(role="gen", system="你是记忆抽取器", prompt=f"证据：{EVIDENCE}\n上轮问题：{fb}")
        return {"description": r.text, "evidence": ["ep#01"]}

    def code_evidence(c, ctx):
        ok = set(c["evidence"]) <= {e["ep"] for e in EVIDENCE}
        return GateResult("code_evidence", ok, "" if ok else "引用了不存在的事件")

    async def llm_consistency(c, ctx):
        r = await ctx.chat(role="critic", system="你是核查器", prompt=f"候选：{c['description']}")
        ok = r.text.startswith("PASS")
        return GateResult("llm_consistency", ok, "" if ok else r.text)

    kw = dict(name="extract", generate=generate, gates=[code_evidence, llm_consistency],
              inputs={"evidence": EVIDENCE}, prompt_ver={"gen": "extract@1+aaaaaa"},
              max_rounds=3, timeout_s=5, actor="qq:10001", scope="real/admin")
    kw.update(overrides)
    return Task(**kw)


async def test_gate_rejects_then_feedback_fixes(ob):
    chat = scripted_chat(["他一直没吃晚饭", "FAIL: 与证据「我刚吃完晚饭」矛盾",
                          "他已经吃过晚饭", "PASS"])
    out = await run(dinner_task(), chat=chat, provider_id="ds",
                    prices={"deepseek-chat": {"in": 2.0, "in_cached": 0.5, "out": 8.0}})
    assert out.status == "accepted"
    assert out.output["description"] == "他已经吃过晚饭"
    assert out.rounds == 2 and out.calls == 4
    assert "与证据" in chat.calls[2]["prompt"]  # 第二轮生成拿到了反馈
    assert [g["pass"] for g in out.gate_log] == [True, False, True, True]
    assert out.cost_est is not None and out.cost_est > 0
    rows = logs(ob)
    evts = [r["evt"] for r in rows]
    assert evts[0] == "run_start" and evts[-1] == "run_end"
    calls = [r for r in rows if r["evt"] == "provider_call"]
    assert len(calls) == 4 and all(r["run_id"] == out.run_id for r in calls)
    assert {r["role"] for r in calls} == {"gen", "critic"}
    assert calls[0]["tokens_cached"] == 1000
    assert all("call_id" in r for r in calls)
    end = rows[-1]
    assert end["outcome"] == "accepted" and end["rounds"] == 2


async def test_exhausted_rounds_use_fallback_and_save_replay(ob):
    chat = scripted_chat(["他一直没吃", "FAIL: 矛盾"] * 3)
    out = await run(dinner_task(fallback=lambda fb: None), chat=chat)
    assert out.status == "fallback" and out.output is None and out.rounds == 3
    assert out.replay_path  # 非 accepted 一律保存
    bundle = replay.load(out.replay_path)
    assert bundle["final"]["outcome"] == "fallback"
    assert bundle["inputs"]["evidence"][0]["text"] == "我刚吃完晚饭"   # 原文保留
    assert bundle["actor"] == "qq:10001"
    assert len(bundle["rounds"]) == 3 and bundle["rounds"][0]["calls"][0]["role"] == "gen"
    assert bundle["env"]["astrbot_ver"] == "4.29.4"


async def test_allow_doubt_returns_last_candidate(ob):
    chat = scripted_chat(["草稿", "FAIL: x"] * 2)
    out = await run(dinner_task(max_rounds=2, allow_doubt=True), chat=chat)
    assert out.status == "accepted_with_doubt" and out.output["description"] == "草稿"


async def test_fatal_gate_stops_retrying(ob):
    async def generate(ctx):
        return {"x": 1}
    calls = {"n": 0}

    def fatal_gate(c, ctx):
        calls["n"] += 1
        return GateResult("code_type", False, "行为自述", fatal=True)

    out = await run(Task(name="reflect", generate=generate, gates=[fatal_gate], max_rounds=3),
                    chat=scripted_chat([]))
    assert out.status == "failed" and out.rounds == 1 and calls["n"] == 1


async def test_timeout_outcome(ob):
    chat = scripted_chat(["慢"], delay=1.0)
    out = await run(dinner_task(timeout_s=0.1, fallback=lambda fb: "兜底"), chat=chat)
    assert out.status == "timeout" and out.output == "兜底"
    assert any(r["evt"] == "run_end" and r["outcome"] == "timeout" for r in logs(ob))


async def test_budget_block(ob):
    chat = scripted_chat(["a", "FAIL", "b", "FAIL"])
    budget = Budget(max_calls=3, kind="session")
    out = await run(dinner_task(fallback=lambda fb: "兜底"), chat=chat, budgets=[budget])
    assert out.status == "fallback" and out.error == "budget:session"
    assert budget.used == 3
    assert any(r["evt"] == "budget_block" and r["kind"] == "session" for r in logs(ob))


async def test_generate_exception_is_failed_not_raised(ob):
    async def generate(ctx):
        raise ValueError("JSON 解析失败")
    out = await run(Task(name="extract", generate=generate, gates=[]), chat=scripted_chat([]))
    assert out.status == "failed" and "JSON" in out.error


async def test_turn_id_flows_into_run_logs(ob):
    ids.begin_turn()
    tid = ids.turn_id.get()
    out = await run(dinner_task(), chat=scripted_chat(["他吃过了", "PASS"]))
    assert out.status == "accepted"
    rows = logs(ob)
    assert rows and all(r.get("turn_id") == tid for r in rows)


async def test_metrics_recorded(ob):
    await run(dinner_task(), chat=scripted_chat(["他一直没吃", "FAIL: 矛盾", "他吃过了", "PASS"]))
    ob.metrics.flush()
    names = {r["name"] for r in ob.store.execute("select name from metrics")}
    assert {"gate_reject", "gate_checked", "run_outcome", "rounds_per_run", "provider_call",
            "latency_ms", "cache_hit_pct"} <= names


# ---------------- AstrBot 适配 ----------------

def fake_resp(text, *, input_other=1200, input_cached=0, output=40, raw_hit=None, raw_model="deepseek-chat"):
    usage = SimpleNamespace(input_other=input_other, input_cached=input_cached, output=output,
                            input=input_other + input_cached)
    raw_usage = SimpleNamespace(model_extra={} if raw_hit is None else {"prompt_cache_hit_tokens": raw_hit})
    raw = SimpleNamespace(model=raw_model, usage=raw_usage)
    return SimpleNamespace(completion_text=text, usage=usage, raw_completion=raw)


def test_usage_prefers_deepseek_raw_field():
    u = usage_from_astrbot(fake_resp("x", input_other=1500, input_cached=0, raw_hit=900))
    assert u.tokens_cached == 900 and u.tokens_in == 1500 and u.tokens_out == 40


def test_usage_cached_null_when_unknown():
    u = usage_from_astrbot(fake_resp("x", input_cached=0, raw_hit=None))
    assert u.tokens_cached is None  # 不支持缓存信息时不记 0


def test_usage_falls_back_to_astrbot_input_cached():
    u = usage_from_astrbot(fake_resp("x", input_other=1000, input_cached=500, raw_hit=None))
    assert u.tokens_cached == 500


async def test_from_astrbot_provider_passes_isolated_context():
    seen = {}

    class FakeProvider:
        async def text_chat(self, **kw):
            seen.update(kw)
            return fake_resp("好的", raw_hit=100)

    chat = from_astrbot_provider(FakeProvider())
    r = await chat(system="S", prompt="P", temperature=0.1, max_tokens=64)
    assert r.text == "好的" and r.model == "deepseek-chat" and r.usage.tokens_cached == 100
    assert seen["contexts"] == [] and seen["system_prompt"] == "S" and seen["max_tokens"] == 64


def test_estimate_cost():
    u = Usage(tokens_in=1_000_000, tokens_out=1_000_000, tokens_cached=500_000)
    prices = {"m": {"in": 2.0, "in_cached": 0.5, "out": 8.0}}
    assert estimate_cost("m", u, prices) == pytest.approx((500_000 * 2 + 500_000 * 0.5 + 1_000_000 * 8) / 1e6)
    assert estimate_cost("unknown", u, prices) is None
