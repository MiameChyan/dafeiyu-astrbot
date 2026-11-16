"""Copy the old bind-mounted AstrBot data directory into the Docker volume.

Runs inside the AstrBot image (see migrate-data-volume.ps1):
    python /tools/migrate_data_volume.py /src /dst

- /src is the old Windows ``deploy-windows\\data`` (mounted read-only).
- /dst is the empty ``dafeiyu-astrbot-data`` volume.

plugins/ and config/ stay on the host (compose bind-mounts them over the volume),
so they are not copied. Stale SQLite ``-shm`` files are skipped: SQLite rebuilds
them from the WAL. After copying, every ``*.db`` is checkpointed and must pass
``PRAGMA integrity_check``; otherwise the script exits non-zero and the caller
leaves the host directory untouched.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import sqlite3
import sys

HOST_ONLY = {"plugins", "config"}
SKIP_TOP = ("cmd_config.json.bak*", "knowledge_base.bak*", "_migrated_to_volume_*")


def skip_top(name: str) -> bool:
    return name in HOST_ONLY or any(fnmatch.fnmatch(name, p) for p in SKIP_TOP) or name.endswith("-shm")


def skip_nested(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n.endswith("-shm")}


def copy_tree(src: str, dst: str) -> list[str]:
    copied = []
    for name in sorted(os.listdir(src)):
        if skip_top(name):
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s) and not os.path.islink(s):
            shutil.copytree(s, d, symlinks=True, ignore=skip_nested, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d, follow_symlinks=False)
        copied.append(name)
    return copied


def check_databases(root: str) -> list[tuple[str, str]]:
    results = []
    for folder, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".db"):
                continue
            path = os.path.join(folder, name)
            con = sqlite3.connect(path)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                rows = con.execute("PRAGMA integrity_check").fetchall()
            finally:
                con.close()
            status = "ok" if rows == [("ok",)] else "; ".join(str(r[0]) for r in rows[:5])
            results.append((os.path.relpath(path, root), status))
    return results


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: migrate_data_volume.py SRC DST", file=sys.stderr)
        return 64
    src, dst = argv[1], argv[2]
    if not os.path.isfile(os.path.join(src, "data_v4.db")):
        print("ERROR: source has no data_v4.db", file=sys.stderr)
        return 3
    if os.path.exists(os.path.join(dst, "data_v4.db")):
        print("ERROR: volume already contains data_v4.db; refusing to overwrite", file=sys.stderr)
        return 2
    copied = copy_tree(src, dst)
    print("copied:", ", ".join(copied))
    results = check_databases(dst)
    bad = [(p, s) for p, s in results if s != "ok"]
    for path, status in results:
        print(f"db {path}: {status}")
    if bad:
        print("ERROR: integrity check failed", file=sys.stderr)
        return 1
    con = sqlite3.connect(os.path.join(dst, "data_v4.db"))
    try:
        convs = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    finally:
        con.close()
    print(f"conversations: {convs}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
