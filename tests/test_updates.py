import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from novelbang import config, updates
from novelbang.server import create_app
from novelbang.updates import UpdateChecker, is_newer, parse_version


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    """Never read or write the real ~/.config/novelbang."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_versions():
    assert parse_version("v0.2.0") == (0, 2, 0) and parse_version("1.10") == (1, 10)
    assert parse_version("0.3.0rc1") is None and parse_version("") is None
    assert is_newer("0.1.10", "0.1.9") and is_newer("v0.2", "0.1.1") and is_newer("1.0.1", "1.0")
    assert not is_newer("0.1.1", "0.1.1") and not is_newer("0.1.0", "0.1.1") and not is_newer("1.0", "1.0.0")
    assert not is_newer("0.3.0rc1", "0.1.0") and not is_newer("garbage", "0.1.0")


@pytest.fixture
def github(monkeypatch):
    """Stands in for GitHub; counts the requests."""
    state = {"calls": 0, "tag": "v9.0.0", "fail": False}

    async def fetch():
        state["calls"] += 1
        if state["fail"]:
            raise httpx.ConnectError("offline")
        return {"version": state["tag"].removeprefix("v"), "url": f"https://github.com/sansolue/novelbang/releases/tag/{state['tag']}"}

    monkeypatch.setattr(updates, "fetch_latest", fetch)
    return state


def test_checks_at_most_once_a_day(tmp_path, github, monkeypatch):
    checker = UpdateChecker(tmp_path / "update.json")
    status = lambda: asyncio.run(checker.status())
    s = status()
    assert s["newer"] and s["latest"] == "9.0.0" and s["url"].endswith("/v9.0.0")
    status()
    assert github["calls"] == 1  # cached
    now = updates.time.time()
    monkeypatch.setattr(updates.time, "time", lambda: now + updates.CHECK_EVERY + 1)
    github["fail"] = True
    s = status()
    assert github["calls"] == 2 and s["latest"] == "9.0.0"  # offline: keeps what it knew
    status()
    assert github["calls"] == 2  # and doesn't retry until tomorrow


def test_older_or_same_release_is_not_offered(tmp_path, github):
    github["tag"] = f"v{updates.__version__}"
    assert asyncio.run(UpdateChecker(tmp_path / "u.json").status())["newer"] is False


def test_fetch_latest_keeps_links_on_github(monkeypatch):
    def handler(request):
        assert request.url == httpx.URL(updates.LATEST_URL)
        assert "NovelBang/" in request.headers["user-agent"]
        return httpx.Response(200, json={"tag_name": "v1.2.3", "html_url": "https://evil.example/x"})

    real = httpx.AsyncClient
    monkeypatch.setattr(updates.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(updates.fetch_latest()) == {"version": "1.2.3", "url": updates.RELEASES_PAGE}


def test_app_preferences_are_merged(tmp_path):
    path = tmp_path / "app.toml"
    config.save_workspace(tmp_path / "novels", path)
    assert config.check_updates(path) is True  # on by default
    config.save_check_updates(False, path)
    config.save_workspace(tmp_path / "other", path)
    assert config.check_updates(path) is False  # saving the folder keeps the other preferences
    assert config.saved_workspace(path) == tmp_path / "other"


def test_update_api(tmp_path, github):
    cli = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    assert cli.get("/api/update").json() == {"supported": False}  # the browser version never checks
    assert cli.put("/api/update", json={"enabled": True}).status_code == 400
    assert github["calls"] == 0

    app = create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml", updates=UpdateChecker(tmp_path / "u.json"))
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.get("/api/update").json()
    assert r["supported"] and r["enabled"] and r["newer"] and r["latest"] == "9.0.0"
    assert c.put("/api/update", json={"enabled": False}).json() == {"supported": True, "enabled": False}
    assert c.get("/api/update").json() == {"supported": True, "enabled": False}
    assert github["calls"] == 1
    assert config.load_app()["check_updates"] is False
    assert json.loads((tmp_path / "u.json").read_text())["version"] == "9.0.0"
