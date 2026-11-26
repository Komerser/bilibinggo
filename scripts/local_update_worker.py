"""Standalone entry point for the Windows updater executable."""
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.local_updater import run

if __name__ == "__main__":
    plan = Path(sys.argv[1]).resolve()
    try:
        result = run(plan)
    finally:
        (plan.parent.parent / "update.lock").unlink(missing_ok=True)
    raise SystemExit(result)
