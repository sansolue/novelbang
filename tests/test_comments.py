import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from novelbang import comments, prompts
from novelbang.export.docx import build_docx
from novelbang.export.epub import build_epub
from novelbang.export.web import build_markdown, build_serial_pack
from novelbang.importer import import_manuscript
from novelbang.project import Workspace, count_words, fingerprint
from novelbang.server import create_app

TEXT = ("Mara {==watched the ship==}{>>too passive?<<} come in {>>check the tide<<}.\n\n"
        "{>>A paragraph of its own<<}\n\nShe {==ran==}.")


def test_find_strip_and_count():
    found = comments.find(TEXT)
    assert [(c["passage"], c["note"]) for c in found] == [
        ("watched the ship", "too passive?"), ("", "check the tide"), ("", "A paragraph of its own"), ("ran", "")]
    assert comments.count(TEXT) == 4 and comments.count("No comments.") == 0
    assert comments.strip(TEXT) == "Mara watched the ship come in.\n\n\n\nShe ran."
    # one comment inside another's passage goes with it
    assert comments.strip("{==a {>>x<<} b==}{>>y<<} c") == "a b c"
    # a passage doesn't reach across to a later comment
    assert comments.strip("{==one==} two {==three==}{>>n<<}") == "one two three"


def test_unclosed_and_resolve():
    assert not comments.unclosed(TEXT)
    assert comments.unclosed("She ran {>>check this") and comments.unclosed("{==half a highlight")
    assert comments.resolve(TEXT, 1, "{>>check the tide<<}").startswith(
        "Mara {==watched the ship==}{>>too passive?<<} come in.\n\n")
    assert comments.resolve(TEXT, 0, "{==watched the ship==}{>>too passive?<<}").startswith("Mara watched the ship come in {>>")
    with pytest.raises(ValueError):
        comments.resolve(TEXT, 0, "{>>check the tide<<}")  # the list was out of date
    with pytest.raises(ValueError):
        comments.resolve(TEXT, 9, "")


def test_comments_dont_count_or_age_a_synopsis():
    plain = comments.strip(TEXT)
    assert count_words(TEXT) == count_words(plain) == 8
    assert fingerprint(TEXT) == fingerprint(plain.replace("\n\n\n\n", "\n\n"))


@pytest.fixture
def book(tmp_path):
    ws = Workspace(tmp_path / "ws")
    p = ws.create("The Salt Road", "Jane Doe")
    p.write_chapter(p.chapters[0].file, TEXT)
    two = p.add_chapter("Two")
    p.write_chapter(two.file, "Plain text, no notes.")
    return ws.get(p.id)


def test_exports_leave_comments_out(book, tmp_path):
    def unzip(path):
        z = zipfile.ZipFile(path)
        return "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith((".xhtml", ".xml", ".html", ".md")))

    outputs = {
        "epub": unzip(build_epub(book, tmp_path)),
        "docx": unzip(build_docx(book, tmp_path)),
        "markdown": build_markdown(book, tmp_path).read_text(),
        "serial": unzip(build_serial_pack(book, tmp_path)),
    }
    for fmt, text in outputs.items():
        assert "watched the ship" in text, fmt
        assert "passive" not in text and "tide" not in text and "{==" not in text and "{>>" not in text, fmt


def test_the_co_writer_doesnt_see_comments(book):
    file = book.chapters[0].file
    msgs = prompts.continue_messages(book, file, TEXT, "", "", 8000)
    assert "watched the ship" in msgs[1]["content"] and "tide" not in msgs[1]["content"]
    msgs = prompts.summarize_messages(book, file, TEXT, 8000)
    assert "tide" not in msgs[1]["content"]
    # …but chat does, so the author can talk a comment over
    assert "tide" in prompts.chat_messages(book, file, TEXT, [], 8000)[0]["content"]


def test_comments_api(book, tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    pid, file = book.id, book.chapters[0].file
    project = c.get(f"/api/projects/{pid}").json()
    assert [ch["comments"] for ch in project["chapters"]] == [4, 0]
    r = c.get(f"/api/projects/{pid}/comments").json()
    assert r["total"] == 4 and [d["name"] for d in r["docs"]] == [file]
    first = r["docs"][0]["comments"][0]
    r = c.post(f"/api/projects/{pid}/comments/resolve", json={"file": file, "index": 0, "raw": first["raw"]})
    assert r.status_code == 200 and r.json()["chapters"][0]["comments"] == 3
    assert book.read_chapter(file).startswith("Mara watched the ship come in {>>check the tide<<}.")
    stale = c.post(f"/api/projects/{pid}/comments/resolve", json={"file": file, "index": 0, "raw": first["raw"]})
    assert stale.status_code == 409
    checks = [i["message"] for i in c.get(f"/api/projects/{pid}/publish").json()["checks"]]
    assert "3 comments are in the text. Comments are left out of every export." in checks
    book.write_chapter(file, "She ran {>>check this")
    checks = [i["message"] for i in c.get(f"/api/projects/{pid}/publish").json()["checks"]]
    assert any("has a comment that isn't finished" in m for m in checks)


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def ref(cid: int) -> str:
    return f'<w:commentRangeEnd w:id="{cid}"/><w:r><w:commentReference w:id="{cid}"/></w:r>'


def test_word_comments_are_imported(tmp_path):
    body = "".join([
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' + run("One") + "</w:p>",
        # a comment on a passage, and one inside it
        "<w:p>" + run("Mara ") + '<w:commentRangeStart w:id="0"/>' + run("watched ")
        + '<w:commentRangeStart w:id="1"/>' + run("the") + ref(1) + run(" ship") + ref(0) + run(" come in.") + "</w:p>",
        # a comment over two paragraphs
        '<w:p><w:commentRangeStart w:id="2"/>' + run("Night came.") + "</w:p>",
        "<w:p>" + run("Then dawn.") + ref(2) + "</w:p>",
        # a point comment, with only its reference
        "<w:p>" + run("End.") + '<w:r><w:commentReference w:id="3"/></w:r></w:p>',
    ])
    doc = f'<w:document {W}><w:body>{body}</w:body></w:document>'
    notes = "".join(f'<w:comment w:id="{i}" w:author="Ed"><w:p>{run(t)}</w:p></w:comment>'
                    for i, t in enumerate(["Too passive?", "Which ship?", "Too fast", "Cut &lt;&lt;} this?"]))
    styles = f'<w:styles {W}><w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>'
    data = BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/comments.xml", f"<w:comments {W}>{notes}</w:comments>")
    p = import_manuscript(Workspace(tmp_path), "draft.docx", data.getvalue())
    text = p.read_chapter(p.chapters[0].file)
    assert text == (
        "Mara {==watched the ship==}{>>Too passive?<<}{>>Which ship?<<} come in.\n\n"
        "{==Night came.==}{>>Too fast<<}\n\nThen dawn.\n\nEnd.{>>Cut << } this?<<}\n"
    )
    assert not comments.unclosed(text) and comments.count(text) == 4
