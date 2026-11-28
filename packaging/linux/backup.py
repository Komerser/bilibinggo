"""Consistent SQLite snapshots; run while the service is stopped for full consistency."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SKIP_NAMES = {"instance.json", ".instance.lock", ".server.lock"}


def snapshot(source: Path, target: Path) -> dict:
    source = source.resolve(strict=True)
    target = target.resolve()
    if target == source or target.is_relative_to(source):
        raise ValueError("Snapshot destination must be outside the live data directory")
    target.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    databases = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name in SKIP_NAMES or path.name.endswith(("-wal", "-shm", "-journal")):
            continue
        relative = path.relative_to(source)
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".db":
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) as src:
                with sqlite3.connect(dest) as dst:
                    src.backup(dst, pages=256, sleep=0.05)
                    check = dst.execute("PRAGMA integrity_check").fetchone()[0]
                    if check != "ok":
                        raise RuntimeError(f"Database integrity check failed: {relative}")
                    tables = [r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                    databases[relative.as_posix()] = {
                        "integrity_check": check,
                        "tables": {t: dst.execute('SELECT COUNT(*) FROM "' + t.replace('"', '""') + '"').fetchone()[0] for t in tables},
                    }
        else:
            shutil.copyfile(path, dest)
        dest.chmod(0o600)
    return {"started_at_utc": started, "completed_at_utc": datetime.now(timezone.utc).isoformat(), "databases": databases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    report = snapshot(args.source, args.target)
    (args.target / "snapshot-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Snapshot complete; database integrity checks passed.")
