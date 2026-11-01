"""Automatic backups: each novel zipped into a folder the author chooses.

Version history lives inside the novel's folder, so a lost or broken disk takes it
too. A folder on another drive, or one a sync client (Dropbox, iCloud Drive,
OneDrive) copies elsewhere, keeps a copy safe:

    <backup folder>/<novel>/<novel> 2026-11-01 14.30.05.zip

A zip holds the whole novel folder, version history included, except exports/
(made again on demand). A novel is backed up when it has changed since its newest
zip and that zip is older than the interval; only the newest `keep` zips of each
novel are kept. Nothing else in the backup folder is ever touched.
"""

from __future__ import annotations

import os
import re
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from .project import ProjectError

STAMP = "%Y-%m-%d %H.%M.%S"
_STAMP_RE = r"\d{4}-\d\d-\d\d \d\d\.\d\d\.\d\d"
SKIP_TOP = {"exports"}  # regenerated on demand


def check_folder(folder: Path, workspace: Path) -> Path:
    """The backup folder, made absolute; refused if it's inside the folder of novels."""
    folder = folder.expanduser()
    if not folder.is_absolute():
        raise ProjectError("Give the backup folder's full path, e.g. /Volumes/Backup/Novels")
    folder, workspace = folder.resolve(), workspace.resolve()
    if folder == workspace or folder.is_relative_to(workspace) or workspace.is_relative_to(folder):
        raise ProjectError("Choose a backup folder outside the folder your novels are in, "
                           "ideally on another drive or in a synced folder")
    return folder


def _zips(dest: Path, project_id: str) -> list[Path]:
    """This novel's backups, oldest first (their names sort by time)."""
    folder = dest / project_id
    if not folder.is_dir():
        return []
    pattern = re.compile(rf"{re.escape(project_id)} {_STAMP_RE}\.zip")
    return sorted(p for p in folder.iterdir() if pattern.fullmatch(p.name))


def last_backup(dest: Path, project_id: str) -> float | None:
    """When the newest backup began: its zip's modified time, set to that moment."""
    zips = _zips(dest, project_id)
    return zips[-1].stat().st_mtime if zips else None


def _files(root: Path):
    for folder, dirs, files in os.walk(root):
        if Path(folder) == root:
            dirs[:] = [d for d in dirs if d not in SKIP_TOP]
        for name in files:
            if not name.endswith(".tmp"):  # half-written saves
                yield Path(folder) / name


def changed_since(root: Path, when: float) -> bool:
    return any(p.stat().st_mtime > when for p in _files(root))


def due(root: Path, dest: Path, every_hours: float) -> bool:
    last = last_backup(dest, root.name)
    if last is None:
        return True
    return time.time() - last >= every_hours * 3600 and changed_since(root, last)


def backup(root: Path, dest: Path, keep: int) -> Path:
    """Zip the novel folder at `root` into dest/<novel>/, then drop all but the newest `keep`."""
    folder = dest / root.name
    folder.mkdir(parents=True, exist_ok=True)
    # Always after the newest, even two backups within a second, so the oldest is the one dropped.
    started, zips = time.time(), _zips(dest, root.name)
    stamp = datetime.fromtimestamp(int(started))
    if zips:
        newest = datetime.strptime(zips[-1].name.removeprefix(root.name + " ").removesuffix(".zip"), STAMP)
        stamp = max(stamp, datetime.fromtimestamp(newest.timestamp() + 1))
    out = folder / f"{root.name} {stamp.strftime(STAMP)}.zip"
    # Written under another name first: a half-written zip never looks like a backup.
    tmp = folder / f".{out.name}.{uuid.uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for path in _files(root):
                z.write(path, Path(root.name) / path.relative_to(root))
        # Anything saved after this moment may have missed the zip, so counts as changed.
        os.utime(tmp, (started, started))
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    for old in _zips(dest, root.name)[: -max(keep, 1)]:
        old.unlink(missing_ok=True)
    return out
