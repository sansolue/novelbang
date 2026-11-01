from fastapi.testclient import TestClient

from novelbang import notices
from novelbang.server import create_app


def test_only_what_the_app_ships():
    names = {d.metadata["Name"].lower() for d in notices.shipped()}
    assert {"fastapi", "fpdf2", "markdown", "uvicorn"} <= names
    assert not names & {"novelbang", "pytest", "pyinstaller", "pillow-stubs"}  # its own, or only for building


def test_notices_carry_every_license_text():
    text = notices.build_notices()
    assert "(no license text" not in text
    assert "fpdf2" in text and "GNU Lesser General Public License" in text
    assert "Python Software Foundation License" in text and "SIL Open Font License" in text


def test_licenses_endpoint(tmp_path):
    c = TestClient(create_app(tmp_path / "ws", settings_path=tmp_path / "s.toml"), base_url="http://127.0.0.1")
    r = c.get("/api/licenses").json()
    assert r["app"].startswith("MIT License") and "SanSolue" in r["app"]
    assert "Third-party software in NovelBang" in r["third_party"]
