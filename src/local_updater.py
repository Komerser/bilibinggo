"""Windows updater worker. Standard library only; runs outside the installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

from src.update_state import read_json, record, write_json
from src.release_channel import STABLE_VERSION

EXE = "Binggo-RepostFix.exe"
MAX_ARCHIVE = 800 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024


def unpack(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        if len(infos) > 30000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError("更新包超过允许大小")
        names = set()
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith("/") or any(p in {"..", "."} or ":" in p or p.endswith((".", " ")) for p in parts)
                    or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError("更新包包含不安全路径")
            if name.casefold() in names:
                raise ValueError("更新包存在重复文件")
            names.add(name.casefold())
            if any(p.casefold() in {"user-data", ".git"} for p in parts):
                raise ValueError("更新包不应包含用户数据")
        package.extractall(destination)
    roots = [p.parent for p in destination.rglob(EXE)]
    if len(roots) != 1:
        raise ValueError("更新包缺少唯一的 Windows 程序")
    payload = roots[0]
    if not (payload / "_internal").is_dir() or not (payload / "Binggo-Updater.exe").is_file():
        raise ValueError("此发行包尚不支持一键更新，请使用发布页手动安装")
    manifest = read_json(payload / "update-capabilities.json", {})
    if manifest.get("schema") != 1 or not STABLE_VERSION.fullmatch(str(manifest.get("version", ""))):
        raise ValueError("更新包缺少兼容性声明")
    return payload


def safe_copy(source: Path, destination: Path) -> None:
    # Never follow junctions/symlinks out of an installation or backup.
    for path in [source, *source.rglob("*")]:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("安装目录包含链接，请手动更新")
    shutil.copytree(source, destination)


def health(port: int, version: str, target: Path) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runtime", timeout=2) as response:
            data = json.load(response)
        return (data.get("version") == version and data.get("ok") is True
                and Path(data["user_home"]).resolve() == (target / "user-data").resolve())
    except Exception:
        return False


def wait_process(pid: int):
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, 60000) != 0:
                raise RuntimeError("旧进程未退出，未替换程序")
        finally:
            kernel.CloseHandle(handle)
    elif ctypes.get_last_error() not in (0, 87):
        raise RuntimeError("无法确认旧进程退出")


def verify_application(plan):
    with urllib.request.urlopen(f"http://127.0.0.1:{plan['port']}/", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("新版网页未就绪")
    if plan.get("cluster_role") in {"member", "host"}:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{plan['port']}/api/cluster/status", timeout=5) as response:
                    state = json.load(response)
                if state.get("role") == plan["cluster_role"] and state.get("connected") and not state.get("error"):
                    return
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("新版集群连接未通过验证")


def finish_handoff(plan):
    req = urllib.request.Request(f"http://127.0.0.1:{plan['port']}/api/updates/finish",
                                 data=b"{}", headers={"X-Binggo-Update-Token": plan["token"], "X-Binggo-Dashboard": "1"})
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("新版无法完成更新交接")


def wait_health(port, version, target, proc, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if health(port, version, target):
            return True
        time.sleep(1)
    return False


def run(plan_file: Path) -> int:
    plan = read_json(plan_file)
    root = plan_file.parent.parent
    job = plan_file.parent
    target = Path(plan["target"]).resolve()
    stage = Path(plan["stage"]).resolve()
    if (not stage.is_relative_to(job.resolve()) or target.parent != root.parent.parent
            or target == target.parent or not (target / EXE).is_file()):
        raise ValueError("更新目录校验失败")
    state = read_json(root / "status.json", {})
    if state.get("id") != job.name:
        raise ValueError("更新任务已失效")
    previous = job / "previous-install"
    backup = job / "backup"
    proc = None
    switched = False
    exited = False
    committed = False
    try:
        # Application explicitly signals that it is drained and shutting down.
        deadline = time.monotonic() + 90
        while not (job / "shutdown-ready").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("等待应用退出超时，未替换程序")
            time.sleep(.2)
        # Wait for the exact server process and launcher to release all files.
        wait_process(plan["pid"])
        exited = True
        time.sleep(2)
        record(root, state, "backup", "正在备份本机数据、配置和旧程序", backup=str(backup))
        safe_copy(target, backup)
        record(root, state, "switch", "备份完成，正在切换程序", data_status="已备份")
        # Keep all local data and installer bookkeeping. Replace managed binaries only.
        for child in target.iterdir():
            if child.name.casefold() in {EXE.casefold(), "_internal", "binggo-updater.exe", "update-capabilities.json"}:
                continue
            dest = stage / child.name
            if dest.exists():
                continue
            if child.is_dir():
                safe_copy(child, dest)
            else:
                shutil.copy2(child, dest)
        target.rename(previous)
        try:
            stage.rename(target)
        except Exception:
            previous.rename(target)
            raise
        switched = True
        record(root, state, "restart", "正在启动新版程序")
        env = os.environ.copy()
        env.update(BINGGO_CONTROL_PORT=str(plan["port"]), BINGGO_UPDATE_VERIFY="1", BINGGO_UPDATE_FINISH_TOKEN=plan["token"])
        env.pop("BINGGO_UPDATE_DEMO", None)
        proc = subprocess.Popen([str(target / EXE), "--serve"], cwd=target, env=env, creationflags=0x08000000)
        record(root, state, "verify", "正在验证运行版本、网页和数据目录")
        if not wait_health(plan["port"], plan["version"], target, proc):
            raise RuntimeError("新版启动验证失败")
        verify_application(plan)
        finish_handoff(plan)
        committed = True
        record(root, state, "success", f"更新完成：当前版本 {plan['version']}。任务队列请在日志中按需继续。",
               current=plan["version"], data_status="数据及配置已保留", finished=True)
        return 0
    except Exception:
        if committed:
            # The new application has resumed work: never roll it back merely
            # because persisting a completion message failed.
            return 0
        if not exited:
            record(root, state, "error", "无法确认旧进程退出，未替换程序；请检查本机进程后重试", data_status="未修改", finished=True)
            return 1
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=15)
        if switched:
            record(root, state, "rollback", "新版验证失败，正在恢复旧程序和升级前数据")
            try:
                target.rename(job / "failed-install")
                previous.rename(target)
            except Exception:
                record(root, state, "error", "自动恢复未完成，请使用备份人工恢复", data_status="需人工处理", finished=True)
                return 1
        env = os.environ.copy()
        env.pop("BINGGO_UPDATE_VERIFY", None)
        env.pop("BINGGO_UPDATE_FINISH_TOKEN", None)
        # A pre-shutdown timeout must never launch another instance.
        if health(plan["port"], plan["current"], target):
            record(root, state, "error", "更新未执行，旧版本仍在运行", data_status="未修改", finished=True)
            return 1
        old = subprocess.Popen([str(target / EXE), "--serve"], cwd=target, env=env, creationflags=0x08000000)
        restored = wait_health(plan["port"], plan["current"], target, old)
        record(root, state, "rolled_back" if restored else "error",
               "更新失败，已恢复旧版本" if restored else "更新失败，旧版本启动未确认，请查看本机备份",
               data_status="已恢复升级前数据" if switched else "未修改", finished=True)
        return 1


if __name__ == "__main__":
    plan_file = Path(sys.argv[1]).resolve()
    try:
        result = run(plan_file)
    finally:
        (plan_file.parent.parent / "update.lock").unlink(missing_ok=True)
    raise SystemExit(result)
