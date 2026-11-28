from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import local_updater as worker
from src.update_state import maintenance, read_json, record, write_json
from web import update_routes as routes


def package(tmp_path, extra=None):
    archive = tmp_path / routes.ASSET_NAME
    files = {f"Binggo/{worker.EXE}": b"new", "Binggo/_internal/runtime": b"runtime",
             "Binggo/Binggo-Updater.exe": b"helper",
             "Binggo/update-capabilities.json": b'{"schema":1,"version":"6.2.0"}'}
    files.update(extra or {})
    with zipfile.ZipFile(archive, "w") as output:
        for name, value in files.items():
            output.writestr(name, value)
    return archive


@pytest.mark.parametrize("name", ["../escape", "Binggo/../../escape", "/absolute", "C:/absolute", "Binggo/user-data/cookies", "Binggo/evil:stream", "Binggo/invalid./x"])
def test_reject_unsafe_archives(tmp_path, name):
    with pytest.raises(ValueError):
        worker.unpack(package(tmp_path, {name: b"no"}), tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_archive_and_checksum(tmp_path):
    archive = package(tmp_path)
    checksums = tmp_path / "SHA256SUMS.txt"
    checksums.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name)
    routes.verify_checksum(archive, checksums)
    root = worker.unpack(archive, tmp_path / "stage")
    assert (root / worker.EXE).read_bytes() == b"new"
    archive.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="校验失败"):
        routes.verify_checksum(archive, checksums)


def test_require_updater_capability(tmp_path):
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(f"Binggo/{worker.EXE}", b"old")
        output.writestr("Binggo/_internal/x", b"old")
    with pytest.raises(ValueError, match="尚不支持"):
        worker.unpack(archive, tmp_path / "stage")


