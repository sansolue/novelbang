from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novelbang.history import git_available
from novelbang.project import ProjectError, Workspace
from novelbang.server import create_app


@pytest.fixture
def ws(tmp_path):
    w = Workspace(tmp_path / "ws")
    p = w.create("The Salt Road")
    p.write_chapter(p.chapters[0].file, "Mara walked.")
    (p.exports_dir / "old.epub").write_bytes(b"x")
    (p.root / ".novelbang").mkdir()
    (p.root / ".novelbang" / "history.git").mkdir()
    (p.root / ".novelbang" / "synopses.json").write_text("{}")
    return w


def test_rename_moves_the_folder(ws):
    p = ws.rename("the-salt-road", "Salt and Iron")
    assert p.id == "salt-and-iron" and p.meta.title == "Salt and Iron"
    assert not (ws.root / "the-salt-road").exists()
    assert ws.rename("salt-and-iron", "SALT and iron").id == "salt-and-iron"  # same folder name: stays
    ws.create("Other")
    assert ws.rename("salt-and-iron", "Other").id == "other-2"  # taken: a free name instead
    with pytest.raises(ProjectError):
        ws.rename("other", "  ")


def test_duplicate_is_a_separate_book(ws):
    orig = ws.get("the-salt-road")
    copy = ws.duplicate("the-salt-road")
    assert copy.id == "the-salt-road-copy" and copy.meta.title == "The Salt Road (copy)"
    assert copy.meta.identifier != orig.meta.identifier
    assert copy.read_chapter(copy.chapters[0].file) == "Mara walked."
    assert not list(copy.exports_dir.iterdir())  # exports aren't copied
    assert (copy.root / ".novelbang" / "synopses.json").exists()
    assert not (copy.root / ".novelbang" / "history.git").exists()  # history starts fresh
    assert ws.duplicate("the-salt-road").id == "the-salt-road-copy-2"


def test_archive_restore_delete(ws):
    ws.archive("the-salt-road")
    assert [p.id for p in ws.list()] == [] and [p.id for p in ws.list_archived()] == ["the-salt-road"]
    with pytest.raises(ProjectError):
        ws.get("the-salt-road")
    assert ws.unarchive("the-salt-road").id == "the-salt-road"
    dest = ws.delete("the-salt-road")
    assert dest == ws.root / "Deleted novels" / "the-salt-road" and (dest / "novel.toml").exists()
    assert ws.list() == []
    # Deleting another novel of the same name never overwrites the first.
    ws.create("The Salt Road")
    assert ws.delete("the-salt-road").name == "the-salt-road-2"
    ws.create("Kept")
    ws.archive("kept")
    assert ws.delete("kept", archived=True).name == "kept"
    for bad in ("..", "../x", ".hidden", ""):
        with pytest.raises(ProjectError):
            ws.get_archived(bad)


def test_manage_api(tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    pid = c.post("/api/projects", json={"title": "Ash"}).json()["id"]
    file = c.get(f"/api/projects/{pid}").json()["chapters"][0]["file"]
    c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": "Edited, not yet in history."})

    r = c.post(f"/api/projects/{pid}/rename", json={"title": "Ember"}).json()
    assert r["id"] == "ember" and r["title"] == "Ember"
    if git_available():  # the edit made under the old name is in the history
        versions = c.get("/api/projects/ember/history", params={"type": "chapter", "name": file}).json()["versions"]
        assert any(v["message"] == "Before renaming the novel" for v in versions)
    assert c.post("/api/projects/ember/duplicate").json()["title"] == "Ember (copy)"
    assert c.post("/api/projects/ember/archive").json() == {"ok": True}
    listing = c.get("/api/projects").json()
    assert [p["id"] for p in listing["projects"]] == ["ember-copy"]
    assert [p["id"] for p in listing["archived"]] == ["ember"]
    assert c.post("/api/archived/ember/restore").json()["id"] == "ember"
    assert Path(c.delete("/api/projects/ember").json()["moved_to"]).parts[-2:] == ("Deleted novels", "ember")
    c.post("/api/projects/ember-copy/archive")
    assert c.delete("/api/archived/ember-copy").status_code == 200
    assert c.get("/api/projects").json()["projects"] == []
    assert c.delete("/api/projects/nope").status_code == 400
    assert c.post("/api/archived/..%2Fx/restore").status_code in (400, 404)
