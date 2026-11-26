"""Local-only Windows update coordination. Never writes to cluster event stores."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from src import app_paths, server_update
from src.local_updater import EXE, MAX_ARCHIVE, unpack
from src.release_channel import REPOSITORY, STABLE_VERSION
from src.update_state import SERVER_UPDATE_ROOT, maintenance, process_alive, read_json, record, state_root, write_json
from src.version_info import compare_versions

REPO = REPOSITORY
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "Binggo-Portable-win64.zip"
ACTIVE = {"download", "checksum", "prepare", "waiting", "backup", "switch", "restart", "verify", "rollback"}


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str


def _local(request: Request):
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "更新仅允许在本机控制台操作")
    if request.headers.get("x-binggo-dashboard") != "1":
        raise HTTPException(403, "请从本机控制台操作更新")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "拒绝跨站更新请求")


def release_plan(current: str, *, server: bool = False) -> dict:
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        response = client.get(RELEASE_API, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        release = response.json()
    version = str(release.get("tag_name", "")).removeprefix("v")
    if not STABLE_VERSION.fullmatch(version) or release.get("prerelease") or release.get("draft"):
        raise ValueError("未找到可安装的正式版本")
    assets = {a["name"]: a for a in release.get("assets", [])}
    result = {"current": current, "latest": version, "available": compare_versions(current, version) == -1,
              "notes": str(release.get("body", ""))[:1800],
              "release_url": f"https://github.com/{REPO}/releases/tag/v{version}"}
    if result["available"]:
        prefix = f"https://github.com/{REPO}/releases/download/v{version}/"
        archive = f"Binggo-{version}-ubuntu.tar.gz" if server else ASSET_NAME
        checksum_name = archive + ".sha256" if archive + ".sha256" in assets else "SHA256SUMS.txt"
        for key, name in (("archive", archive), ("checksums", checksum_name)):
            asset = assets.get(name, {})
            url = asset.get("browser_download_url", "")
            if url != prefix + name:
                raise ValueError("该正式版缺少当前平台的部署包或校验文件")
            result[key] = url
        if int(assets[archive].get("size", 0)) > MAX_ARCHIVE:
            raise ValueError("更新包超过允许大小")
    return result


def download(url: str, output: Path, limit: int):
    # Allow GitHub's asset redirect, never arbitrary hosts or plaintext HTTP.
    with httpx.Client(timeout=httpx.Timeout(30, read=60), follow_redirects=False) as client:
        for _ in range(5):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
                raise ValueError("更新下载来源不受信任")
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = response.headers["location"]
                    continue
                response.raise_for_status()
                size = 0
                with output.open("xb") as stream:
                    for block in response.iter_bytes(65536):
                        size += len(block)
                        if size > limit:
                            raise ValueError("下载文件超过允许大小")
                        stream.write(block)
                return
    raise ValueError("下载跳转次数过多")


def verify_checksum(archive: Path, checksums: Path):
    matches = []
    for line in checksums.read_text(encoding="utf-8-sig").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == ASSET_NAME:
            matches.append(fields[0].lower())
    if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0]):
        raise ValueError("校验文件没有唯一有效的 Windows 安装包记录")
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if not secrets.compare_digest(actual, matches[0]):
        raise ValueError("更新包校验失败，旧程序未修改")


class LocalUpdates:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.plans = {}
        self.worker = None
        self.cancel = threading.Event()

    def capabilities(self):
        if server_update.configured():
            supported = server_update.ready()
            return {"supported": supported, "demo_enabled": False, "current": app_paths.__version__,
                    "scope": "仅此服务器", "mode": "server", "label": "更新此服务器",
                    "reason": "" if supported else "服务器更新服务未安装，请在 SSH 中完成部署。"}
        target = app_paths.bundle_root().resolve()
        supported = (sys.platform == "win32" and app_paths.is_frozen()
                     and os.environ.get("BINGGO_UPDATE_DEMO") != "1"
                     and Path(sys.executable).name == EXE
                     and app_paths.user_home().resolve() == target / "user-data"
                     and (target / "Binggo-Updater.exe").is_file())
        return {"supported": supported, "demo_enabled": os.environ.get("BINGGO_UPDATE_DEMO") == "1",
                "current": app_paths.__version__, "scope": "仅此电脑", "mode": "desktop", "label": "更新版本",
                "reason": "" if supported else ("当前为演示环境，真实安装已关闭；可在下方预览成功与失败恢复流程。" if os.environ.get("BINGGO_UPDATE_DEMO") == "1" else "当前为源码或不支持的运行方式；请使用含更新程序的 Windows 发行版。")}

    def status(self):
        if server_update.configured():
            state = read_json(SERVER_UPDATE_ROOT / "state/status.json",
                              {"phase": "idle", "message": "尚未开始更新", "events": []})
            pending = read_json(SERVER_UPDATE_ROOT / "inbox/request.json", {})
            if pending and pending.get("id") != state.get("id"):
                state = {**pending, "phase": "download", "message": "等待服务器更新服务接手", "finished": False}
            return {**self.capabilities(), "update": state}
        with self.lock:
            root = state_root()
            state = read_json(root / "status.json", {"phase": "idle", "message": "尚未开始更新", "events": []})
            marker = root / "update.lock"
            if marker.exists() and not (self.worker and self.worker.is_alive()):
                try:
                    owner = int(marker.read_text().strip())
                except (OSError, ValueError):
                    owner = -1
                if owner > 0 and not process_alive(owner):
                    if state.get("id") and not state.get("finished"):
                        record(root, state, "error", "上次更新进程已退出，当前运行版本为 " + app_paths.__version__ + "；请核对备份和数据后重试", finished=True, data_status="请核对")
                    marker.unlink(missing_ok=True)
            return {**self.capabilities(), "update": state}

    def prepare(self):
        plan = release_plan(app_paths.__version__, server=server_update.configured())
        plan.update(plan_id=uuid.uuid4().hex, created_at=time.time())
        with self.lock:
            self.plans = {plan["plan_id"]: plan}
        from web.job_runner import runner
        cluster = getattr(self.app.state, "cluster", None)
        busy = runner.is_running() or (cluster is not None and cluster.update_busy())
        return {k: v for k, v in plan.items() if k not in {"archive", "checksums"}} | self.capabilities() | {"busy": busy}

    def apply(self, plan_id: str, port: int, *, server_token: str = ""):
        with self.lock:
            if not self.capabilities()["supported"]:
                raise HTTPException(409, self.capabilities()["reason"])
            plan = self.plans.get(plan_id)
            if not plan or time.time() - plan["created_at"] > 900 or not plan["available"]:
                raise HTTPException(409, "更新计划已失效，请重新检查")
            if server_update.configured():
                if not server_update.authorized(server_token):
                    raise HTTPException(403, "服务器更新口令无效")
                try:
                    server_update.submit("request.json", {"id": uuid.uuid4().hex, "current": plan["current"],
                                                           "target": plan["latest"]})
                except FileExistsError as exc:
                    raise HTTPException(409, "已有服务器更新正在进行") from exc
                self.plans.pop(plan_id, None)
                return self.status()
            if self.worker and self.worker.is_alive():
                raise HTTPException(409, "已有更新任务正在执行")
            root = state_root()
            root.mkdir(parents=True, exist_ok=True)
            try:
                with (root / "update.lock").open("x") as lock:
                    lock.write(str(os.getpid()))
            except FileExistsError:
                raise HTTPException(409, "已有更新任务或未完成的恢复记录，请先查看本机更新日志")
            self.cancel.clear()
            state = {"id": uuid.uuid4().hex, "current": plan["current"], "target": plan["latest"], "demo": False, "finished": False}
            record(root, state, "download", "正在下载正式版更新包")
            self.worker = threading.Thread(target=self._perform, args=(dict(plan), port, state), daemon=True)
            self.worker.start()
            return self.status()

    def _perform(self, plan, port, state):
        root = state_root()
        job = root / state["id"]
        job.mkdir()
        handed_off = False
        try:
            target = app_paths.bundle_root().resolve()
            if shutil.disk_usage(target).free < 3 * 1024**3:
                raise ValueError("可用磁盘空间不足 3 GB，请清理后重试")
            archive, sums = job / ASSET_NAME, job / "SHA256SUMS.txt"
            download(plan["archive"], archive, MAX_ARCHIVE)
            download(plan["checksums"], sums, 1024 * 1024)
            record(root, state, "checksum", "正在校验安装包")
            verify_checksum(archive, sums)
            record(root, state, "prepare", "正在检查更新包和运行环境")
            stage = unpack(archive, job / "stage")
            manifest = read_json(stage / "update-capabilities.json", {})
            if manifest.get("version") != plan["latest"]:
                raise ValueError("更新包声明的版本与发布版本不一致")
            installed_bytes = 0
            for path in target.rglob("*"):
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise ValueError("安装目录包含链接，请手动更新")
                if path.is_file():
                    installed_bytes += path.stat().st_size
            if shutil.disk_usage(target).free < installed_bytes * 2 + 256 * 1024**2:
                raise ValueError("磁盘空间不足以完整备份和保留数据，旧程序未修改")
            # Freeze new admissions before checking for active jobs. Existing jobs finish normally.
            from web.job_runner import runner
            runner.pause_admission_for_update()
            record(root, state, "waiting", "已暂停领取新任务，等待当前任务完成；可取消更新")
            cluster = getattr(self.app.state, "cluster", None)
            if cluster:
                cluster.wake.set()
            deadline = time.monotonic() + 24 * 3600
            while True:
                if self.cancel.is_set():
                    record(root, state, "cancelled", "更新已取消，旧版本与数据未修改", finished=True)
                    return
                if not runner.is_running() and (cluster is None or cluster.drain_for_update()):
                    break
                if time.monotonic() > deadline:
                    raise ValueError("等待任务超过 24 小时，已取消更新")
                time.sleep(.5)
            if self.cancel.is_set():
                raise ValueError("更新已取消")
            from src.dashboard_server import _active_server, request_shutdown
            if _active_server is None:
                raise ValueError("当前运行方式不能安全重启，请使用本地启动器")
            helper = job / "Binggo-Updater.exe"
            shutil.copy2(target / "Binggo-Updater.exe", helper)
            token = secrets.token_hex(32)
            payload = {"target": str(target), "stage": str(stage), "pid": os.getpid(), "port": port,
                       "cluster_role": (cluster.store.read("session") or {}).get("role", "") if cluster else "",
                       "current": plan["current"], "version": plan["latest"], "token": token}
            write_json(job / "plan.json", payload)
            helper_proc = subprocess.Popen([str(helper), str(job / "plan.json")], cwd=job, creationflags=0x08000000)
            (root / "update.lock").write_text(str(helper_proc.pid), encoding="utf-8")
            record(root, state, "backup", "任务已结束，正在退出程序并备份本机数据")
            (job / "shutdown-ready").touch()
            handed_off = True
            request_shutdown()
        except Exception as exc:
            # Do not include HTTP URLs (redirects can contain signed credentials).
            message = str(exc) if isinstance(exc, ValueError) else "更新准备失败，请检查网络、目录权限和磁盘空间后重试"
            record(root, state, "error", message, data_status="旧程序与数据未修改", finished=True)
        finally:
            if not handed_off:
                maintenance.clear()
                (root / "update.lock").unlink(missing_ok=True)
                self.resume_cluster()

    def resume_cluster(self):
        with self.lock:
            cluster = getattr(self.app.state, "cluster", None)
            if cluster and cluster.stop_event.is_set():
                from web.cluster_service import ClusterService

                replacement = ClusterService(cluster.directory)
                self.app.state.cluster = replacement
                replacement.start()

    def demo(self, failure=False):
        if not self.capabilities()["demo_enabled"]:
            raise HTTPException(404, "未启用演示")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise HTTPException(409, "已有更新流程正在运行")
            state = {"id": uuid.uuid4().hex, "current": app_paths.__version__, "target": "演示新版", "demo": True, "finished": False}
            self.cancel.clear()
            def play():
                for phase, message in [("download", "下载更新包"), ("checksum", "校验更新包"), ("prepare", "检查运行环境"),
                                       ("backup", "备份数据和旧版本"), ("switch", "切换程序"), ("restart", "重启服务"), ("verify", "验证版本、网页与集群连接")]:
                    record(state_root(), state, phase, "演示：" + message)
                    time.sleep(.9)
                record(state_root(), state, "rolled_back" if failure else "success",
                       "演示：新版验证失败，恢复旧版本流程完成；未修改真实程序或数据" if failure else "演示完成：更新流程验证通过；真实运行版本未改变",
                       data_status="演示模式，未修改", finished=True)
            self.worker = threading.Thread(target=play, daemon=True)
            self.worker.start()
            return self.status()


def install_updates(app):
    manager = LocalUpdates(app)
    app.state.local_updates = manager
    router = APIRouter(prefix="/api/updates", tags=["local-updates"])

    @router.get("/status")
    def status():
        return manager.status()

    @router.get("/history")
    def history():
        directory = SERVER_UPDATE_ROOT / "state" if server_update.configured() else state_root()
        paths = sorted((directory / "history").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return {"entries": [value for p in paths[:30] if (value := read_json(p))], "scope": "local"}

    @router.post("/prepare")
    def prepare(request: Request):
        _local(request)
        try:
            return manager.prepare()
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except httpx.HTTPError:
            raise HTTPException(502, "暂时无法读取正式版本，请稍后重试")

    @router.post("/apply")
    def apply(request: Request, body: ApplyRequest):
        _local(request)
        from src.dashboard_server import get_dashboard_port
        return manager.apply(body.plan_id, get_dashboard_port(),
                             server_token=request.headers.get("x-binggo-update-admin", ""))

    @router.post("/cancel")
    def cancel(request: Request):
        _local(request)
        update = manager.status()["update"]
        if update.get("phase") != "waiting":
            raise HTTPException(409, "仅等待任务期间可以取消")
        if server_update.configured():
            if not server_update.authorized(request.headers.get("x-binggo-update-admin", "")):
                raise HTTPException(403, "服务器更新口令无效")
            try:
                server_update.submit("cancel.json", {"id": update["id"]})
            except FileExistsError:
                pass
            return {"ok": True}
        manager.cancel.set()
        return {"ok": True}

    @router.post("/systemd/drain")
    def drain(request: Request):
        _local(request)
        if not server_update.authorized(request.headers.get("x-binggo-update-worker", ""), worker=True):
            raise HTTPException(403, "服务器更新服务凭据无效")
        from web.job_runner import runner

        runner.pause_admission_for_update()
        cluster = getattr(app.state, "cluster", None)
        return {"ready": not runner.is_running() and (cluster is None or cluster.drain_for_update())}

    @router.post("/systemd/resume")
    def resume(request: Request):
        _local(request)
        if not server_update.authorized(request.headers.get("x-binggo-update-worker", ""), worker=True):
            raise HTTPException(403, "服务器更新服务凭据无效")
        maintenance.clear()
        manager.resume_cluster()
        return {"ok": True}

    @router.post("/demo")
    def demo(request: Request, failure: bool = False):
        _local(request)
        return manager.demo(failure)

    @router.post("/finish")
    def finish(request: Request):
        _local(request)
        expected = os.environ.get("BINGGO_UPDATE_FINISH_TOKEN", "")
        if not expected or not secrets.compare_digest(expected, request.headers.get("x-binggo-update-token", "")):
            raise HTTPException(403, "更新交接凭据无效")
        maintenance.clear()
        os.environ.pop("BINGGO_UPDATE_FINISH_TOKEN", None)
        os.environ.pop("BINGGO_UPDATE_VERIFY", None)
        return {"ok": True}

    app.include_router(router)
