"""Build the Ubuntu release from tracked project files and built frontend assets."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.app_paths import __version__
from src.release_channel import REPOSITORY

PACKAGE = f"binggo-server-{__version__}"
CONFIG = {"config/cookies.txt.example", "config/llm.env.example", "config/sources.yaml"}


def copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def digest(file: Path) -> str:
    with file.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def inputs():
    command = ["git", "ls-files", "-z", "--", "src", "web", "mcp", "packaging/linux", "config"]
    for name in subprocess.check_output(command, cwd=ROOT).decode().split("\0"):
        if not name or name.startswith("web/frontend/") or (name.startswith("config/") and name not in CONFIG):
            continue
        yield name


def make_bundle(destination: Path) -> Path:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    frontend = ROOT / "web/static/dist"
    if not (frontend / "index.html").is_file():
        raise SystemExit("Build the frontend before packaging")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"Binggo-{__version__}-ubuntu.tar.gz"
    with tempfile.TemporaryDirectory(prefix="binggo-linux-") as tmp:
        bundle = Path(tmp) / PACKAGE
        app = bundle / "app"
        for name in inputs():
            copy(ROOT / name, app / name)
        for path in frontend.rglob("*"):
            if path.is_file():
                copy(path, app / "web/static/dist" / path.relative_to(frontend))
        for name in ("README.md", "compose.yaml", "deploy.sh"):
            copy(ROOT / "packaging/linux" / name, bundle / name)
        manifest = {"version": __version__, "repository": REPOSITORY, "commit": commit,
                    "systemd_update_schema": 1, "data_directory": "/var/lib/binggo"}
        (bundle / "deployment-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        files = sorted(path for path in bundle.rglob("*") if path.is_file())
        (bundle / "FILES.sha256").write_text("".join(f"{digest(path)}  {path.relative_to(bundle).as_posix()}\n" for path in files),
                                            encoding="ascii", newline="\n")
        with archive.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gzip_file:
            with tarfile.open(fileobj=gzip_file, mode="w") as tar:
                for path in [bundle, *sorted(bundle.rglob("*"))]:
                    member = tar.gettarinfo(str(path), arcname=(Path(PACKAGE) / path.relative_to(bundle)).as_posix())
                    member.uid = member.gid = member.mtime = 0
                    member.uname = member.gname = "root"
                    member.mode = 0o755 if member.isdir() or path.suffix == ".sh" else 0o644
                    if member.isfile():
                        with path.open("rb") as file:
                            tar.addfile(member, file)
                    else:
                        tar.addfile(member)
    archive.with_suffix(".gz.sha256").write_text(f"{digest(archive)}  {archive.name}\n", encoding="ascii", newline="\n")
    return archive


if __name__ == "__main__":
    print(make_bundle(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "dist"))