def test_private_history_does_not_touch_cluster(tmp_path):
    state = {"id": "one", "current": "6.1.1", "target": "6.2.0"}
    record(tmp_path, state, "success", "done", finished=True)
    assert read_json(tmp_path / "history/one.json")["message"] == "done"
    assert set(p.name for p in tmp_path.iterdir()) == {"history", "status.json"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    maintenance.clear()
    monkeypatch.setattr(routes, "state_root", lambda: tmp_path)
    app = FastAPI()
    routes.install_updates(app)
    with TestClient(app) as client:
        yield client
    maintenance.clear()


def test_routes_require_local_dashboard_header(client):
    assert client.post("/api/updates/apply", json={"plan_id": "fake"}).status_code == 403
    assert client.post("/api/updates/prepare", headers={"X-Binggo-Dashboard": "1", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/updates/finish", headers={"X-Binggo-Dashboard": "1"}).status_code == 403


def test_source_update_is_rejected(client):
    response = client.post("/api/updates/apply", json={"plan_id": "fake"}, headers={"X-Binggo-Dashboard": "1"})
    assert response.status_code == 409
    assert client.get("/api/updates/status").json()["update"]["phase"] == "idle"


def test_demo_not_enabled_by_default(client, monkeypatch):
    monkeypatch.delenv("BINGGO_UPDATE_DEMO", raising=False)
    assert client.post("/api/updates/demo", headers={"X-Binggo-Dashboard": "1"}).status_code == 404


def test_handoff_token_required(client, monkeypatch):
    monkeypatch.setenv("BINGGO_UPDATE_FINISH_TOKEN", "secret")
    maintenance.set()
    headers = {"X-Binggo-Dashboard": "1", "X-Binggo-Update-Token": "bad"}
    assert client.post("/api/updates/finish", headers=headers).status_code == 403
    assert maintenance.is_set()
    headers["X-Binggo-Update-Token"] = "secret"
    assert client.post("/api/updates/finish", headers=headers).status_code == 200
    assert not maintenance.is_set()


def test_maintenance_prevents_native_jobs(isolated_home):
    from web.job_runner import JobRunner
    runner = JobRunner()
    maintenance.set()
    try:
        assert runner.try_start("refresh_status") is None
        assert not runner.is_running()
    finally:
        maintenance.clear()


class Process:
    def __init__(self):
        self.stopped = False
    def poll(self):
        return 0 if self.stopped else None
    def terminate(self):
        self.stopped = True
    def wait(self, timeout=None):
        return 0


def install_fixture(tmp_path, monkeypatch):
    target = tmp_path / "Installed Binggo"
    target.mkdir()
    (target / worker.EXE).write_text("old")
    (target / "user-data/data").mkdir(parents=True)
    (target / "user-data/data/history.db").write_text("old data")
    (target / "unins000.exe").write_text("uninstaller")
    root = tmp_path / ".binggo-updates/key"
    job = root / "job"
    job.mkdir(parents=True)
    stage = job / "stage"
    stage.mkdir()
    (stage / worker.EXE).write_text("new")
    plan = {"target": str(target), "stage": str(stage), "pid": 12345, "port": 12345,
            "version": "6.2.0", "current": "6.1.1", "token": "private"}
    write_json(job / "plan.json", plan)
    record(root, {"id": "job"}, "backup", "starting")
    (job / "shutdown-ready").touch()
    monkeypatch.setattr(worker, "wait_process", lambda pid: None)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(worker, "finish_handoff", lambda plan: None)
    monkeypatch.setattr(worker, "verify_application", lambda plan: None)
    monkeypatch.setattr(worker, "health", lambda *a: False)
    return target, job, root


def test_update_preserves_data_and_installer(tmp_path, monkeypatch):
    target, job, root = install_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(worker, "wait_health", lambda *a: True)
    assert worker.run(job / "plan.json") == 0
    assert (target / worker.EXE).read_text() == "new"
    assert (target / "user-data/data/history.db").read_text() == "old data"
    assert (target / "unins000.exe").read_text() == "uninstaller"
    assert (job / "backup/user-data/data/history.db").read_text() == "old data"
    assert read_json(root / "status.json")["phase"] == "success"


def test_failed_start_rolls_back_program_and_data(tmp_path, monkeypatch):
    target, job, root = install_fixture(tmp_path, monkeypatch)
    def check(port, version, path, process):
        if version == "6.2.0":
            (path / "user-data/data/history.db").write_text("migrated data")
            return False
        return True
    monkeypatch.setattr(worker, "wait_health", check)
    assert worker.run(job / "plan.json") == 1
    assert (target / worker.EXE).read_text() == "old"
    assert (target / "user-data/data/history.db").read_text() == "old data"
    assert read_json(root / "status.json")["phase"] == "rolled_back"


def test_running_process_timeout_never_replaces_install(tmp_path, monkeypatch):
    target, job, root = install_fixture(tmp_path, monkeypatch)
    def blocked(pid):
        raise RuntimeError("still running")
    monkeypatch.setattr(worker, "wait_process", blocked)
    assert worker.run(job / "plan.json") == 1
    assert (target / worker.EXE).read_text() == "old"
    assert not (job / "previous-install").exists()


def test_duplicate_update_and_expired_plan(client, tmp_path, monkeypatch):
    manager = client.app.state.local_updates
    monkeypatch.setattr(manager, "capabilities", lambda: {"supported": True})
    with pytest.raises(Exception) as error:
        manager.apply("missing", 8182)
    assert error.value.status_code == 409
    manager.plans["valid"] = {"created_at": routes.time.time(), "available": True}
    (tmp_path / "update.lock").touch()
    with pytest.raises(Exception) as error:
        manager.apply("valid", 8182)
    assert error.value.status_code == 409


def test_waiting_update_can_cancel_without_shutdown(tmp_path, monkeypatch):
    from web.job_runner import runner
    target = tmp_path / "install"
    target.mkdir()
    (target / worker.EXE).write_text("old")
    root = tmp_path / "private-updates"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    archive = package(source)
    monkeypatch.setattr(routes, "state_root", lambda: root)
    monkeypatch.setattr(routes.app_paths, "bundle_root", lambda: target)
    app = SimpleNamespace(state=SimpleNamespace())
    manager = routes.LocalUpdates(app)
    def fake_download(url, output, limit):
        if output.name == routes.ASSET_NAME:
            output.write_bytes(archive.read_bytes())
        else:
            output.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + routes.ASSET_NAME)
    monkeypatch.setattr(routes, "download", fake_download)
    monkeypatch.setattr(runner, "is_running", lambda: True)
    monkeypatch.setattr(routes.time, "sleep", lambda duration: manager.cancel.set())
    state = {"id": "waiting-job", "current": "6.1.1", "target": "6.2.0"}
    manager._perform({"archive": "unused", "checksums": "unused", "latest": "6.2.0"}, 8182, state)
    assert read_json(root / "status.json")["phase"] == "cancelled"
    assert (target / worker.EXE).read_text() == "old"
    assert not maintenance.is_set()
