import os
import socket

import pytest
from fastapi.testclient import TestClient

from novelbang import config, desktop
from novelbang.project import Workspace
from novelbang.server import create_app


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    """Never read or write the real ~/.config/novelbang."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("NOVELBANG_WORKSPACE", raising=False)


def test_free_port_skips_a_busy_port():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert desktop.free_port(port) != port
    assert desktop.free_port(port) == port  # free again once closed


def test_reveal_exports_stays_in_the_workspace(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "reveal", opened.append)
    p = Workspace(tmp_path).create("Ash")
    p.exports_dir.mkdir(exist_ok=True)
    api = desktop.DesktopAPI(Workspace(tmp_path.resolve()), first_run=False)
    assert api.reveal_exports(p.id) is True
    assert opened == [p.exports_dir.resolve()]
    (tmp_path / "exports").mkdir()
    for bad in ("..", "../..", "/etc", f"{p.id}/..", "missing"):
        assert api.reveal_exports(bad) is False
    assert len(opened) == 1


def test_workspace_choice_is_remembered(tmp_path, monkeypatch):
    assert config.saved_workspace() is None
    assert config.default_workspace() == config.Path.home() / "Novels"
    config.save_workspace(tmp_path / "My Novels")
    assert config.default_workspace() == tmp_path / "My Novels"
    monkeypatch.setenv("NOVELBANG_WORKSPACE", str(tmp_path / "env"))
    assert config.default_workspace() == tmp_path / "env"  # the environment wins


def test_first_run_setup_switches_the_live_server(tmp_path):
    default = tmp_path / "Novels"
    app = create_app(default, settings_path=tmp_path / "s.toml")
    c = TestClient(app, base_url="http://127.0.0.1")
    api = desktop.DesktopAPI(app.state.workspace, first_run=True)
    assert api.workspace_setup_needed()

    chosen = tmp_path / "Writing"
    Workspace(chosen).create("Salt Road")
    assert api._use(chosen) == {"workspace": str(chosen.resolve()), "note": ""}
    assert not api.workspace_setup_needed()
    assert not default.exists()  # the empty default made before setup is tidied away
    assert config.saved_workspace() == chosen.resolve()
    listing = c.get("/api/projects").json()
    assert listing["workspace"] == str(chosen.resolve())
    assert [p["title"] for p in listing["projects"]] == ["Salt Road"]


def test_choosing_a_novel_uses_its_parent_folder(tmp_path):
    novel = Workspace(tmp_path / "Writing").create("Ash").root
    api = desktop.DesktopAPI(Workspace(tmp_path / "Novels"), first_run=False)
    r = api._use(novel)
    assert r["workspace"] == str((tmp_path / "Writing").resolve()) and "Ash" not in r["workspace"]
    assert "is a novel" in r["note"]
    assert (tmp_path / "Novels").is_dir()  # not first run: the old folder is left alone


def test_keeping_the_default_and_unwritable_folders(tmp_path):
    ws = Workspace(tmp_path / "Novels")
    Workspace(ws.root).create("Kept")
    api = desktop.DesktopAPI(ws, first_run=True)
    # chmod can't make a folder read-only on Windows, and root can write anywhere.
    if os.name != "nt" and os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            assert "can't write" in api._use(locked)["error"]
        finally:
            locked.chmod(0o700)
        assert api.workspace_setup_needed() and ws.root == tmp_path / "Novels"
    assert api.keep_workspace() == str(tmp_path / "Novels")
    assert config.saved_workspace() == tmp_path / "Novels" and not api.workspace_setup_needed()
