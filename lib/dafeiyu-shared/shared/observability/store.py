"""obs.db：指标、审计、配置快照、提示词版本（日志规格 §2、§7）。

只有插件进程写入（单写者）；重跑工具以只读方式打开。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
pragma journal_mode = wal;
create table if not exists metrics (
    ts_min  text not null,          -- 分钟桶，Asia/Shanghai，'2026-11-15T21:03'
    name    text not null,
    labels  text not null,          -- 排序后的 JSON
    kind    text not null,          -- counter | hist
    count   integer not null default 0,
    sum     real not null default 0,
    buckets text,                   -- hist：JSON 数组，与 HIST_BOUNDS 对齐
    primary key (ts_min, name, labels)
);
create table if not exists audit (
    id      text primary key,       -- ULID
    ts      text not null,
    actor   text,
    action  text not null,
    target  text,
    turn_id text,
    run_id  text,
    detail  text
);
create index if not exists audit_ts on audit(ts);
create table if not exists config_snapshots (
    cfg_hash text primary key,
    ts       text not null,
    body     text not null
);
create table if not exists prompt_versions (
    name       text not null,
    tag        text not null,
    sha        text not null,
    first_seen text not null,
    primary key (name, tag, sha)
);
"""


class ObsStore:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self._lock = threading.Lock()
        if readonly:
            self._db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True,
                                       check_same_thread=False, timeout=5)
            self._db.execute("pragma query_only = on")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            self._db.executescript(SCHEMA)
        self._db.row_factory = sqlite3.Row

    def execute(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._db.execute(sql, params)
            rows = cur.fetchall()
            if not self.readonly:
                self._db.commit()
            return rows

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        if self.readonly:
            raise PermissionError("obs.db opened read-only")
        with self._lock:
            self._db.executemany(sql, seq)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- 便捷写入 ----
    def add_audit(self, *, id: str, ts: str, action: str, actor: str | None, target: str | None,
                  turn_id: str | None, run_id: str | None, detail: dict[str, Any] | None) -> None:
        self.execute(
            "insert into audit(id, ts, actor, action, target, turn_id, run_id, detail) values (?,?,?,?,?,?,?,?)",
            (id, ts, actor, action, target, turn_id, run_id,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None),
        )
