import json
import os
import zipfile
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from novelbang.export import EXPORTERS
from novelbang.llm import _ThinkFilter
from novelbang.project import Project, ProjectError, Workspace
from novelbang import prompts
from novelbang.server import create_app

PROSE = """The rain had not stopped for three days. Mara said, "It's fine" -- but it wasn't.

She found the letter under the floorboard&mdash;*unopened*.

***

Morning came. **Nobody** spoke.
"""


@pytest.fixture
def project(tmp_path):
    p = Workspace(tmp_path).create("The Salt Road", "Jane Doe")
    first = p.chapters[0].file
    p.write_chapter(first, PROSE)
    p.update_chapter(first, synopsis="Mara finds a letter.")
    dedication = p.add_chapter("Dedication", kind="front")
    p.reorder([dedication.file, first])
    p.write_chapter(dedication.file, "For M.")
    p.add_chapter("Chapter Two")
    return p


def test_create_layout(tmp_path):
    p = Workspace(tmp_path).create("The Salt Road")
    assert (p.root / "novel.toml").is_file()
    assert {"outline.md", "characters.md", "world.md", "style.md"} <= set(p.list_notes())
    assert len(p.chapters) == 1 and (p.chapters_dir / p.chapters[0].file).is_file()
    # Second project with the same title gets its own folder.
    assert Workspace(tmp_path).create("The Salt Road").root.name == "the-salt-road-2"


def test_roundtrip_and_numbering(project):
    loaded = Project.load(project.root)
    assert [c.title for c in loaded.chapters] == ["Dedication", "Chapter One", "Chapter Two"]
    assert [n for _, n in loaded.numbered()] == [None, 1, 2]
    assert loaded.chapters[1].synopsis == "Mara finds a letter."
    assert loaded.meta.author == "Jane Doe"


def test_hand_added_files_are_adopted(project):
    (project.chapters_dir / "099-epilogue.md").write_text("The end.")
    loaded = Project.load(project.root)
    assert loaded.chapters[-1].file == "099-epilogue.md"
    assert loaded.chapters[-1].title == "Epilogue"


def test_missing_chapter_file_keeps_its_metadata(project):
    toml_before = (project.root / "novel.toml").read_text()
    path = project.chapters_dir / project.chapters[1].file
    hidden = path.rename(path.with_suffix(".bak"))
    loaded = Project.load(project.root)
    assert (project.root / "novel.toml").read_text() == toml_before  # loading never writes
    assert loaded.chapters[1].synopsis == "Mara finds a letter."
    assert loaded.word_counts()[path.name] == 0
    assert any("missing" in i["message"] for i in loaded.publish_checks())
    with pytest.raises(ProjectError):
        loaded.read_chapter(path.name)
    hidden.rename(path)
    back = Project.load(project.root)
    assert [c.title for c in back.chapters] == ["Dedication", "Chapter One", "Chapter Two"]
    assert back.chapters[1].synopsis == "Mara finds a letter."


def test_delete_goes_to_trash(project):
    ch = project.chapters[-1]
    project.delete_chapter(ch.file)
    assert (project.root / "trash" / ch.file).exists()
    assert ch.file not in [c.file for c in project.chapters]


@pytest.mark.parametrize("name", ["../novel.toml", "../../etc/passwd.md", "a/b.md", ".hidden.md", "x.txt"])
def test_rejects_unsafe_names(project, name):
    with pytest.raises(ProjectError):
        project.read_note(name)


def test_epub_is_well_formed(project):
    project.set_cover(b"\x89PNG\r\n\x1a\nfake", "png")
    project.meta.subtitle = "A Novel"
    project.meta.series = "Salt"
    project.save()
    out = EXPORTERS["epub"][2](project)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        for name in names:
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ElementTree.fromstring(z.read(name))  # raises on malformed XML
        opf = z.read("OEBPS/content.opf").decode()
        assert "cover-image" in opf and "Jane Doe" in opf
        chapter = z.read("OEBPS/ch002.xhtml").decode()
        assert "Chapter 1" in chapter
        assert "<em>unopened</em>" in chapter
        assert 'class="scene-break"' in chapter
        assert "&mdash;" not in chapter and "&ldquo;" not in chapter
        assert "Chapter" not in z.read("OEBPS/ch001.xhtml").decode().split("<h1>")[0].split("<body")[1]


