"""App preferences that aren't co-writer settings: where the novels live.

Shared by the desktop app and the command line, so both open the same novels.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w

from .llm import config_dir

APP_FILE = "app.toml"


def saved_workspace(path: Path | None = None) -> Path | None:
    """The folder chosen in the desktop app, if one was."""
    try:
        data = tomllib.loads((path or config_dir() / APP_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("workspace")
    return Path(value).expanduser() if isinstance(value, str) and value else None


def save_workspace(workspace: Path, path: Path | None = None) -> None:
    path = path or config_dir() / APP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps({"workspace": str(workspace)}), "utf-8")


def default_workspace() -> Path:
    """NOVELBANG_WORKSPACE, else the saved choice, else ~/Novels."""
    env = os.environ.get("NOVELBANG_WORKSPACE")
    return Path(env).expanduser() if env else saved_workspace() or Path.home() / "Novels"
