"""App preferences that aren't co-writer settings: where the novels live, update checks.

Shared by the desktop app and the command line, so both open the same novels.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w

from .llm import config_dir

APP_FILE = "app.toml"


def _app_path(path: Path | None) -> Path:
    return path or config_dir() / APP_FILE


def load_app(path: Path | None = None) -> dict:
    try:
        return tomllib.loads(_app_path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_app(changes: dict, path: Path | None = None) -> None:
    """Merge `changes` into app.toml, keeping the other preferences."""
    path = _app_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(load_app(path) | changes), "utf-8")


def saved_workspace(path: Path | None = None) -> Path | None:
    """The folder chosen in the desktop app, if one was."""
    value = load_app(path).get("workspace")
    return Path(value).expanduser() if isinstance(value, str) and value else None


def save_workspace(workspace: Path, path: Path | None = None) -> None:
    save_app({"workspace": str(workspace)}, path)


def default_workspace() -> Path:
    """NOVELBANG_WORKSPACE, else the saved choice, else ~/Novels."""
    env = os.environ.get("NOVELBANG_WORKSPACE")
    return Path(env).expanduser() if env else saved_workspace() or Path.home() / "Novels"


def check_updates(path: Path | None = None) -> bool:
    """Whether the desktop app looks for new versions (on unless turned off in Settings)."""
    return load_app(path).get("check_updates", True) is not False


def save_check_updates(enabled: bool, path: Path | None = None) -> None:
    save_app({"check_updates": bool(enabled)}, path)
