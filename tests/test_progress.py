from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from novelbang import progress
from novelbang.project import ProjectError, Workspace
from novelbang.server import create_app

TODAY = date(2026, 11, 1)


@pytest.fixture
def novel(tmp_path):
    return Workspace(tmp_path / "ws").create("The Salt Road")


def test_record_adds_to_the_day(novel):
    progress.record(novel, 300, TODAY)
    progress.record(novel, -50, TODAY)  # cuts count against the day
    progress.record(novel, 120, TODAY - timedelta(days=1))
    assert progress.load_days(novel) == {"2026-11-01": 250, "2026-10-31": 120}


def test_streak():
    day = lambda n: (TODAY - timedelta(days=n)).isoformat()  # noqa: E731
    days = {day(1): 600, day(2): 500, day(3): 100, day(4): 900}
    assert progress.streak(days, 500, TODAY) == 2  # today not written yet doesn't break it
    assert progress.streak({**days, day(0): 500}, 500, TODAY) == 3
    assert progress.streak(days, 0, TODAY) == 4  # no goal: any writing counts
    assert progress.streak({day(2): 900}, 500, TODAY) == 0  # missed yesterday


def test_summary_and_deadline(novel):
    novel.meta.update({"daily_words": 400, "target_words": 10000, "deadline": "2026-11-10"})
    progress.record(novel, 450, TODAY)
    s = progress.summary(novel, total_words=4000, today=TODAY)
    assert s["today"] == 450 and s["goal"] == 400 and s["streak"] == 1
    assert len(s["recent"]) == 30 and s["recent"][-1] == {"date": "2026-11-01", "words": 450}
    assert s["deadline"] == {"date": "2026-11-10", "days_left": 10, "remaining": 6000, "per_day": 600}
    late = progress.summary(novel, total_words=4000, today=date(2026, 12, 1))["deadline"]
    assert late["days_left"] == 0 and late["per_day"] is None


@pytest.mark.parametrize("changes", [{"deadline": "next week"}, {"daily_words": -5}])
def test_bad_goals_are_refused(novel, changes):
    with pytest.raises(ProjectError):
        novel.meta.update(changes)


def test_only_editor_saves_count(novel, tmp_path):
    c = TestClient(create_app(novel.root.parent, settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    pid, file = novel.id, novel.chapters[0].file
    r = c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": "one two three four"})
    assert r.json()["today"] == 4
    assert c.put(f"/api/projects/{pid}/chapters/{file}/text", json={"text": "one two"}).json()["today"] == 2
    c.post(f"/api/projects/{pid}/replace", json={"query": "two", "replacement": "two and three"})
    p = c.get(f"/api/projects/{pid}").json()
    assert p["words"] == 4 and p["progress"]["today"] == 2  # Replace all isn't writing
    r = c.put(f"/api/projects/{pid}/meta", json={"daily_words": 1000, "deadline": ""})
    assert r.json()["progress"]["goal"] == 1000 and r.json()["progress"]["deadline"] is None
