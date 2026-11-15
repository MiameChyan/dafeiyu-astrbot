import asyncio
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shared.observability import Observability, cfg_hash, ids, replay
from shared.observability.prompts import PromptVersionError, load_prompt, render
from shared.observability.sink import JsonlSink
from shared.observability.store import ObsStore


@pytest.fixture
def ob(tmp_path):
    o = Observability()
    o.configure(tmp_path / "obs", level="info", env={"astrbot_ver": "4.29.4", "plugin_ver": "0.0.1"})
    yield o
    o.close()


def read_logs(root: Path) -> list[dict]:
    rows = []
    for f in sorted((root / "logs").rglob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line]
    return rows


# ---------------- 日志 ----------------

def test_log_writes_jsonl_with_ids_and_partitions_by_component_and_day(ob):
    with ids.bind(turn_id="T1", run_id="R1", round=2):
        ob.log("memory.extract", "candidate_rejected", gate="source_precedence", actor="qq:123")
    ob.log("trpg.stage", "round_sent", bubbles=3)
    ob.close()
    rows = read_logs(ob.root)
    assert len(rows) == 2
    first = next(r for r in rows if r["evt"] == "candidate_rejected")
    assert first["turn_id"] == "T1" and first["run_id"] == "R1" and first["round"] == 2
    assert first["actor"] == "qq:123"  # 不脱敏：保留真实账号
    assert first["ts"].endswith("+08:00")
    day = first["ts"][:10]
    assert (ob.root / "logs" / "memory" / f"{day}.jsonl").exists()
    assert (ob.root / "logs" / "trpg" / f"{day}.jsonl").exists()


def test_level_filter_and_none_fields_dropped(ob):
    ob.log("memory.recall", "debug_detail", "debug", x=1)
    ob.log("memory.recall", "recall_done", k=4, degraded=None)
    ob.close()
    rows = read_logs(ob.root)
    assert [r["evt"] for r in rows] == ["recall_done"]
    assert "degraded" not in rows[0]


def test_log_is_noop_when_not_configured():
    o = Observability()
    o.log("x", "y")  # 不抛错
    o.audit("a")
    assert o.save_replay({"task": "t", "run_id": "r", "final": {"outcome": "failed"}}) is None


def test_span_records_duration_and_error(ob):
    with ob.span("memory.recall", "recall_done") as extra:
        extra["k"] = 4
    with pytest.raises(ValueError):
        with ob.span("memory.recall", "recall_done"):
            raise ValueError("boom")
    ob.close()
    rows = read_logs(ob.root)
    ok, err = rows
    assert ok["k"] == 4 and ok["dur_ms"] >= 0 and ok["lvl"] == "info"
    assert err["lvl"] == "error" and "boom" in err["error"]


def test_stage_relative_timestamps(ob):
    with ob.run_clock():
        ob.stage("trpg.stage", "t_wake")
        ob.stage("trpg.stage", "t_first_sent")
    ob.close()
    rows = read_logs(ob.root)
    assert [r["name"] for r in rows] == ["t_wake", "t_first_sent"]
    assert rows[1]["t_rel_ms"] >= rows[0]["t_rel_ms"] >= 0


def test_sink_backpressure_drops_debug_first(tmp_path):
    sink = JsonlSink(tmp_path, max_queue=10, interval_s=60)  # 写线程几乎不消费
    # 让写线程阻塞在 get(timeout=60) 之外：直接灌满
    accepted = [sink.put({"lvl": "info", "comp": "x", "evt": str(i), "ts": "2026-11-15"}) for i in range(10)]
    debug_ok = sink.put({"lvl": "debug", "comp": "x", "evt": "d", "ts": "2026-11-15"})
    assert sum(accepted) >= 8
    assert debug_ok is False and sink.dropped["debug"] == 1
    sink.close()


# ---------------- ID 链 ----------------

class FakeEvent:
    def __init__(self):
        self.extra = {}

    def set_extra(self, k, v):
        self.extra[k] = v

    def get_extra(self, k, default=None):
        return self.extra.get(k, default)


async def test_turn_id_propagates_into_created_tasks_but_not_across_events():
    async def handle_event(ev):
        tid = ids.begin_turn(ev)

        async def tool_call():  # 模拟 AstrBot 工具经 create_task 执行
            await asyncio.sleep(0)
            return ids.turn_id.get()

        inner = await asyncio.create_task(tool_call())
        return tid, inner

    (t1, i1), (t2, i2) = await asyncio.gather(handle_event(FakeEvent()), handle_event(FakeEvent()))
    assert t1 == i1 and t2 == i2 and t1 != t2


def test_begin_turn_reuses_event_turn_id_and_ensure_turn_falls_back():
    ev = FakeEvent()
    t1 = ids.begin_turn(ev)
    assert ids.begin_turn(ev) == t1
    token = ids.turn_id.set(None)
    try:
        assert ids.ensure_turn(ev) == t1  # 上下文丢失时从 event 兜底
    finally:
        ids.turn_id.reset(token)


# ---------------- 指标 ----------------

