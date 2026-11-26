"""The fork's release channel and version format, shared with the system updater."""
from __future__ import annotations

import re

REPOSITORY = "Komerser/bilibinggo"
STABLE_VERSION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2,3}")


def version_key(version: str) -> tuple[int, int, int, int]:
    if not STABLE_VERSION.fullmatch(version):
        raise ValueError("Invalid release version")
    major, minor, patch, *revision = map(int, version.split("."))
    return major, minor, patch, revision[0] if revision else 0
