"""Unprivileged side of the Ubuntu systemd updater."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
import uuid

from src.update_state import SERVER_UPDATE_ROOT

CONFIG = Path("/etc/binggo-updater")


def configured() -> bool:
    return sys.platform == "linux" and os.environ.get("BINGGO_SYSTEMD_UPDATE") == "1"


def ready() -> bool:
    return configured() and (CONFIG / "admin-token").is_file() and (SERVER_UPDATE_ROOT / "inbox").is_dir()


def authorized(provided: str, *, worker: bool = False) -> bool:
    if not ready() or len(provided) != 64 or not provided.isascii():
        return False
    try:
        token = (CONFIG / ("worker-token" if worker else "admin-token")).read_text(encoding="ascii").strip()
    except OSError:
        return False
    return len(token) == 64 and secrets.compare_digest(provided, token)


def submit(name: str, content: dict) -> None:
    import json

    inbox = SERVER_UPDATE_ROOT / "inbox"
    temp = inbox / (".request-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="ascii") as file:
            json.dump(content, file)
        # Link the completed request into view atomically; never replace a pending request.
        os.link(temp, inbox / name)
    finally:
        temp.unlink(missing_ok=True)
