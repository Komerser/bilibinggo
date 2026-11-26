from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import tarfile
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src import server_update
from web import update_routes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def systemd_worker(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location("test_systemd_updater", ROOT / "packaging/linux/updater.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_requests_require_server_admin_and_accept_a_single_pending_job(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "admin-token").write_text("a" * 64)
    (config / "worker-token").write_text("b" * 64)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(server_update, "configured", lambda: True)
    monkeypatch.setattr(server_update, "CONFIG", config)
    monkeypatch.setattr(server_update, "SERVER_UPDATE_ROOT", tmp_path)
    monkeypatch.setattr(update_routes, "SERVER_UPDATE_ROOT", tmp_path)
    monkeypatch.setattr(update_routes, "release_plan", lambda current, *, server: {
        "current": current, "latest": "6.1.2.02", "available": True,
        "archive": "fixed-url", "checksums": "fixed-checksum",
    })
    app = FastAPI()
    update_routes.install_updates(app)
    with TestClient(app) as client:
        headers = {"X-Binggo-Dashboard": "1"}
        plan = client.post("/api/updates/prepare", headers=headers).json()
        assert plan["label"] == "更新此服务器"
        assert "archive" not in plan
        payload = {"plan_id": plan["plan_id"]}
        assert client.post("/api/updates/apply", headers=headers, json=payload).status_code == 403
        assert not (inbox / "request.json").exists()
        assert client.post("/api/updates/systemd/drain", headers={
            **headers, "X-Binggo-Update-Worker": "a" * 64,
        }).status_code == 403
        headers["X-Binggo-Update-Admin"] = "a" * 64
        status = client.post("/api/updates/apply", headers=headers, json=payload).json()
        assert status["update"]["target"] == "6.1.2.02"
        assert "token" not in (inbox / "request.json").read_text()
        plan = client.post("/api/updates/prepare", headers=headers).json()
        assert client.post("/api/updates/apply", headers=headers, json={"plan_id": plan["plan_id"]}).status_code == 409


def test_rejects_links_in_systemd_package(systemd_worker, tmp_path):
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        item = tarfile.TarInfo("binggo-server-6.1.2.01/app")
        item.type = tarfile.SYMTYPE
        item.linkname = "outside"
        package.addfile(item)
    with pytest.raises(systemd_worker.UpdateError):
        systemd_worker.extract_release(archive, tmp_path / "stage", "6.1.2.01")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("valid", [True, False])
def test_systemd_switch_preserves_or_restores_data_and_environment(systemd_worker, tmp_path, monkeypatch, valid):
    worker = systemd_worker
    install = tmp_path / "install"
    data = tmp_path / "data"
    state = tmp_path / "state"
    work = tmp_path / "work" / ("a" * 32)
    config = tmp_path / "config"
    unit = tmp_path / worker.SERVICE
    for directory in (install / "app", install / "venv", data, state, work, config, install / "new", install / "new-venv"):
        directory.mkdir(parents=True)
    for path, text in ((install / "app/version", "old"), (install / "venv/runtime", "old"),
                       (install / "new/version", "new"), (install / "new-venv/runtime", "new"),
                       (data / "activity.db", "before"), (config / "server.env", "existing"), (unit, "service")):
        path.write_text(text)
    monkeypatch.setattr(worker, "INSTALL", install)
    monkeypatch.setattr(worker, "DATA", data)
    monkeypatch.setattr(worker, "CONFIG", config)
    monkeypatch.setattr(worker, "UNIT", unit)
    monkeypatch.setattr(worker, "copy_tree", shutil.copytree)
    actions = []
    monkeypatch.setattr(worker, "systemctl", actions.append)

    def verify(version, cluster=""):
        if version == "6.1.1.01":
            assert (data / "activity.db").read_text() == "before"
            return True
        assert cluster == "member"
        assert (install / "app/version").read_text() == "new"
        assert (install / "venv/runtime").read_text() == "new"
        assert (state / "hold").exists()
        if not valid:
            (data / "activity.db").write_text("new schema")
        return valid

    phases = []
    dashboard = SimpleNamespace(verify=verify, worker=actions.append)
    result = worker.switch(dashboard, lambda phase, message, **kw: phases.append(phase), state, work,
                           install / "new", install / "new-venv", "6.1.1.01", "6.1.2.01", "member")
    assert result == valid
    assert not (state / "hold").exists()
    assert (data / "activity.db").read_text() == "before"
    assert (config / "server.env").read_text() == "existing"
    assert (work / "backup/data/activity.db").read_text() == "before"
    assert (install / "app/version").read_text() == ("new" if valid else "old")
    assert (install / "venv/runtime").read_text() == ("new" if valid else "old")
    assert phases[-1] == ("success" if valid else "rolled_back")
    assert actions == (["stop", "start", "resume"] if valid else ["stop", "start", "stop", "start"])
