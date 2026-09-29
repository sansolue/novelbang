from fastapi.testclient import TestClient

from novelbang import prompts
from novelbang.project import Workspace, change_ratio, fingerprint
from novelbang.server import create_app

PARAS = [f"Paragraph {i}: " + " ".join(f"word{i}_{j}" for j in range(20)) for i in range(8)]
TEXT = "\n\n".join(PARAS)


def test_change_ratio():
    fp = fingerprint(TEXT)
    assert change_ratio(fp, TEXT) == 0
    # Whitespace-only reflow doesn't count as a change.
    assert change_ratio(fp, TEXT.replace(" ", "  ").replace("\n\n", "\n \n")) == 0
    one_edited = "\n\n".join(PARAS[:-1] + [PARAS[-1] + " extra"])
    assert 0.1 < change_ratio(fp, one_edited) < 0.2
    assert change_ratio(fp, "\n\n".join(PARAS[:4])) == 0.5  # half cut
    assert change_ratio(fp, "Something else entirely.") == 1.0


def test_synopsis_states(tmp_path):
    p = Workspace(tmp_path).create("Salt")
    f = p.chapters[0].file
    state = lambda: p.chapter_stats()[f]["synopsis_state"]  # noqa: E731

    assert state() == "none"  # empty chapter needs no synopsis
    p.write_chapter(f, TEXT)
    assert state() == "missing"
    p.update_chapter(f, synopsis="Things happen.")
    assert state() == "fresh"
    p.write_chapter(f, "\n\n".join(PARAS[:-1] + [PARAS[-1] + " small fix"]))
    assert state() == "fresh"  # minor edits don't nag
    p.write_chapter(f, "\n\n".join(PARAS[:4] + ["A brand new ending " + "x " * 200]))
    assert state() == "stale"
    p.update_chapter(f, synopsis="Things happen differently.")
    assert state() == "fresh"  # saving a synopsis re-anchors it to the current text
    # Synopses written by hand in novel.toml (no fingerprint) are trusted.
    (p.root / ".novelbang" / "synopses.json").unlink()
    p.write_chapter(f, "Rewritten. " * 100)
    assert state() == "fresh"


def test_summarize_prompt_has_context_and_ending(tmp_path):
    p = Workspace(tmp_path).create("Salt")
    one = p.chapters[0].file
    p.update_chapter(one, synopsis="Mara finds the letter.")
    two = p.add_chapter("Two").file
    long_text = "OPENING " + "middle " * 3000 + " FINAL LINE"
    p.write_chapter(two, long_text)

    msg = prompts.summarize_messages(p, two, long_text, 6000)[1]["content"]
    assert "Mara finds the letter." in msg
    assert "OPENING" in msg and "FINAL LINE" in msg and "omitted" in msg
    assert len(msg) < 8000
    # Batch overrides replace stored synopses of earlier chapters.
    msg = prompts.summarize_messages(p, two, long_text, 6000, {one: "Mara burns the letter."})[1]["content"]
    assert "Mara burns the letter." in msg and "finds" not in msg


def test_continue_sees_previous_chapter_ending(tmp_path):
    p = Workspace(tmp_path).create("Salt")
    one = p.chapters[0].file
    p.write_chapter(one, "Long ago. " * 50 + "She stepped off the pier into the dark.")
    two = p.add_chapter("Two").file
    msg = prompts.continue_messages(p, two, "", "", "", 8000)[1]["content"]
    assert "End of the previous chapter (Chapter One)" in msg
    assert "stepped off the pier" in msg
    # Deep into a chapter, the previous ending isn't needed.
    msg = prompts.continue_messages(p, two, "x " * 2000, "", "", 8000)[1]["content"]
    assert "End of the previous chapter" not in msg


def test_api_reports_synopsis_state(tmp_path):
    with TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1") as c:
        p = c.post("/api/projects", json={"title": "Ash"}).json()
        pid, f = p["id"], p["chapters"][0]["file"]
        c.put(f"/api/projects/{pid}/chapters/{f}/text", json={"text": TEXT})
        assert c.get(f"/api/projects/{pid}").json()["chapters"][0]["synopsis_state"] == "missing"
        r = c.put(f"/api/projects/{pid}/chapters/{f}", json={"synopsis": "Stuff."}).json()
        assert r["chapters"][0]["synopsis_state"] == "fresh"
        c.put(f"/api/projects/{pid}/chapters/{f}/text", json={"text": "All new. " * 100})
        assert c.get(f"/api/projects/{pid}").json()["chapters"][0]["synopsis_state"] == "stale"
        # Renaming a chapter doesn't re-anchor its synopsis.
        r = c.put(f"/api/projects/{pid}/chapters/{f}", json={"title": "Renamed"}).json()
        assert r["chapters"][0]["synopsis_state"] == "stale"
