import socket

from novelbang import desktop
from novelbang.project import Workspace


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
    api = desktop.DesktopAPI(tmp_path.resolve())
    assert api.reveal_exports(p.id) is True
    assert opened == [p.exports_dir.resolve()]
    (tmp_path / "exports").mkdir()
    for bad in ("..", "../..", "/etc", f"{p.id}/..", "missing"):
        assert api.reveal_exports(bad) is False
    assert len(opened) == 1
