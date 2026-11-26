"""Private local update state; deliberately separate from cluster events."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

maintenance = threading.Event()
SERVER_UPDATE_ROOT = Path("/var/lib/binggo-updater")
if os.environ.get("BINGGO_UPDATE_VERIFY") == "1" or (
    os.environ.get("BINGGO_SYSTEMD_UPDATE") == "1" and (SERVER_UPDATE_ROOT / "state/hold").exists()
):
    maintenance.set()


def state_root() -> Path:
    from src.app_paths import bundle_root, is_frozen, user_home
    if is_frozen():
        target = bundle_root().resolve()
        key = hashlib.sha256(str(target).casefold().encode()).hexdigest()[:16]
        return target.parent / ".binggo-updates" / key
    return user_home() / "data" / "local-updates"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if mode is not None:
        temporary.chmod(mode)
    os.replace(temporary, path)


def process_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # Access denied: conservatively busy.
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def record(root: Path, state: dict, phase: str, message: str, *, mode: int | None = None, **extra) -> dict:
    state.update(phase=phase, message=message, updated_at=int(time.time()), **extra)
    state.setdefault("events", []).append({"phase": phase, "message": message, "at": int(time.time())})
    state["events"] = state["events"][-80:]
    write_json(root / "status.json", state, mode=mode)
    write_json(root / "history" / (state["id"] + ".json"), state, mode=mode)
    return state
