import os
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novelbang import backup, config
from novelbang.project import ProjectError, Workspace
from novelbang.server import create_app


@pytest.fixture
def novel(tmp_path):
    p = Workspace(tmp_path / "Novels").create("The Salt Road")
    p.write_chapter(p.chapters[0].file, "The rain had not stopped.")
    (p.exports_dir / "the-salt-road.epub").write_bytes(b"made again on demand")
    return p


def test_backup_zips_the_novel_without_exports(novel, tmp_path):
    out = backup.backup(novel.root, tmp_path / "Backup", keep=5)
    assert out.parent == tmp_path / "Backup" / novel.id and out.name.startswith(f"{novel.id} ")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert f"{novel.id}/novel.toml" in names and f"{novel.id}/chapters/{novel.chapters[0].file}" in names
    assert not any("/exports/" in n for n in names)
    assert not list(out.parent.glob("*.tmp"))


def test_only_the_newest_are_kept_and_nothing_else_is_touched(novel, tmp_path):
    dest = tmp_path / "Backup"
    (dest / novel.id).mkdir(parents=True)
    (dest / novel.id / "my notes.txt").write_text("mine")
    made = [backup.backup(novel.root, dest, keep=2) for _ in range(4)]
    kept = sorted(p.name for p in (dest / novel.id).iterdir())
    assert kept == sorted([made[2].name, made[3].name, "my notes.txt"])


def test_due_after_the_interval_only_if_changed(novel, tmp_path):
    dest = tmp_path / "Backup"
    assert backup.due(novel.root, dest, 24)  # never backed up
    out = backup.backup(novel.root, dest, keep=5)
    assert not backup.due(novel.root, dest, 24)
    # Pretend the backup is two days old.
    old = time.time() - 48 * 3600
    os.utime(out, (old, old))
    for path in novel.root.rglob("*"):
        os.utime(path, (old - 60, old - 60))
    assert not backup.due(novel.root, dest, 24)  # old, but nothing changed since
    novel.write_chapter(novel.chapters[0].file, "The rain stopped.")
    assert backup.due(novel.root, dest, 24)


def test_backup_folder_must_be_outside_the_novels(tmp_path):
    novels = tmp_path / "Novels"
    novels.mkdir()
    for bad in (novels, novels / "backups", tmp_path, Path("relative/path")):
        with pytest.raises(ProjectError):
            backup.check_folder(bad, novels)
    assert backup.check_folder(tmp_path / "Backup", novels) == (tmp_path / "Backup").resolve()


def test_prefs_fall_back_to_defaults(tmp_path):
    assert config.backup_prefs() == config.BACKUP_DEFAULTS
    config.save_app({"backup_folder": 3, "backup_every": 5, "backup_keep": 0})
    assert config.backup_prefs() == config.BACKUP_DEFAULTS


def test_backup_endpoints(novel, tmp_path):
    c = TestClient(create_app(novel.root.parent, settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    assert c.get("/api/backups").json()["folder"] == ""
    assert c.post("/api/backups/run").status_code == 400  # no folder yet
    assert c.put("/api/backups", json={"folder": str(tmp_path / "missing")}).status_code == 400
    assert c.put("/api/backups", json={"folder": str(novel.root.parent)}).status_code == 400
    dest = tmp_path / "Backup"
    dest.mkdir()
    assert c.put("/api/backups", json={"folder": str(dest), "every": 7}).status_code == 400
    r = c.put("/api/backups", json={"folder": str(dest), "every": 6, "keep": 3}).json()
    assert r["folder"] == str(dest.resolve()) and r["every"] == 6 and r["last"] is None
    r = c.post("/api/backups/run").json()
    assert r["done"] == 1 and r["status"]["last"] is not None
    r = c.post("/api/backups/run").json()
    assert r["done"] == 0 and r["unchanged"] == 1  # nothing changed since
    dest.rename(tmp_path / "Unplugged")
    r = c.post("/api/backups/run")
    assert r.status_code == 400 and "connected" in r.json()["detail"]
    assert "connected" in c.get("/api/backups").json()["error"]
    assert not dest.exists()  # never made on this disk in the drive's place
