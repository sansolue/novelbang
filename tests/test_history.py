import subprocess

import pytest
from fastapi.testclient import TestClient

from novelbang.history import HistoryTracker, ProjectHistory, git_available, word_diff
from novelbang.project import Workspace
from novelbang.server import create_app

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


@pytest.fixture
def project(tmp_path):
    return Workspace(tmp_path / "ws").create("Salt", "Jane")


def test_snapshots_versions_and_read(project):
    h = ProjectHistory(project.root)
    ch = project.chapters[0].file
    path = f"chapters/{ch}"
    assert h.versions(path) == []

    project.write_chapter(ch, "One two three.")
    first = h.snapshot("auto", describe=lambda changes: "first")
    assert first and h.snapshot("auto") is None  # nothing changed → no commit

    project.write_chapter(ch, "One two three four five.")
    h.snapshot("checkpoint", "Before AI rewrite")
    h.snapshot("milestone", "Draft 1", allow_empty=True)
    (project.notes_dir / "world.md").write_text("elsewhere")
    h.snapshot("auto")  # doesn't touch the chapter → not in its history

    versions = h.versions(path)
    assert [(v.kind, v.message) for v in versions] == [
        ("milestone", "Draft 1"), ("checkpoint", "Before AI rewrite"), ("auto", "first"),
    ]
    assert [v.words for v in versions] == [5, 5, 3]
    assert [v.delta for v in versions] == [None, 2, 3]
    assert h.read(first, path) == "One two three."
    assert h.read(first, "chapters/nope.md") is None


def test_excludes_exports_and_trash(project):
    (project.exports_dir / "book.epub").write_bytes(b"x")
    project.add_chapter("Doomed")
    project.delete_chapter(project.chapters[-1].file)  # → trash/
    h = ProjectHistory(project.root)
    h.snapshot("auto")
    files = h._git("ls-files").stdout.decode().split()
    assert "novel.toml" in files and not any(f.startswith(("exports/", "trash/", ".novelbang/")) for f in files)


def test_isolated_from_users_git(project, tmp_path, monkeypatch):
    # The author's own repo around the project, and a global config that would break commits.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[commit]\n  gpgsign = true\n[gpg]\n  program = /bin/false\n")
    monkeypatch.setenv("HOME", str(home))
    subprocess.run(["git", "init", "-q", str(project.root)], check=True)

    assert ProjectHistory(project.root).snapshot("auto")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=project.root, capture_output=True, text=True)
    assert ".novelbang" not in status.stdout
    log = subprocess.run(["git", "log"], cwd=project.root, capture_output=True, text=True)
    assert log.returncode != 0  # the author's repo still has no commits


def test_word_diff():
    old = "The rain fell.\n\nMara ran home quickly.\n\nThe end.\n"
    new = "The rain fell.\n\nMara walked home slowly.\n\nThe end.\n"
    diff = word_diff(old, new)
    assert ("-", "ran") in diff and ("+", "walked") in diff
    assert "".join(t for op, t in diff if op != "+") == old
    assert "".join(t for op, t in diff if op != "-") == new
    assert word_diff("same", "same") == [("=", "same")]


def test_tracker_timing(monkeypatch):
    import novelbang.history as history

    clock = [1000.0]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])
    t = HistoryTracker()
    root = object()
    t.touched(root)
    clock[0] += 30
    assert t.due() == []  # still typing
    for _ in range(12):  # keeps typing every 25s for 5 minutes
        clock[0] += 25
        t.touched(root)
    assert t.due() == [root]  # max interval reached
    t.touched(root)
    clock[0] += 61
    assert t.due() == [root]  # idle
    assert t.due() == []
    t.touched(root)
    assert t.due(force=True) == [root]


def test_api_history_and_restore(tmp_path):
    app = create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        p = c.post("/api/projects", json={"title": "Ash"}).json()
        pid, file = p["id"], p["chapters"][0]["file"]
        base = f"/api/projects/{pid}"
        q = {"type": "chapter", "name": file}
        assert c.get(base).json()["history"] is True  # first visit → session snapshot

        c.put(f"{base}/chapters/{file}/text", json={"text": "Mara ran home."})
        v1 = c.post(f"{base}/history", json={"kind": "milestone", "message": "Draft 1"}).json()["version"]
        c.put(f"{base}/chapters/{file}/text", json={"text": "Mara walked home."})

        versions = c.get(f"{base}/history", params=q).json()["versions"]
        assert versions[0]["message"] == "Draft 1"
        detail = c.get(f"{base}/history/{v1}", params=q).json()
        assert detail["text"] == "Mara ran home." and ["+", "walked"] in detail["diff"]

        r = c.post(f"{base}/history/restore", json={"version": v1, **q}).json()
        assert r["text"] == "Mara ran home."
        assert c.get(f"{base}/chapters/{file}/text").json()["text"] == "Mara ran home."
        kinds = [v["kind"] for v in c.get(f"{base}/history", params=q).json()["versions"]]
        assert kinds[:2] == ["restore", "checkpoint"]  # the overwritten text was saved first

        assert c.get(f"{base}/history/{v1}", params={"type": "note", "name": "../x.md"}).status_code == 400
        assert c.get(f"{base}/history/nothex", params=q).status_code == 400
        assert c.post(f"{base}/history", json={"kind": "auto"}).status_code == 400

        c.put(f"{base}/notes/style.md", json={"text": "Terse."})
    # Leaving the client shuts the app down, which flushes pending edits to history.
    h = ProjectHistory(tmp_path / "ws" / pid)
    assert h.versions("notes/style.md")[0].kind == "auto"
