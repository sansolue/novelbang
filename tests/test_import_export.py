import pytest
from fastapi.testclient import TestClient

from novelbang.importer import import_manuscript, read_markdown, split_chapters
from novelbang.project import ProjectError, Workspace
from novelbang.server import create_app


# ---- import ------------------------------------------------------------------------

def test_markdown_and_text_imports(tmp_path):
    ws = Workspace(tmp_path)
    md = b"---\ntitle: Ember\nauthor: K. Lin\n---\n\n# Ember\n\n## Chapter 1\n\nFire *rose*.\n\n---\n\nSmoke.\n\n## Chapter 2\n\nAsh.\n"
    p = import_manuscript(ws, "draft.md", md)
    assert (p.meta.title, p.meta.author, p.meta.number_chapters) == ("Ember", "K. Lin", False)
    assert [(c.title, p.read_chapter(c.file)) for c in p.chapters] == [
        ("Chapter 1", "Fire *rose*.\n\n***\n\nSmoke.\n"), ("Chapter 2", "Ash.\n")]
    txt = "THE IRON SEA\nby R. Vale\n\nPROLOGUE\n\nSalt.\n\nCHAPTER ONE\n\nThe ship\nleft at dawn. 5*3_000\n\nCHAPTER TWO: Storm\n\nWind.\n"
    p = import_manuscript(ws, "sea.txt", txt.encode("cp1252"))
    assert (p.meta.title, p.meta.author) == ("The Iron Sea", "R. Vale")
    assert [(c.title, c.kind) for c in p.chapters] == [("Prologue", "front"), ("Chapter One", "chapter"), ("Storm", "chapter")]
    assert p.read_chapter(p.chapters[1].file) == "The ship left at dawn. 5\\*3\\_000\n"


def test_text_before_the_first_chapter_is_kept(tmp_path):
    long_opening = "A long opening that is clearly part of the story and not a title page at all. " * 3
    chapters, _ = split_chapters(read_markdown(f"{long_opening}\n\n# One\n\nText.\n"))
    assert [c.title for c in chapters] == ["Opening", "One"] and chapters[0].text.startswith("A long opening")
    chapters, _ = split_chapters(read_markdown("No headings at all.\n\nJust two paragraphs.\n"))
    assert len(chapters) == 1 and chapters[0].text == "No headings at all.\n\nJust two paragraphs.\n"
    # A title page (title, subtitle, byline) is read, not kept as text…
    draft = read_markdown("The Salt Road\n\nA Novel\n\nby Jane Doe\n\n# One\n\nText.\n")
    chapters, _ = split_chapters(draft)
    assert (draft.title, draft.subtitle, draft.author, [c.title for c in chapters]) == ("The Salt Road", "A Novel", "Jane Doe", ["One"])
    # …but anything more than that is story.
    chapters, _ = split_chapters(read_markdown("Rain.\n\nMore rain.\n\nStill raining.\n\nSun.\n\n# One\n\nText.\n"))
    assert chapters[0].title == "Opening" and chapters[0].text.startswith("Rain.")


def test_import_refuses_what_it_cannot_read(tmp_path):
    ws = Workspace(tmp_path)
    for name, data, msg in [
        ("old.doc", b"x", "Save it as a Word document"),
        ("book.scriv", b"x", "Compile"),
        ("broken.docx", b"not a zip", "isn't a Word document"),
        ("empty.md", b"\n\n", "no text"),
        ("photo.jpg", b"x", "Import a Word document"),
    ]:
        with pytest.raises(ProjectError, match=msg):
            import_manuscript(ws, name, data)
    assert ws.list() == []  # nothing half-made is left behind


def test_import_api(tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    md = b"# One\n\nText.\n\n# Two\n\nMore.\n"
    r = c.post("/api/import", params={"name": "draft.md", "author": "Someone"}, content=md).json()
    assert r["title"] == "draft" and r["chapter_count"] == 2 and r["author"] == "Someone"
    assert c.post("/api/import", params={"name": "x.doc"}, content=b"x").status_code == 400
