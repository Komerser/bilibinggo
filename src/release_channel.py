"""The fork's release channel and version format, shared with the system updater."""
from __future__ import annotations

import re

REPOSITORY = "Aggressiveless/bilibinggo"
UPSTREAM_REPOSITORY = "Komerser/bilibinggo"
STABLE_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2,3}")
_BASE = r"(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}"
UPSTREAM_VERSION = re.compile(_BASE)
FORK_TAG = re.compile(rf"v({_BASE})\.01")
MERGED_VERSION = re.compile(rf"({_BASE})\.02")


def combined_version(tag: str) -> str:
    match = FORK_TAG.fullmatch(tag)
    if not match:
        raise ValueError("GitHub 发行标签须使用 vX.Y.Z.01")
    return match[1] + ".02"


def release_tag(version: str) -> str:
    match = MERGED_VERSION.fullmatch(version)
    if not match:
        raise ValueError("合并版运行版本须使用 X.Y.Z.02")
    return "v" + match[1] + ".01"


def version_key(version: str) -> tuple[int, int, int, int]:
    if not STABLE_VERSION.fullmatch(version):
        raise ValueError("Invalid release version")
    major, minor, patch, *revision = map(int, version.split("."))
    return major, minor, patch, revision[0] if revision else 0
