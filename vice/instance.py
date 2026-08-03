"""Instance-scoped names and paths.

Environment overrides are intentionally opt-in: an ordinary Vice process sees
exactly the historical paths and branding.
"""
from __future__ import annotations

import os
from pathlib import Path

from .runtime import actual_home_dir


def _path_env(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


HOME = actual_home_dir()
INSTANCE = os.environ.get("VICE_INSTANCE", "vice").strip() or "vice"
APP_NAME = os.environ.get("VICE_APP_NAME", "Vice").strip() or "Vice"
RUNTIME_NAME = os.environ.get("VICE_RUNTIME_NAME", INSTANCE).strip() or INSTANCE
CONFIG_DIR = _path_env("VICE_CONFIG_DIR", HOME / ".config" / INSTANCE)
DATA_DIR = _path_env("VICE_DATA_DIR", HOME / ".local" / "share" / INSTANCE)
CACHE_DIR = _path_env("VICE_CACHE_DIR", HOME / ".cache" / INSTANCE)
RUNTIME_DIR = _path_env("VICE_RUNTIME_DIR", Path("/tmp") / RUNTIME_NAME)
CLI_NAME = os.environ.get("VICE_CLI_NAME", "vice" if INSTANCE == "vice" else INSTANCE)
APP_CLI_NAME = os.environ.get(
    "VICE_APP_CLI_NAME", "vice-app" if INSTANCE == "vice" else f"{INSTANCE}-app"
)
SERVICE_NAME = os.environ.get(
    "VICE_SERVICE_NAME", "vice.service" if INSTANCE == "vice" else f"{INSTANCE}.service"
)
IS_PATCH = INSTANCE == "vice-patch"


def default_output_directory() -> Path:
    return HOME / "Videos" / ("Vice" if INSTANCE == "vice" else "Vice-Patch")
