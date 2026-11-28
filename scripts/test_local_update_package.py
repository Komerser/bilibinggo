"""Opt-in Windows package smoke test, using only fresh disposable local copies.

Run after building dist/Binggo-RepostFix and copying Binggo-Updater.exe there.
No GitHub downloads, user accounts, production data or cluster joins are used.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.update_state import read_json, record, write_json
from src.local_updater import EXE


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.load(response)


def stop(port, target):
    access = read_json(target / "user-data/config/control-access.json")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/control/v2/shutdown", data=b"{}",
                                     headers={"Authorization": "Bearer " + access["token"]})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def exercise(failure=False):
    case = ROOT / ".local" / ("update-smoke-" + uuid.uuid4().hex[:10])
    target = case / "Binggo"
    source = ROOT / "dist/Binggo-RepostFix"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("user-data"))
    key = hashlib.sha256(str(target.resolve()).casefold().encode()).hexdigest()[:16]
    state_root = case / ".binggo-updates" / key
    job = state_root / "smoke"
    job.mkdir(parents=True)
    stage = job / "stage"
    shutil.copytree(source, stage, ignore=shutil.ignore_patterns("user-data"))
    port = 18185 if failure else 18184
    env = os.environ.copy()
    env["BINGGO_CONTROL_PORT"] = str(port)
    for name in ("BINGGO_UPDATE_DEMO", "BINGGO_UPDATE_VERIFY", "BINGGO_UPDATE_FINISH_TOKEN"):
        env.pop(name, None)
    old = subprocess.Popen([str(target / EXE), "--serve"], cwd=target, env=env, creationflags=0x08000000)
    deadline = time.monotonic() + 60
    while True:
        try:
            runtime = get(port, "/api/runtime")
            break
        except Exception:
            if old.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("isolated test application did not start")
            time.sleep(1)
    marker = target / "user-data/data/update-smoke-marker.txt"
    marker.write_text("preserve this local data", encoding="utf-8")
    plan = {"target": str(target), "stage": str(stage), "pid": old.pid, "port": port,
            "version": "99.0.0" if failure else runtime["version"], "current": runtime["version"],
            "token": secrets.token_hex(32), "cluster_role": ""}
    write_json(job / "plan.json", plan)
    record(state_root, {"id": "smoke", "current": runtime["version"], "target": plan["version"]}, "backup", "Isolated package smoke test")
    helper = job / "Binggo-Updater.exe"
    shutil.copy2(source / helper.name, helper)
    update = subprocess.Popen([str(helper), str(job / "plan.json")], cwd=job, env=env, creationflags=0x08000000)
    (job / "shutdown-ready").touch()
    stop(port, target)
    code = update.wait(timeout=180)
    result = read_json(state_root / "status.json")
    try:
        assert result["phase"] == ("rolled_back" if failure else "success"), result
        assert marker.read_text(encoding="utf-8") == "preserve this local data"
        assert get(port, "/api/runtime")["version"] == runtime["version"]
        assert get(port, "/api/updates/history")["entries"][0]["phase"] == result["phase"]
        print(json.dumps({"case": "rollback" if failure else "success", "helper_exit": code, "result": result["phase"], "data_preserved": True, "sandbox": str(case)}, ensure_ascii=False), flush=True)
    finally:
        stop(port, target)


if __name__ == "__main__":
    exercise(failure="--failure" in sys.argv)
