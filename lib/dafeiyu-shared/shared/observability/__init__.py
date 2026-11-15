"""日志与观测（规格：shared/docs/日志与观测规格.md）。

用法：
    from shared.observability import obs
    obs.configure(root=Path(".../obs"))
    obs.log("memory.extract", "candidate_rejected", gate="source_precedence", cand="…")
    with obs.span("memory.recall", "recall_done") as extra:
        ...; extra["k"] = 4
    obs.metrics.inc("gate_reject", gate="llm_support", task="extract")
    obs.audit("memory_write", target="admin-no-coriander", actor="qq:123")

未 configure 时所有调用都是空操作，便于单元测试与插件降级。
观测内部的任何异常都被吞掉并计数，绝不影响业务。
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import ids, replay
from .metrics import Metrics
from .sink import LEVELS, JsonlSink
from .store import ObsStore

_run_t0: ContextVar[float | None] = ContextVar("obs_run_t0", default=None)


def cfg_hash(cfg: dict[str, Any]) -> str:
    body = json.dumps(cfg, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


class Observability:
    def __init__(self) -> None:
        self.enabled = False
        self.root: Path | None = None
        self.level = LEVELS["info"]
        self.tz = ZoneInfo("Asia/Shanghai")
        self.sink: JsonlSink | None = None
        self.store: ObsStore | None = None
        self.metrics = Metrics(None, self.now)
        self.env: dict[str, Any] = {}
        self.replay_sample_rate = 0.1
        self.debug_payloads = False
        self.internal_errors = 0

    # ---- 生命周期 ----
    def configure(self, root: Path, *, level: str = "info", timezone: str = "Asia/Shanghai",
                  replay_sample_rate: float = 0.1, debug_payloads: bool = False,
                  env: dict[str, Any] | None = None, tick_every_s: float = 60.0) -> None:
        self.close()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.level = LEVELS[level]
        self.tz = ZoneInfo(timezone)
        self.store = ObsStore(self.root / "obs.db")
        self.metrics = Metrics(self.store, self.now)
        self.sink = JsonlSink(self.root / "logs", on_tick=self._tick, tick_every_s=tick_every_s)
        self.replay_sample_rate = replay_sample_rate
        self.debug_payloads = debug_payloads
        self.env = dict(env or {})
        self.enabled = True

    def close(self) -> None:
        if self.sink is not None:
            self.sink.close()
        if self.store is not None:
            try:
                self.metrics.flush()
            except Exception:  # noqa: BLE001
                self.internal_errors += 1
            self.store.close()
        self.sink = None
        self.store = None
        self.enabled = False

    def _tick(self) -> None:
        if self.sink is not None:
            for lvl, n in list(self.sink.dropped.items()):
                if n:
                    self.metrics.inc("obs_dropped", n, lvl=lvl)
                    self.sink.dropped[lvl] = 0
        self.metrics.flush()

    # ---- 时间 ----
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def now_iso(self) -> str:
        return self.now().isoformat(timespec="milliseconds")

    # ---- 日志 ----
    def log(self, comp: str, evt: str, lvl: str = "info", **fields: Any) -> None:
        if not self.enabled or LEVELS.get(lvl, 20) < self.level:
            return
        try:
            row: dict[str, Any] = {"ts": self.now_iso(), "lvl": lvl, "comp": comp, "evt": evt}
            row.update(ids.current())
            row.update({k: v for k, v in fields.items() if v is not None})
            assert self.sink is not None
            self.sink.put(row)
        except Exception:  # noqa: BLE001
            self.internal_errors += 1

    @contextmanager
    def span(self, comp: str, evt: str, lvl: str = "info", **fields: Any) -> Iterator[dict[str, Any]]:
        """计时块：结束时写一行带 dur_ms 的日志；块内可往 yield 出的 dict 追加字段。异常时记 error 并继续抛出。"""
        extra: dict[str, Any] = {}
        t0 = time.perf_counter()
        try:
            yield extra
        except BaseException as exc:
            self.log(comp, evt, "error", dur_ms=round((time.perf_counter() - t0) * 1000, 1),
                     status="error", error=f"{type(exc).__name__}: {exc}", **fields, **extra)
            raise
        else:
            self.log(comp, evt, lvl, dur_ms=round((time.perf_counter() - t0) * 1000, 1),
                     **fields, **extra)

    @contextmanager
    def run_clock(self) -> Iterator[None]:
        token = _run_t0.set(time.perf_counter())
        try:
            yield
        finally:
            _run_t0.reset(token)

    def stage(self, comp: str, name: str, **fields: Any) -> None:
        """分段时间戳：相对当前 run 起点的毫秒数（日志规格 §5.1 stage）。"""
        t0 = _run_t0.get()
        rel = round((time.perf_counter() - t0) * 1000, 1) if t0 is not None else None
        self.log(comp, "stage", name=name, t_rel_ms=rel, **fields)

    # ---- 审计 ----
    def audit(self, action: str, *, target: str | None = None, actor: str | None = None,
              **detail: Any) -> None:
        if not self.enabled or self.store is None:
            return
        try:
            cur = ids.current()
            self.store.add_audit(id=ids.new_id(), ts=self.now_iso(), action=action, actor=actor,
                                 target=target, turn_id=cur.get("turn_id"), run_id=cur.get("run_id"),
                                 detail=detail or None)
        except Exception:  # noqa: BLE001
            self.internal_errors += 1

    # ---- 配置快照 ----
    def snapshot_config(self, cfg: dict[str, Any]) -> str:
        full = {"cfg": cfg, "env": self.env}
        h = cfg_hash(full)
        if self.enabled and self.store is not None:
            try:
                self.store.execute("insert or ignore into config_snapshots(cfg_hash, ts, body) values (?,?,?)",
                                   (h, self.now_iso(), json.dumps(full, ensure_ascii=False, default=str)))
            except Exception:  # noqa: BLE001
                self.internal_errors += 1
        return h

    # ---- 回放 ----
    def save_replay(self, bundle: dict[str, Any], *, kp_rejected: bool = False) -> Path | None:
        if not self.enabled or self.root is None:
            return None
        try:
            if not replay.should_save(bundle.get("final", {}).get("outcome", ""),
                                      sample_rate=self.replay_sample_rate,
                                      debug_payloads=self.debug_payloads, kp_rejected=kp_rejected):
                return None
            return replay.save(self.root / "replays", bundle)
        except Exception:  # noqa: BLE001
            self.internal_errors += 1
            return None


obs = Observability()

__all__ = ["obs", "Observability", "ids", "replay", "cfg_hash"]
