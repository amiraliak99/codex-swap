"""Resolve Codex and codex-swap data directories independently."""

import os
import sys
from pathlib import Path


def get_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def get_backup_root() -> Path:
    if override := os.environ.get("CODEX_SWAP_HOME"):
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex-swap"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "codex-swap"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "codex-swap"