@pytest.mark.parametrize("fmt", ["html", "site", "serial", "markdown"])
def test_other_exports(project, fmt):
    out = EXPORTERS[fmt][2](project)
    assert out.is_file() and out.stat().st_size > 0
    if out.suffix == ".zip":
        with zipfile.ZipFile(out) as z:
            assert len(z.namelist()) >= 3


def test_publish_checks(project):
    levels = {c["message"]: c["level"] for c in project.publish_checks()}
    assert any("cover" in m for m in levels)
    assert any("Chapter Two" in m and lvl == "warn" for m, lvl in levels.items())


def test_think_filter_across_chunks():
    f = _ThinkFilter()
    chunks = ["Hello <th", "ink>secret plan</thi", "nk> world", "<think>more</think>!"]
    assert "".join(f.feed(c) for c in chunks) == "Hello  world!"


def test_prompts_fit_budget(project):
    first = project.chapters[1].file
    msgs = prompts.continue_messages(project, first, PROSE * 200, "", "make it tense", 6000)
    assert len(msgs[1]["content"]) < 9000
    assert "make it tense" in msgs[1]["content"]
    assert "Style guide" in msgs[1]["content"]
    later = prompts.continue_messages(project, project.chapters[2].file, "", "", "", 6000)
    assert "Mara finds a letter." in later[1]["content"]


def test_api_flow(tmp_path):
    app = create_app(tmp_path / "ws", settings_path=tmp_path / "settings.toml")
    c = TestClient(app, base_url="http://127.0.0.1")
    p = c.post("/api/projects", json={"title": "Ash", "author": "A"}).json()
    pid, file = p["id"], p["chapters"][0]["file"]
    assert c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": "One two three."}).json()["ok"]
    assert c.get(f"/api/projects/{pid}").json()["words"] == 3
    r = c.post(f"/api/projects/{pid}/chapters", json={"title": "Two"}).json()
    order = [ch["file"] for ch in r["project"]["chapters"]][::-1]
    assert [ch["file"] for ch in c.put(f"/api/projects/{pid}/order", json={"files": order}).json()["chapters"]] == order
    assert c.put(f"/api/projects/{pid}/meta", json={"description": "Blurb", "number_chapters": False}).json()["meta"]["number_chapters"] is False
    name = c.post(f"/api/projects/{pid}/export/epub").json()["name"]
    assert c.get(f"/api/projects/{pid}/exports/{name}").status_code == 200
    assert c.get(f"/api/projects/{pid}/exports/..%2Fnovel.toml").status_code == 404
    assert c.get(f"/api/projects/{pid}/notes/..%2F..%2Fsecret.md").status_code in (400, 404)
    # No model configured → a streamed error, not a crash.
    lines = c.post(f"/api/projects/{pid}/ai/continue", json={"file": file}).text.splitlines()
    assert "error" in json.loads(lines[0])
    # Probing a different server never sends the saved API key; and it's POST-only.
    c.put("/api/settings", json={"api_key": "sk-secret"})
    seen = []

    async def fake_list_models(s):
        seen.append((s.base_url, s.api_key))
        return []

    import novelbang.server as server_mod
    server_mod.list_models, real = fake_list_models, server_mod.list_models
    try:
        assert c.get("/api/llm/models?base_url=https://evil.example").status_code == 405
        c.post("/api/llm/models", json={"base_url": "https://evil.example"})
        c.post("/api/llm/models", json={})
    finally:
        server_mod.list_models = real
    assert seen == [("https://evil.example", ""), ("http://localhost:11434/v1", "sk-secret")]
    # Requests addressed to another host name are refused.
    assert TestClient(app, base_url="http://evil.example").get("/api/projects").status_code == 400


_RealAsyncClient = __import__("httpx").AsyncClient


def _collect(settings, monkeypatch, handler):
    import asyncio
    import httpx
    from novelbang import llm

    real = _RealAsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def run():
        return [x async for x in llm.stream_chat(settings, [{"role": "user", "content": "hi"}])]

    return asyncio.run(run())


SSE_OK = (
    'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    "data: [DONE]\n\n"
)


