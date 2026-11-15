"""结构化 JSONL 日志写入（日志规格 §2、§4、§11）。

- 调用方只做入队（线程安全、不阻塞事件循环）；后台线程批量落盘。
- 文件按 `<obs_dir>/logs/<组件根>/<YYYY-MM-DD>.jsonl` 组织，日期按配置时区切分。
- 背压：队列 ≥80% 时丢弃 debug；队列满时丢弃 info；warn/error 最多等待 1 秒。
  被丢弃的条数累计在 `dropped`，由上层作为 obs_dropped 指标上报。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


class JsonlSink:
    def __init__(self, root: Path, *, max_queue: int = 10000, batch: int = 200,
                 interval_s: float = 0.5, on_tick: Callable[[], None] | None = None,
                 tick_every_s: float = 60.0) -> None:
        self.root = Path(root)
        self.max_queue = max_queue
        self.batch = batch
        self.interval_s = interval_s
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_queue)
        self.dropped = defaultdict(int)  # 按级别计数
        self._on_tick = on_tick
        self._tick_every_s = tick_every_s
        self._last_tick = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="obs-sink", daemon=True)
        self._stopped = threading.Event()
        self._thread.start()

    # ---- 入队 ----
    def put(self, row: dict[str, Any]) -> bool:
        lvl = row.get("lvl", "info")
        size = self._q.qsize()
        if lvl == "debug" and size >= self.max_queue * 0.8:
            self.dropped["debug"] += 1
            return False
        try:
            if LEVELS.get(lvl, 20) >= LEVELS["warn"]:
                self._q.put(row, timeout=1.0)
            else:
                self._q.put_nowait(row)
            return True
        except queue.Full:
            self.dropped[lvl] += 1
            return False

    # ---- 后台写入 ----
    def _run(self) -> None:
        pending: list[dict[str, Any]] = []
        while True:
            try:
                item = self._q.get(timeout=self.interval_s)
                if item is None:
                    self._flush(pending)
                    pending.clear()
                    break
                pending.append(item)
                while len(pending) < self.batch:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        self._flush(pending)
                        pending.clear()
                        self._stopped.set()
                        return
                    pending.append(nxt)
            except queue.Empty:
                pass
            if pending:
                self._flush(pending)
                pending.clear()
            self._maybe_tick()
        self._maybe_tick(force=True)
        self._stopped.set()

    def _maybe_tick(self, force: bool = False) -> None:
        if self._on_tick is None:
            return
        now = time.monotonic()
        if force or now - self._last_tick >= self._tick_every_s:
            self._last_tick = now
            try:
                self._on_tick()
            except Exception:  # noqa: BLE001 - 观测故障不得外溢
                pass

    def _flush(self, rows: list[dict[str, Any]]) -> None:
        groups: dict[Path, list[str]] = defaultdict(list)
        for row in rows:
            comp_root = str(row.get("comp", "misc")).split(".", 1)[0] or "misc"
            day = str(row.get("ts", ""))[:10] or "unknown"
            groups[self.root / comp_root / f"{day}.jsonl"].append(
                json.dumps(row, ensure_ascii=False, default=str))
        for path, lines in groups.items():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write("\n".join(lines) + "\n")
            except OSError:
                self.dropped["io_error"] += len(lines)

    def close(self, timeout: float = 5.0) -> None:
        if self._thread.is_alive():
            try:
                self._q.put(None, timeout=timeout)
            except queue.Full:
                pass
            self._thread.join(timeout=timeout)