def test_metrics_counter_and_hist_flush_accumulate(ob):
    ob.metrics.inc("gate_reject", gate="llm_support", task="extract")
    ob.metrics.inc("gate_reject", gate="llm_support", task="extract")
    for v in (3, 40, 700, 99999):
        ob.metrics.observe("latency_ms", v, task="extract")
    ob.metrics.observe("cache_hit_ratio", None, model="x")  # null 不计
    assert ob.metrics.flush() == 2
    ob.metrics.inc("gate_reject", gate="llm_support", task="extract")
    ob.metrics.flush()
    rows = {r["name"]: r for r in ob.store.execute("select * from metrics")}
    assert rows["gate_reject"]["count"] == 3
    hist = rows["latency_ms"]
    assert hist["count"] == 4 and json.loads(hist["buckets"])[-1] == 1  # 99999 落入 +inf 桶
    assert "cache_hit_ratio" not in rows


# ---------------- 审计 / 配置 ----------------

def test_audit_and_config_snapshot(ob):
    with ids.bind(turn_id="T9"):
        ob.audit("memory_write", target="admin-no-coriander", actor="qq:123", op="ADD")
    h1 = ob.snapshot_config({"index_max_lines": 50})
    h2 = ob.snapshot_config({"index_max_lines": 50})
    h3 = ob.snapshot_config({"index_max_lines": 60})
    assert h1 == h2 != h3
    row = ob.store.execute("select * from audit")[0]
    assert row["turn_id"] == "T9" and row["actor"] == "qq:123" and json.loads(row["detail"]) == {"op": "ADD"}
    snap = json.loads(ob.store.execute("select body from config_snapshots where cfg_hash=?", (h1,))[0]["body"])
    assert snap["env"]["astrbot_ver"] == "4.29.4"


def test_cfg_hash_is_order_independent():
    assert cfg_hash({"a": 1, "b": 2}) == cfg_hash({"b": 2, "a": 1})


# ---------------- 提示词版本 ----------------

def test_prompt_version_hash_and_stale_label_detection(tmp_path, ob):
    p = tmp_path / "extract.md"
    p.write_text("# prompt: extract@3\n从事件中抽取事实：{{events}}\n", encoding="utf-8")
    v1 = load_prompt(p, store=ob.store, now_iso="t")
    assert v1.ver.startswith("extract@3+") and not v1.stale_label
    assert render(v1, events="E1") == "从事件中抽取事实：E1\n"
    # 内容改了、标签没升
    p.write_text("# prompt: extract@3\n新的写法：{{events}}\n", encoding="utf-8")
    v2 = load_prompt(p, store=ob.store, now_iso="t")
    assert v2.sha != v1.sha and v2.stale_label
    # strict 模式：再换一次内容
    p.write_text("# prompt: extract@3\n第三种写法：{{events}}\n", encoding="utf-8")
    with pytest.raises(PromptVersionError):
        load_prompt(p, store=ob.store, now_iso="t", strict=True)


def test_prompt_crlf_normalized_and_header_required(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_bytes("# prompt: x@1\n正文\n".encode())
    b.write_bytes("# prompt: x@1\r\n正文\r\n".encode())
    assert load_prompt(a).sha == load_prompt(b).sha  # Windows 换行不改变版本
    bad = tmp_path / "bad.md"
    bad.write_text("没有声明\n", encoding="utf-8")
    with pytest.raises(PromptVersionError):
        load_prompt(bad)


def test_render_rejects_missing_placeholders(tmp_path):
    p = tmp_path / "p.md"
    p.write_text("# prompt: p@1\n{{a}} {{b}}\n", encoding="utf-8")
    with pytest.raises(PromptVersionError):
        render(load_prompt(p), a=1)


# ---------------- 回放 ----------------

def test_replay_should_save_layering():
    rng = random.Random(0)
    for outcome in ("failed", "timeout", "fallback", "accepted_with_doubt"):
        assert replay.should_save(outcome, sample_rate=0.0, rng=rng)
    assert replay.should_save("accepted", sample_rate=0.0, kp_rejected=True, rng=rng)
    assert not replay.should_save("accepted", sample_rate=0.0, rng=rng)
    assert replay.should_save("accepted", sample_rate=1.0, rng=rng)


def test_replay_roundtrip_keeps_raw_text_and_account(ob):
    bundle = {"run_id": ids.new_id(), "task": "reflect", "actor": "qq:123456",
              "inputs": {"evidence": [{"ep": "01J", "speaker": "qq:123456", "text": "我吃完晚饭了"}]},
              "final": {"outcome": "fallback"}}
    path = ob.save_replay(bundle)
    assert path is not None and path.suffix == ".zst"
    back = replay.load(path)
    assert back["inputs"]["evidence"][0]["text"] == "我吃完晚饭了"
    assert back["actor"] == "qq:123456"
    listed = list(replay.iter_bundles(ob.root / "replays", task="reflect"))
    assert len(listed) == 1


def test_replay_purge_skips_golden(tmp_path):
    root = tmp_path / "replays"
    old = replay.save(root, {"task": "reflect", "run_id": "old", "final": {}})
    gold = replay.save(root, {"task": "golden", "run_id": "g", "final": {}})
    import os
    past = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(old, (past, past))
    os.utime(gold, (past, past))
    assert replay.purge_expired(root, 14) == 1
    assert not old.exists() and gold.exists()


def test_store_readonly_rejects_writes(tmp_path):
    rw = ObsStore(tmp_path / "obs.db")
    rw.close()
    ro = ObsStore(tmp_path / "obs.db", readonly=True)
    with pytest.raises(Exception):
        ro.execute("insert into audit(id, ts, action) values ('1','t','x')")
    ro.close()
