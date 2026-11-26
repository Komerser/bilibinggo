"""Install a verified Ubuntu release for the local Binggo systemd instance."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

# Isolated mode excludes the script directory. Only add the root-owned helper location.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_channel import REPOSITORY, STABLE_VERSION, version_key
from update_state import SERVER_UPDATE_ROOT, read_json, record

API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD = f"https://github.com/{REPOSITORY}/releases/download"
PORT_FILE = Path("/etc/binggo-updater/port")
WORKER_TOKEN = Path("/etc/binggo-updater/worker-token")
SERVICE = "binggo.service"
INSTALL = Path("/opt/binggo")
DATA = Path("/var/lib/binggo")
CONFIG = Path("/etc/binggo")
UNIT = Path("/etc/systemd/system/binggo.service")
MAX_ARCHIVE = 800 * 1024**2
MAX_EXTRACTED = 2 * 1024**3
REQUEST_ID = re.compile(r"[a-f0-9]{32}")


class UpdateError(RuntimeError):
    pass


class Cancelled(UpdateError):
    pass


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_github(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def require_github(url):
    parts = urlsplit(url)
    hosts = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    if parts.scheme != "https" or parts.hostname not in hosts or parts.username or parts.password or parts.port not in {None, 443}:
        raise UpdateError("下载地址不在允许的 GitHub 来源内")


def open_github(url):
    require_github(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Binggo-systemd-updater", "Accept": "application/vnd.github+json"})
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), GitHubRedirect()).open(request, timeout=25)


def download(url, path, limit):
    with open_github(url) as response, path.open("xb") as output:
        size = 0
        deadline = time.monotonic() + 300
        while block := response.read(64 * 1024):
            size += len(block)
            if size > limit or time.monotonic() > deadline:
                raise UpdateError("下载过大或超过允许时间")
            output.write(block)


def checksum(path, checksums):
    values = []
    for line in checksums.read_text(encoding="utf-8-sig").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == path.name:
            values.append(fields[0].lower())
    if len(values) != 1 or not re.fullmatch(r"[a-f0-9]{64}", values[0]):
        raise UpdateError("未找到唯一有效的 SHA-256 校验记录")
    with path.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != values[0]:
            raise UpdateError("更新包 SHA-256 校验失败")


def fetch_release(version, work, progress):
    with open_github(API) as response:
        payload = json.loads(response.read(2 * 1024**2 + 1))
    if payload.get("tag_name") != f"v{version}" or payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("最新正式版本已变化，请重新检查更新")
    name = f"Binggo-{version}-ubuntu.tar.gz"
    assets = {asset.get("name"): asset for asset in payload.get("assets", [])}
    sums_name = name + ".sha256" if name + ".sha256" in assets else "SHA256SUMS.txt"
    prefix = f"{DOWNLOAD}/v{version}/"
    for item in (name, sums_name):
        if assets.get(item, {}).get("browser_download_url") != prefix + item:
            raise UpdateError("正式版缺少 Ubuntu 部署包或校验文件")
    archive, sums = work / name, work / sums_name
    download(prefix + name, archive, MAX_ARCHIVE)
    download(prefix + sums_name, sums, 1024**2)
    progress("checksum", "正在校验 Ubuntu 部署包")
    checksum(archive, sums)
    return archive


def extract_release(archive, destination, version):
    root = f"binggo-server-{version}"
    size = 0
    names = set()
    with tarfile.open(archive, "r:gz") as package:
        for item in package:
            path = PurePosixPath(item.name)
            if (
                not path.parts or path.parts[0] != root or ".." in path.parts
                or "\\" in item.name or str(path) in names
                or not (item.isfile() or item.isdir())
            ):
                raise UpdateError("部署包包含不支持的文件路径")
            size += item.size
            names.add(str(path))
            if len(names) > 30000 or size > MAX_EXTRACTED:
                raise UpdateError("解压内容超过允许大小")
            package.extract(item, destination, filter="data")
    return destination / root


def program_version(app):
    tree = ast.parse((app / "src/app_paths.py").read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(name, ast.Name) and name.id == "__version__" for name in statement.targets
        ):
            version = ast.literal_eval(statement.value)
            if isinstance(version, str) and STABLE_VERSION.fullmatch(version):
                return version
    raise UpdateError("无法识别程序版本")


def validate_payload(bundle, version):
    manifest = read_json(bundle / "deployment-manifest.json", {})
    if manifest.get("version") != version or manifest.get("systemd_update_schema") != 1 or manifest.get("repository") != REPOSITORY:
        raise UpdateError("部署包版本或更新协议不匹配")
    app = bundle / "app"
    required = ("packaging/linux/server.py", "packaging/linux/requirements.lock", "web/static/dist/index.html", "web/update_routes.py")
    if not all((app / name).is_file() for name in required) or program_version(app) != version:
        raise UpdateError("部署包缺少运行所需文件或版本不一致")
    return app


def read_request(path):
    import pwd

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != pwd.getpwnam("binggo").pw_uid or info.st_size > 512:
            raise UpdateError("服务器更新请求格式无效")
        result = json.loads(stream.read(513))
    if not isinstance(result, dict):
        raise UpdateError("服务器更新请求格式无效")
    return result


def systemctl(action):
    subprocess.run(["/usr/bin/systemctl", action, SERVICE], check=True, timeout=150)


def copy_tree(source, target):
    subprocess.run(["/usr/bin/cp", "-a", "--reflink=auto", "--", str(source), str(target)], check=True, timeout=600)


class Dashboard:
    def __init__(self, port, token):
        self.url = f"http://127.0.0.1:{port}"
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path):
        with self.opener.open(self.url + path, timeout=8) as response:
            return json.load(response)

    def worker(self, action):
        request = urllib.request.Request(
            self.url + "/api/updates/systemd/" + action, data=b"{}",
            headers={"X-Binggo-Dashboard": "1", "X-Binggo-Update-Worker": self.token},
        )
        with self.opener.open(request, timeout=12) as response:
            return json.load(response)

    def verify(self, version, expected_cluster=""):
        deadline = time.monotonic() + 85
        while time.monotonic() < deadline:
            try:
                runtime = self.get("/api/runtime")
                if runtime.get("ok") and runtime.get("version") == version and runtime.get("user_home") == str(DATA):
                    with self.opener.open(self.url + "/", timeout=8) as response:
                        if b"Binggo" not in response.read(256 * 1024):
                            raise UpdateError("新版网页验证失败")
                    if not isinstance(self.get("/api/summary").get("job"), dict):
                        raise UpdateError("新版数据读取验证失败")
                    if not expected_cluster:
                        return True
                    cluster = self.get("/api/cluster/status")
                    if cluster.get("role") == expected_cluster and cluster.get("connected") and not cluster.get("error"):
                        return True
            except (OSError, ValueError):
                pass
            time.sleep(1)
        return False


def stage_program(app, request_id):
    staged = INSTALL / f".app-new-{request_id}"
    venv = INSTALL / f".venv-new-{request_id}"
    copy_tree(app, staged)
    # The release was unpacked under the updater's private directory.
    subprocess.run(["/usr/bin/chmod", "-R", "a+rX", "--", str(staged)], check=True)
    new_lock = staged / "packaging/linux/requirements.lock"
    old_lock = INSTALL / "app/packaging/linux/requirements.lock"
    if old_lock.exists() and new_lock.read_bytes() == old_lock.read_bytes():
        return staged, None
    subprocess.run(["/usr/bin/python3", "-m", "venv", str(venv)], check=True, timeout=180)
    subprocess.run([
        str(venv / "bin/python"), "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
        "--only-binary=:all:", "--index-url", "https://pypi.org/simple", "-r", str(new_lock),
    ], check=True, timeout=600)
    subprocess.run(["/usr/bin/chmod", "-R", "a+rX", "--", str(venv)], check=True)
    return staged, venv


def switch(dashboard, progress, state_dir, work, staged, venv, current, version, cluster):
    request_id = work.name
    held = state_dir / "hold"
    old_app = INSTALL / f".app-before-{request_id}"
    old_env = INSTALL / f".venv-before-{request_id}"
    live_app, live_env = INSTALL / "app", INSTALL / "venv"
    snapshot = work / "backup"
    stopped = started = committed = False
    held.touch(mode=0o640, exist_ok=False)
    try:
        progress("backup", "正在停止服务并备份数据、配置和程序",
                 backup=str(snapshot), previous_program=str(old_app))
        systemctl("stop")
        stopped = True
        snapshot.mkdir(mode=0o700)
        copy_tree(DATA, snapshot / "data")
        if CONFIG.exists():
            copy_tree(CONFIG, snapshot / "config")
        shutil.copy2(UNIT, snapshot / SERVICE)
        progress("switch", "备份完成，正在切换程序版本", data_status="已备份")
        live_app.rename(old_app)
        staged.rename(live_app)
        if venv:
            live_env.rename(old_env)
            venv.rename(live_env)
        progress("restart", "正在启动新版服务")
        started = True
        systemctl("start")
        progress("verify", "正在验证运行版本、网页和原集群连接")
        if not dashboard.verify(version, cluster):
            raise UpdateError("新版运行状态未通过验证")
        held.unlink()
        committed = True
        try:
            dashboard.worker("resume")
        except (OSError, ValueError):
            progress("error", f"新版 {version} 已运行，但恢复任务接收未确认。请在 SSH 中检查服务。",
                     running_version=version, data_status="数据和配置已保留", finished=True)
            return False
        progress("success", f"更新完成，当前版本 {version}。如有暂停的队列，可在日志页继续。",
                 running_version=version, data_status="数据和配置已保留", finished=True)
        return True
    except Exception:
        if committed:
            # Work may already be running on the validated new version.
            raise
        try:
            if started:
                systemctl("stop")
            if old_app.exists():
                progress("rollback", "新版未通过验证，正在恢复旧程序及升级前数据")
                if live_app.exists():
                    live_app.rename(INSTALL / f".app-failed-{request_id}")
                old_app.rename(live_app)
            if old_env.exists():
                if live_env.exists():
                    live_env.rename(INSTALL / f".venv-failed-{request_id}")
                old_env.rename(live_env)
            if started:
                restored = DATA.with_name(f".binggo-restore-{request_id}")
                copy_tree(snapshot / "data", restored)
                DATA.rename(DATA.with_name(f".binggo-failed-{request_id}"))
                restored.rename(DATA)
            held.unlink(missing_ok=True)
            if stopped:
                systemctl("start")
                if not dashboard.verify(current):
                    raise UpdateError("旧版启动验证未通过")
        except Exception:
            progress("error", "自动恢复未完成，请在 SSH 中检查服务和备份",
                     data_status="需人工处理", finished=True)
            return False
        progress("rolled_back" if started else "error",
                 "新版未通过验证，已恢复旧版本" if started else "未完成安装，旧版本仍保留",
                 running_version=current, data_status="已恢复升级前数据" if started else "数据未替换", finished=True)
        return False


def update(request, *, bundle=None):
    root = SERVER_UPDATE_ROOT
    request_id, current, version = request.get("id", ""), request.get("current", ""), request.get("target", "")
    if not (
        isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id)
        and isinstance(current, str) and STABLE_VERSION.fullmatch(current)
        and isinstance(version, str) and STABLE_VERSION.fullmatch(version)
    ):
        raise UpdateError("更新请求缺少有效版本")
    state_dir = root / "state"
    work = root / "work" / request_id
    state = {"id": request_id, "current": current, "target": version, "finished": False, "demo": False}

    def progress(phase, message, **details):
        record(state_dir, state, phase, message, mode=0o640, **details)
        print(f"{phase}: {message}", flush=True)

    port = int(PORT_FILE.read_text(encoding="ascii"))
    if not 1024 <= port <= 65535:
        raise UpdateError("服务端口配置无效")
    dashboard = Dashboard(port, WORKER_TOKEN.read_text(encoding="ascii").strip())
    drained = False
    try:
        if (state_dir / "hold").exists():
            raise UpdateError("检测到未完成的升级，请先通过 SSH 检查并恢复服务")
        if program_version(INSTALL / "app") != current or version_key(version) <= version_key(current):
            raise UpdateError("版本已变化或目标版本不高于当前版本")
        runtime = dashboard.get("/api/runtime")
        if runtime.get("user_home") != str(DATA) or runtime.get("version") != current or runtime.get("bind_host") != "127.0.0.1":
            raise UpdateError("运行实例与 systemd 部署不匹配")
        if min(shutil.disk_usage(INSTALL).free, shutil.disk_usage(root).free) < 3 * 1024**3:
            raise UpdateError("磁盘空闲空间不足 3 GB")
        work.mkdir(mode=0o700)
        if bundle is None:
            progress("download", "正在下载正式版 Ubuntu 部署包")
            archive = fetch_release(version, work, progress)
            bundle = extract_release(archive, work / "unpacked", version)
        progress("prepare", "正在检查版本、安装文件和运行依赖")
        app = validate_payload(bundle, version)
        staged, venv = stage_program(app, request_id)
        former_cluster = dashboard.get("/api/cluster/status")
        cluster = former_cluster.get("role") if former_cluster.get("connected") and not former_cluster.get("error") else ""
        progress("waiting", "等待正在执行的任务结束")
        if not request.get("bootstrap"):
            drained = True
            deadline = time.monotonic() + 24 * 3600
            while True:
                cancel = read_request(root / "inbox/cancel.json")
                if cancel and cancel.get("id") == request_id:
                    raise Cancelled("已取消更新，原版本继续运行")
                if dashboard.worker("drain").get("ready"):
                    cancel = read_request(root / "inbox/cancel.json")
                    if cancel and cancel.get("id") == request_id:
                        raise Cancelled("已取消更新，原版本继续运行")
                    break
                if time.monotonic() > deadline:
                    raise Cancelled("等待任务超过 24 小时，原版本继续运行")
                time.sleep(2)
        elif dashboard.get("/api/jobs/current").get("state") == "running":
            raise UpdateError("当前任务尚未结束，请稍后执行首次升级")
        return switch(dashboard, progress, state_dir, work, staged, venv, current, version, cluster or "")
    except Cancelled as exc:
        progress("cancelled", str(exc), data_status="未修改", finished=True)
        return False
    except Exception as exc:
        message = str(exc) if isinstance(exc, UpdateError) else f"升级未完成（{type(exc).__name__}），请查看服务日志"
        progress("error", message, finished=True)
        return False
    finally:
        if drained and not (state_dir / "hold").exists() and state.get("phase") != "success":
            try:
                dashboard.worker("resume")
            except (OSError, ValueError):
                pass


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, help="upgrade from a locally verified, extracted bundle as root")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run as root")
    os.umask(0o077)
    root = SERVER_UPDATE_ROOT
    with (root / "work/worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.bootstrap:
            bundle = args.bootstrap.resolve(strict=True)
            request = {"id": uuid.uuid4().hex, "current": program_version(INSTALL / "app"),
                       "target": program_version(bundle / "app"), "bootstrap": True}
            return 0 if update(request, bundle=bundle) else 1
        inbox = root / "inbox"
        try:
            request = read_request(inbox / "request.json")
            if request is None:
                return 0
            # Only the root CLI can use bootstrap mode.
            request = {key: request.get(key) for key in ("id", "current", "target")}
            return 0 if update(request) else 1
        finally:
            (inbox / "cancel.json").unlink(missing_ok=True)
            (inbox / "request.json").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
