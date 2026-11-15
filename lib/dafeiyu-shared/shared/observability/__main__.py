"""回放包查看工具（只读）。

    python -m shared.observability list  <obs_dir> [--task extract] [--outcome fallback]
    python -m shared.observability show  <obs_dir> <run_id>

离线重跑（日志规格 §8.2）在记忆系统 M3 前补齐：需要按 task 注册生成器与核查门。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import replay


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m shared.observability")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("obs_dir", type=Path)
    p_list.add_argument("--task")
    p_list.add_argument("--outcome")
    p_show = sub.add_parser("show")
    p_show.add_argument("obs_dir", type=Path)
    p_show.add_argument("run_id")
    args = ap.parse_args(argv)
    root = args.obs_dir / "replays"

    if args.cmd == "list":
        n = 0
        for path, b in replay.iter_bundles(root, task=args.task):
            outcome = b.get("final", {}).get("outcome")
            if args.outcome and outcome != args.outcome:
                continue
            rounds = len(b.get("rounds", []))
            print(f"{b['run_id']}  {b['task']:<16} {outcome:<20} rounds={rounds}  actor={b.get('actor')}")
            n += 1
        print(f"共 {n} 条", file=sys.stderr)
        return 0

    matches = list(root.rglob(f"{args.run_id}.json.zst"))
    if not matches:
        print("未找到", file=sys.stderr)
        return 1
    print(json.dumps(replay.load(matches[0]), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
