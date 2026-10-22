import logging

import pytest
from fastapi.testclient import TestClient

from novelbang.history import git_available
from novelbang.project import ProjectError, Workspace
from novelbang.search import find, pattern, replace, replace_in, search
from novelbang.server import create_app


def spans(text, query, **opts):
    return [text[s:e] for s, e in find(text, pattern(query, **opts))]


def test_options():
    text = "Mara met MARA. Marathon? mara."
    assert spans(text, "mara") == ["Mara", "MARA", "Mara", "mara"]
    assert spans(text, "Mara", match_case=True) == ["Mara", "Mara"]
    assert spans(text, "mara", whole_word=True) == ["Mara", "MARA", "mara"]
    assert spans("Mara's coat", "Mara", whole_word=True) == ["Mara"]
    assert spans("Zoë and zoë", "ZOË", whole_word=True) == ["Zoë", "zoë"]


def test_quotes_match_either_way():
    text = "“Don’t,” she said. \"Don't.\""
    assert spans(text, "don't") == ["Don’t", "Don't"]
    assert spans(text, '"Don') == ["“Don", '"Don']
    assert spans(text, "Don’t") == ["Don’t", "Don't"]


def test_regex_characters_are_literal():
    assert spans("Cost (2.5) or 205?", "(2.5)") == ["(2.5)"]
    assert spans("a.b axb", "a.b") == ["a.b"]


def test_picture_paths_are_left_alone():
    text = '![The map](../assets/images/map.jpg "A map of the coast")\n\nShe drew a map.'
    assert spans(text, "map") == ["map", "map", "map"]  # alt text, caption, prose
    new, n = replace(text, pattern("map"), "chart")
    assert n == 3
    assert new == '![The chart](../assets/images/map.jpg "A chart of the coast")\n\nShe drew a chart.'


def test_bad_queries():
    with pytest.raises(ProjectError):
        pattern("")
    with pytest.raises(ProjectError):
        pattern("x" * 501)


@pytest.fixture
def project(tmp_path):
    p = Workspace(tmp_path).create("The Salt Road")
    one = p.chapters[0].file
    p.write_chapter(one, "Mara walked.\nThen Mara ran, and Mara hid.")
    p.update_chapter(one, synopsis="Mara runs away.")
    two = p.add_chapter("Two").file
    p.write_chapter(two, "Nobody here.")
    p.write_note("characters.md", "Mara: the heroine.")
    return p, one, two


def test_search_project(project):
    p, one, _ = project
    r = search(p, pattern("mara"))
    assert r["total"] == 5
    assert [(d["type"], d["name"], d["count"]) for d in r["docs"]] == [("chapter", one, 4), ("note", "characters.md", 1)]
    hits = r["docs"][0]["hits"]
    assert [h["field"] for h in hits] == ["text", "text", "text", "synopsis"]
    # Context stops at line breaks.
    assert (hits[1]["before"], hits[1]["match"], hits[1]["after"]) == ("Then ", "Mara", " ran, and Mara hid.")


def test_replace_in_project(project):
    p, one, two = project
    r = replace_in(p, pattern("Mara", whole_word=True), "Mira", only={("chapter", one), ("chapter", two)})
    assert r == {"replaced": 4, "changed": [{"type": "chapter", "name": one}]}
    assert p.read_chapter(one) == "Mira walked.\nThen Mira ran, and Mira hid."
    assert p.chapter(one).synopsis == "Mira runs away."
    assert p.read_note("characters.md") == "Mara: the heroine."  # not chosen
    assert type(p).load(p.root).chapter(one).synopsis == "Mira runs away."  # saved


def test_search_and_replace_api(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    p = c.post("/api/projects", json={"title": "Ash"}).json()
    pid, file = p["id"], p["chapters"][0]["file"]
    c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": "QUILLON fought. Quillon won."})
    r = c.get(f"/api/projects/{pid}/search", params={"q": "quillon", "match_case": True}).json()
    assert r["total"] == 0
    r = c.get(f"/api/projects/{pid}/search", params={"q": "quillon"}).json()
    assert r["total"] == 2 and r["docs"][0]["title"]
    assert c.get(f"/api/projects/{pid}/search", params={"q": ""}).status_code == 400

    r = c.post(f"/api/projects/{pid}/replace", json={"query": "Quillon", "replacement": "Tamsin", "match_case": True}).json()
    assert r["replaced"] == 1 and r["project"]["words"] == 4
    assert c.get(f"/api/projects/{pid}/chapters/{file}/text").json()["text"] == "QUILLON fought. Tamsin won."
    r = c.post(f"/api/projects/{pid}/replace", json={"query": "QUILLON", "replacement": "X", "docs": []}).json()
    assert r["replaced"] == 0
    assert "QUILLON" not in caplog.text and "Tamsin" not in caplog.text  # the writing stays out of the log

    # A version is saved first, so History can undo a replace.
    if git_available():
        versions = c.get(f"/api/projects/{pid}/history", params={"type": "chapter", "name": file}).json()["versions"]
        before = next(v for v in versions if "Before replacing “Quillon” with “Tamsin”" in v["message"])
        old = c.get(f"/api/projects/{pid}/history/{before['id']}", params={"type": "chapter", "name": file}).json()
        assert old["text"] == "QUILLON fought. Quillon won."
