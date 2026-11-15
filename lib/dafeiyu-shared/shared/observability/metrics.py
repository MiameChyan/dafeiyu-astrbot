"""进程内预聚合指标，按分钟桶刷入 obs.db（日志规格 §6、§11）。"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from .store import ObsStore

# 直方图桶上界（毫秒 / token / 任意数值通用），最后一个桶为 +inf
HIST_BOUNDS: tuple[float, ...] = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)


def _labels_key(labels: dict[str, Any]) -> str:
    return json.dumps({k: labels[k] for k in sorted(labels) if labels[k] is not None},
                      ensure_ascii=False, separators=(",", ":"))


class Metrics:
    def __init__(self, store: ObsStore | None, now: Callable[[], datetime]) -> None:
        self.store = store
        self._now = now
        self._lock = threading.Lock()
        # key: (ts_min, name, labels, kind) -> [count, sum, buckets|None]
        self._agg: dict[tuple[str, str, str, str], list] = {}

    def _bucket(self) -> str:
        return self._now().strftime("%Y-%m-%dT%H:%M")

    def inc(self, name: str, value: float = 1, **labels: Any) -> None:
        key = (self._bucket(), name, _labels_key(labels), "counter")
        with self._lock:
            row = self._agg.setdefault(key, [0, 0.0, None])
            row[0] += 1
            row[1] += value

    def observe(self, name: str, value: float | None, **labels: Any) -> None:
        if value is None:
            return  # null 语义：缺失值不计入（日志规格 §4.2）
        key = (self._bucket(), name, _labels_key(labels), "hist")
        with self._lock:
            row = self._agg.setdefault(key, [0, 0.0, [0] * (len(HIST_BOUNDS) + 1)])
            row[0] += 1
            row[1] += value
            idx = next((i for i, b in enumerate(HIST_BOUNDS) if value <= b), len(HIST_BOUNDS))
            row[2][idx] += 1

    def snapshot(self) -> dict[tuple[str, str, str, str], list]:
        with self._lock:
            return {k: [v[0], v[1], list(v[2]) if v[2] else None] for k, v in self._agg.items()}

    def flush(self) -> int:
        """把累计值加到 obs.db（同键累加），清空内存。返回写入行数。"""
        with self._lock:
            items = list(self._agg.items())
            self._agg.clear()
        if not items or self.store is None:
            return 0
        for (ts_min, name, labels, kind), (count, total, buckets) in items:
            existing = self.store.execute(
                "select count, sum, buckets from metrics where ts_min=? and name=? and labels=?",
                (ts_min, name, labels))
            if existing:
                old = existing[0]
                merged_buckets = None
                if buckets is not None:
                    prev = json.loads(old["buckets"]) if old["buckets"] else [0] * len(buckets)
                    merged_buckets = [a + b for a, b in zip(prev, buckets)]
                self.store.execute(
                    "update metrics set count=?, sum=?, buckets=? where ts_min=? and name=? and labels=?",
                    (old["count"] + count, old["sum"] + total,
                     json.dumps(merged_buckets) if merged_buckets is not None else None,
                     ts_min, name, labels))
            else:
                self.store.execute(
                    "insert into metrics(ts_min, name, labels, kind, count, sum, buckets) values (?,?,?,?,?,?,?)",
                    (ts_min, name, labels, kind, count, total,
                     json.dumps(buckets) if buckets is not None else None))
        return len(items)
