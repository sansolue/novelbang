import re
import struct
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from novelbang.export import pdf as pdf_mod
from novelbang.export.docx import approx_words, build_docx, image_size
from novelbang.export.pdf import build_pdf
from novelbang.export.web import build_manuscript_html
from novelbang.importer import import_manuscript, read_markdown, split_chapters
from novelbang.project import ProjectError, Workspace
from novelbang.server import create_app

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def png(w: int, h: int) -> bytes:
    raw = b"".join(b"\x00" + b"\xb4\x5a\x28" * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
def book(tmp_path):
    ws = Workspace(tmp_path / "ws")
    p = ws.create("The Salt Road", "Jane Q. Doe")
    src = p.add_image(png(600, 300), "map.png")
    ded = p.add_chapter("Dedication", "front")
    p.write_chapter(ded.file, "For M.")
    one = p.chapters[0]
    p.update_chapter(one.file, title="The Harbour")
    p.write_chapter(one.file, "Mara watched the *Salt Queen* come\nin.\n\n\"It's late,\" Tom said.[^1]\n\n***\n\n"
                              f"Night came.\n\n![The coast]({src} \"The coast road\")\n\n[^1]: He was late himself.\n")
    two = p.add_chapter("The Road")
    p.write_chapter(two.file, "The road ran north. " * 300)
    p.reorder([ded.file, one.file, two.file])
    return Workspace(tmp_path / "ws").get(p.id)


# ---- Word manuscript ----------------------------------------------------------------

def test_docx_manuscript(book, tmp_path):
    out = build_docx(book, tmp_path)
    z = zipfile.ZipFile(out)
    assert z.testzip() is None
    doc = ET.fromstring(z.read("word/document.xml"))
    styles = [(p.find(f"{W}pPr/{W}pStyle").get(f"{W}val") if p.find(f"{W}pPr/{W}pStyle") is not None else "",
               "".join(t.text or "" for t in p.iter(f"{W}t"))) for p in doc.iter(f"{W}p")]
    assert ("BookTitle", "The Salt Road") in styles and ("Byline", "by Jane Q. Doe") in styles
    assert [t for s, t in styles if s == "Heading1"] == ["Dedication", "Chapter 1: The Harbour", "Chapter 2: The Road"]
    assert ("SceneBreak", "#") in styles and ("Caption", "The coast road") in styles and styles[-1] == ("End", "END")
    assert ("", "Mara watched the Salt Queen come in.") in styles  # the soft line break is a space
    italic = [r for r in doc.iter(f"{W}r") if r.find(f"{W}rPr/{W}i") is not None]
    assert [r.find(f"{W}t").text for r in italic] == ["Salt Queen", "The coast road"]
    assert b"He was late himself." in z.read("word/footnotes.xml") and len(list(doc.iter(f"{W}footnoteReference"))) == 1
    assert "Doe / THE SALT ROAD / " in z.read("word/header1.xml").decode()
    assert 'w:default="1" w:styleId="Normal"' in z.read("word/styles.xml").decode()
    assert any(n.startswith("word/media/image1.png") for n in z.namelist())
    assert "about 1,200 words" in "".join(t for _, t in styles)


def test_approx_words():
    assert [approx_words(n) for n in (40, 949, 1260, 87654)] == [
        "about 100 words", "about 900 words", "about 1,300 words", "about 88,000 words"]


def test_image_sizes(tmp_path):
    (tmp_path / "a.png").write_bytes(png(7, 5))
    (tmp_path / "a.gif").write_bytes(b"GIF89a" + struct.pack("<HH", 9, 4) + b"\0" * 20)
    jpeg = b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9 + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 30, 40) + b"\0" * 10
    (tmp_path / "a.jpg").write_bytes(jpeg)
    assert [image_size(tmp_path / f"a.{e}") for e in ("png", "gif", "jpg")] == [(7, 5), (9, 4), (40, 30)]


# ---- import ------------------------------------------------------------------------

def test_word_round_trip(book, tmp_path):
    """A manuscript NovelBang wrote imports back as the same book."""
    out = build_docx(book, tmp_path)
    p = import_manuscript(Workspace(tmp_path / "again"), out.name, out.read_bytes())
    assert p.meta.title == "The Salt Road"
    assert [(c.title, c.kind) for c in p.chapters] == [("Dedication", "front"), ("The Harbour", "chapter"), ("The Road", "chapter")]
    text = p.read_chapter(p.chapters[1].file)
    assert text.startswith("Mara watched the *Salt Queen* come in.\n\n“It’s late,” Tom said.[^")
    assert "\n\n***\n\nNight came." in text and "]: He was late himself." in text
    assert re.search(r"!\[\]\(\.\./assets/images/image1\.png\)", text)
    assert (p.images_dir / "image1.png").read_bytes() == png(600, 300)
    assert p.meta.number_chapters  # "Chapter 1: " was taken off the titles, so the book numbers them


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


# ---- print PDF -----------------------------------------------------------------------

def pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", pdf))


def test_print_pdf(book, tmp_path):
    out = build_pdf(book, tmp_path)
    data = out.read_bytes()
    assert data.startswith(b"%PDF-") and b"/Title (The Salt Road)" in data or b"The Salt Road" in data
    # title, copyright, dedication (recto 3), blank, chapter 1 (recto 5)…, chapter 2 on a recto
    assert pages(data) >= 7
    assert re.search(rb"/MediaBox\s*\[0(?:\.0+)? 0(?:\.0+)? 432(?:\.0+)? 648(?:\.0+)?\]", data)  # 6 × 9 in
    book.meta.update({"trim_size": "5x8"})
    small = build_pdf(book, tmp_path).read_bytes()
    assert re.search(rb"/MediaBox\s*\[0(?:\.0+)? 0(?:\.0+)? 360(?:\.0+)? 576(?:\.0+)?\]", small)
    assert pages(small) > pages(data)  # the same text on smaller pages


def test_trim_size_is_validated_and_used_by_print_html(book, tmp_path):
    with pytest.raises(ProjectError, match="trim_size"):
        book.meta.update({"trim_size": "7x10"})
    book.meta.update({"trim_size": "5.5x8.5"})
    assert "size: 5.5in 8.5in" in build_manuscript_html(book, tmp_path).read_text()


def test_pdf_characters_the_font_lacks(book, tmp_path, monkeypatch):
    book.write_chapter(book.chapters[1].file, "Ἀχιλλεύς and Жуков.")
    monkeypatch.setattr(pdf_mod, "SYSTEM_FALLBACKS", ())
    with pytest.raises(ProjectError, match="can't print some of the book's characters"):
        build_pdf(book, tmp_path)


def test_import_and_export_api(book, tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    for fmt, suffix in (("docx", "-manuscript.docx"), ("pdf", "-print.pdf")):
        r = c.post(f"/api/projects/{book.id}/export/{fmt}").json()
        assert r["name"].endswith(suffix)
    data = (book.exports_dir / "the-salt-road-manuscript.docx").read_bytes()
    r = c.post("/api/import", params={"name": "salt.docx", "author": "Someone"}, content=data).json()
    assert r["title"] == "The Salt Road" and r["chapter_count"] == 3
    assert r["author"] == "Jane Q. Doe"  # the manuscript's own byline wins over the default
    md = b"# One\n\nText.\n"
    assert c.post("/api/import", params={"name": "a.md", "author": "Someone"}, content=md).json()["author"] == "Someone"
    assert c.post("/api/import", params={"name": "x.doc"}, content=b"x").status_code == 400