def test_thinking_disabled_by_default_and_strict_server_fallback(monkeypatch):
    import httpx
    from novelbang.llm import THINKING, LLMSettings

    bodies = []

    def strict(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": "unknown field reasoning_effort"})
        return httpx.Response(200, text=SSE_OK)

    out = _collect(LLMSettings(model="m"), monkeypatch, strict)
    assert out == [THINKING, "Hello"]
    assert bodies[0]["reasoning_effort"] == "none" and "reasoning_effort" not in bodies[1]

    bodies.clear()
    _collect(LLMSettings(model="m", thinking=True), monkeypatch, strict)
    assert len(bodies) == 1 and "reasoning_effort" not in bodies[0]


def test_add_chapter_after_unknown_leaves_no_file(project):
    before = sorted(p.name for p in project.chapters_dir.iterdir())
    with pytest.raises(ProjectError):
        project.add_chapter("Stray", after="nope.md")
    assert sorted(p.name for p in project.chapters_dir.iterdir()) == before
    assert len(Project.load(project.root).chapters) == 3


def test_meta_and_settings_updates_are_validated(tmp_path):
    import stat

    settings_path = tmp_path / "settings.toml"
    c = TestClient(create_app(tmp_path / "ws", settings_path=settings_path), base_url="http://127.0.0.1")
    pid = c.post("/api/projects", json={"title": "Ash"}).json()["id"]
    url = f"/api/projects/{pid}/meta"
    assert c.put(url, json={"target_words": "abc"}).status_code == 400
    assert c.put(url, json={"number_chapters": "yes please"}).status_code == 400
    assert c.put(url, json={"title": {"x": 1}}).status_code == 400
    # One bad field rejects the whole update.
    assert c.put(url, json={"title": "Changed", "target_words": "abc"}).status_code == 400
    meta = c.put(url, json={"__dict__": {}, "identifier": "x", "number_chapters": "false",
                            "series_index": 2, "target_words": "90000"}).json()["meta"]
    assert meta["title"] == "Ash" and meta["identifier"] != "x"
    assert meta["number_chapters"] is False and meta["series_index"] == "2" and meta["target_words"] == 90000

    assert c.put("/api/settings", json={"temperature": "hot", "model": "m"}).status_code == 400
    assert c.get("/api/settings").json()["model"] == ""  # untouched by the rejected update
    assert c.put("/api/settings", json={"api_key": "sk-x"}).status_code == 200
    if os.name != "nt":  # Windows has no mode bits; the user profile's ACLs keep the key private there
        assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600


def test_concurrent_edits_are_not_lost(project):
    import threading

    ws = Workspace(project.root.parent)
    files = [c.file for c in project.chapters]

    def set_synopsis(i):
        for n in range(20):
            with ws.edit(project.id) as p:
                p.update_chapter(files[i], synopsis=f"s{i}-{n}")

    threads = [threading.Thread(target=set_synopsis, args=(i,)) for i in range(len(files))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    loaded = Project.load(project.root)
    assert [c.synopsis for c in loaded.chapters] == [f"s{i}-19" for i in range(len(files))]
    assert not list(project.root.rglob("*.tmp"))


def _png() -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + chunk(b"IEND", b"")


def test_pictures_in_every_export(project):
    first = project.chapters[1].file
    src = project.add_image(_png(), "The Map.PNG")
    assert src == "../assets/images/the-map.png"
    assert project.add_image(_png(), "the map.png").endswith("the-map-2.png")
    with pytest.raises(ProjectError):
        project.add_image(b"x", "evil.svg")
    project.write_chapter(first, f'{PROSE}\n![A map]({src} "The coast road")\n\nAfter the map.\n\n'
                                 "![gone](../assets/images/nope.png) and ![x](../novel.toml)\n")
    assert project.word_counts()[first] < len(PROSE.split()) + 10

    out = EXPORTERS["epub"][2](project)
    with zipfile.ZipFile(out) as z:
        assert z.read("OEBPS/assets/images/the-map.png") == _png()
        opf = z.read("OEBPS/content.opf").decode()
        assert 'href="assets/images/the-map.png" media-type="image/png"' in opf
        chapter = z.read("OEBPS/ch002.xhtml").decode()
        ElementTree.fromstring(chapter)
        assert '<figure class="illustration">' in chapter
        assert 'src="assets/images/the-map.png"' in chapter
        assert "<figcaption>The coast road</figcaption>" in chapter
        assert "nope.png" not in chapter and "novel.toml" not in chapter
        assert "nope" not in opf

    html = EXPORTERS["html"][2](project).read_text()
    assert "data:image/png;base64," in html and "../assets" not in html
    for fmt in ("site", "serial"):
        with zipfile.ZipFile(EXPORTERS[fmt][2](project)) as z:
            assert any(n.endswith("/assets/images/the-map.png") for n in z.namelist())
            pages = [z.read(n).decode() for n in z.namelist() if n.endswith((".html", ".md"))]
            assert any('src="assets/images/the-map.png"' in p or "](assets/images/the-map.png" in p for p in pages)

    checks = [c for c in project.publish_checks() if c["level"] == "error"]
    assert any("nope.png" in c["message"] for c in checks)
    assert any("novel.toml" in c["message"] for c in checks)
    assert not any("the-map" in c["message"] for c in checks)


def test_picture_upload_api(tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    pid = c.post("/api/projects", json={"title": "Ash"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/images?name=image.png", content=_png(), headers={"Content-Type": "image/png"})
    assert r.json() == {"src": "../assets/images/image.png"}
    r = c.post(f"/api/projects/{pid}/images?name=../../x.png", content=b"<svg/>", headers={"Content-Type": "image/svg+xml"})
    assert r.status_code == 400
    r = c.post(f"/api/projects/{pid}/images?name=../../x.png", content=_png(), headers={"Content-Type": "image/png"})
    assert r.json()["src"] == "../assets/images/x.png"


def test_preview_and_assets_api(tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    p = c.post("/api/projects", json={"title": "Ash"}).json()
    pid, file = p["id"], p["chapters"][0]["file"]
    src = c.post(f"/api/projects/{pid}/images?name=my map.png", content=_png(), headers={"Content-Type": "image/png"}).json()["src"]
    text = f'Before.\n\n![Map]({src} "The road")\n\n![lost](../assets/images/lost.png)\n'
    r = c.post(f"/api/projects/{pid}/preview", json={"text": text, "file": file}).json()
    assert "Chapter 1" in r["html"] and "<figcaption>The road</figcaption>" in r["html"]
    assert f'src="/api/projects/{pid}/assets/images/my-map.png"' in r["html"]
    assert r["missing"] == ["../assets/images/lost.png"] and "figure.illustration" in r["css"]
    assert c.get(f"/api/projects/{pid}/assets/images/my-map.png").content == _png()
    for bad in ("../novel.toml", "..%2Fnovel.toml", "images/lost.png", "%2E%2E/%2E%2E/s.toml"):
        assert c.get(f"/api/projects/{pid}/assets/{bad}").status_code == 404
    note = c.post(f"/api/projects/{pid}/preview", json={"text": "Just *notes*."}).json()
    assert "<h1>" not in note["html"] and "<em>notes</em>" in note["html"]
    meta = c.put(f"/api/projects/{pid}/meta", json={"picture_size": "0"}).json()["meta"]
    assert meta["picture_size"] == 0


def test_split_chapter_at_cursor(project):
    first = project.chapters[1]
    project.update_chapter(first.file, status="revised")
    at = PROSE.index("Morning came.")
    new = project.split_chapter(first.file, at, "Morning")
    assert [c.title for c in project.chapters] == ["Dedication", "Chapter One", "Morning", "Chapter Two"]
    assert new.kind == "chapter" and new.status == "revised" and new.synopsis == ""
    assert project.read_chapter(first.file).endswith("***\n")
    assert project.read_chapter(new.file) == "Morning came. **Nobody** spoke.\n"
    assert Project.load(project.root).chapters[2].file == new.file


@pytest.mark.parametrize("at", [0, -1, len(PROSE) - 1, len(PROSE) + 5])
def test_split_needs_text_on_both_sides(project, at):
    with pytest.raises(ProjectError):
        project.split_chapter(project.chapters[1].file, at, "Nope")
    assert len(project.chapters) == 3 and len(list(project.chapters_dir.glob("*.md"))) == 3


def test_split_refuses_inside_a_comment(project):
    file = project.chapters[1].file
    text = "Before.\n\n{==a passage==}{>>a note<<}\n\nAfter.\n"
    project.write_chapter(file, text)
    with pytest.raises(ProjectError, match="comment"):
        project.split_chapter(file, text.index("note"), "Nope")
    assert len(project.chapters) == 3


def test_merge_chapter_with_next(project):
    one, two = project.chapters[1], project.chapters[2]
    project.write_chapter(two.file, "\nThe second day.\n")
    project.update_chapter(two.file, synopsis="A second day.")
    project.merge_chapter(one.file)
    assert [c.title for c in project.chapters] == ["Dedication", "Chapter One"]
    assert project.read_chapter(one.file) == PROSE.strip() + "\n\nThe second day.\n"
    assert project.chapters[1].synopsis == "Mara finds a letter. A second day."
    assert (project.root / "trash" / two.file).exists()
    with pytest.raises(ProjectError):
        project.merge_chapter(one.file)  # nothing after the last chapter


def test_split_and_merge_endpoints(tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    pid = c.post("/api/projects", json={"title": "Split Test"}).json()["id"]
    file = c.get(f"/api/projects/{pid}").json()["chapters"][0]["file"]
    text = "One 😀 part.\n\nTwo part.\n"
    c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": text})
    r = c.post(f"/api/projects/{pid}/chapters/{file}/split", json={"at": text.index("Two"), "title": "Two"})
    assert r.status_code == 200
    new = r.json()["file"]
    assert c.get(f"/api/projects/{pid}/chapters/{new}/text").json()["text"] == "Two part.\n"
    assert c.post(f"/api/projects/{pid}/chapters/{file}/split", json={"at": 0}).status_code == 400
    r = c.post(f"/api/projects/{pid}/chapters/{file}/merge")
    assert [ch["file"] for ch in r.json()["chapters"]] == [file]
    assert c.get(f"/api/projects/{pid}/chapters/{file}/text").json()["text"] == "One 😀 part.\n\nTwo part.\n"


def test_restore_deleted_chapter_to_its_place(project):
    one = project.chapters[1]
    project.delete_chapter(one.file)
    project.delete_note("world.md")
    items = sorted(project.list_trash(), key=lambda t: t["type"], reverse=True)
    assert [(t["type"], t["title"]) for t in items] == [("note", "world"), ("chapter", "Chapter One")]
    assert items[1]["words"] > 0
    loaded = Project.load(project.root)
    assert loaded.restore_from_trash(items[1]["name"]) == ("chapter", one.file)
    assert [c.title for c in loaded.chapters] == ["Dedication", "Chapter One", "Chapter Two"]
    assert loaded.chapters[1].synopsis == "Mara finds a letter."
    assert loaded.read_chapter(one.file) == PROSE
    assert loaded.restore_from_trash(items[0]["name"]) == ("note", "world.md")
    assert "world.md" in loaded.list_notes() and loaded.list_trash() == []
    with pytest.raises(ProjectError):
        loaded.restore_from_trash(items[0]["name"])


def test_restore_keeps_both_when_the_name_was_reused(project):
    project.delete_note("world.md")
    project.write_note("world.md", "# New world\n")
    _, name = project.restore_from_trash("world.md")
    assert name == "world-2.md" and project.read_note("world.md") == "# New world\n"


def test_restore_files_deleted_before_the_index(project):
    """Old projects have files in trash/ with no record of what they were."""
    (project.root / "trash").mkdir()
    (project.root / "trash" / "004-the-storm.2.md").write_text("Thunder.")
    (project.root / "trash" / "timeline.md").write_text("# Timeline")
    items = {t["name"]: t for t in project.list_trash()}
    assert items["004-the-storm.2.md"]["type"] == "chapter" and items["004-the-storm.2.md"]["title"] == "The Storm"
    assert items["timeline.md"]["type"] == "note"
    assert project.restore_from_trash("004-the-storm.2.md") == ("chapter", "004-the-storm.md")
    assert project.chapters[-1].title == "The Storm"


def test_trash_endpoints(tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    pid = c.post("/api/projects", json={"title": "Trash Test"}).json()["id"]
    file = c.get(f"/api/projects/{pid}").json()["chapters"][0]["file"]
    assert c.delete(f"/api/projects/{pid}/chapters/{file}").json()["trashed"] == 1
    [item] = c.get(f"/api/projects/{pid}/trash").json()["items"]
    r = c.post(f"/api/projects/{pid}/trash/restore", json={"name": item["name"]}).json()
    assert r["type"] == "chapter" and r["name"] == file and r["project"]["trashed"] == 0
    assert c.post(f"/api/projects/{pid}/trash/restore", json={"name": "../novel.toml"}).status_code == 400
