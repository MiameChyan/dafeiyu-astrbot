"""回放包（日志规格 §8）：保存、加载、分层抽样。

回放包保存原文与真实账号 ID，不脱敏（日志规格 §9）。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import zstandard

ALWAYS_SAVE = {"failed", "timeout", "fallback", "accepted_with_doubt"}


def should_save(outcome: str, *, sample_rate: float, debug_payloads: bool = False,
                kp_rejected: bool = False, rng: random.Random | None = None) -> bool:
    if debug_payloads or kp_rejected or outcome in ALWAYS_SAVE:
        return True
    return (rng or random).random() < sample_rate


def path_for(root: Path, task: str, run_id: str) -> Path:
    return Path(root) / task / f"{run_id}.json.zst"


def save(root: Path, bundle: dict[str, Any]) -> Path:
    path = path_for(root, bundle["task"], bundle["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(bundle, ensure_ascii=False, default=str).encode("utf-8")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(zstandard.ZstdCompressor(level=6).compress(raw))
    tmp.replace(path)
    return path


def load(path: Path) -> dict[str, Any]:
    data = zstandard.ZstdDecompressor().decompress(Path(path).read_bytes())
    return json.loads(data.decode("utf-8"))


def iter_bundles(root: Path, *, task: str | None = None,
                 since: datetime | None = None) -> Iterator[tuple[Path, dict[str, Any]]]:
    base = Path(root)
    dirs = [base / task] if task else [p for p in base.iterdir() if p.is_dir()] if base.exists() else []
    for d in dirs:
        if not d.exists() or d.name in {"golden", "reruns"}:
            continue
        for path in sorted(d.glob("*.json.zst")):
            if since is not None and datetime.fromtimestamp(path.stat().st_mtime) < since:
                continue
            yield path, load(path)


def purge_expired(root: Path, keep_days: int, *, now: datetime | None = None) -> int:
    """删除过期回放包；golden（回归集）与 reruns 不在此清理。"""
    cutoff = (now or datetime.now()) - timedelta(days=keep_days)
    removed = 0
    base = Path(root)
    if not base.exists():
        return 0
    for d in base.iterdir():
        if not d.is_dir() or d.name in {"golden", "reruns"}:
            continue
        for path in d.glob("*.json.zst"):
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
    return removed
