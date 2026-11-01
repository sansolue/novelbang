import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never read or write the real ~/.config/novelbang, e.g. the backup folder set there."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
