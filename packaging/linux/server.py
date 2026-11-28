"""Foreground server for systemd and Docker; keep the dashboard on loopback."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    os.umask(0o077)
    os.environ.setdefault("BINGGO_HOME", str(ROOT.parent / "user-data"))
    os.environ.pop("BINGGO_E2E", None)
    os.environ.pop("BILI_COOKIE", None)
    port = int(os.environ.get("BINGGO_PORT", "8182"))
    if not 1024 <= port <= 65535:
        raise ValueError("BINGGO_PORT must be between 1024 and 65535")

    from src import dashboard_server

    dashboard_server.DEV_DASHBOARD_PORT = port
    dashboard_server.PACKAGED_DASHBOARD_PORT = port
    dashboard_server.DASHBOARD_PORT = port
    dashboard_server.DASHBOARD_URL = dashboard_server.get_dashboard_url()

    from src.app_paths import ensure_user_dirs, user_home
    from src.app_logging import setup_logging

    # Unlike the desktop launcher, never spawn a child or open a browser.
    # Hold an advisory lock for the full lifetime of this process.
    import fcntl

    data = user_home() / "data"
    data.mkdir(parents=True, exist_ok=True)
    with (data / ".server.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another server already owns this data directory", file=sys.stderr)
            return 1
        ensure_user_dirs()
        setup_logging(console=True)
        dashboard_server.run_dashboard_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
