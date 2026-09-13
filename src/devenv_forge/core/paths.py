"""Per-OS locations for this application's own data."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "DevEnvForge"
APP_SLUG = "devenv-forge"


def data_dir() -> Path:
    """Directory for backups, logs and saved definitions."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_SLUG


def backup_dir() -> Path:
    return data_dir() / "backups"


def log_dir() -> Path:
    return data_dir() / "logs"


def ensure_dirs() -> None:
    for directory in (data_dir(), backup_dir(), log_dir()):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
