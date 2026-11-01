import base64
import hashlib
import subprocess
import sys
import threading
import time

import httpx
import pytest

from novelbang import installer
from novelbang.installer import Target, UpdateError

URL = "https://github.com/sansolue/novelbang/releases/download/v9.0.0/"


def test_targets(tmp_path):
    assert installer.find_target(frozen=False)[0] is None
    app = tmp_path / "Applications" / "NovelBang.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    t, _ = installer.find_target(True, str(app / "Contents/MacOS/NovelBang"), "darwin")
    assert t.kind == "macos" and t.path == app and t.asset_name("9.0.0").startswith("NovelBang-9.0.0-")
    moved = "/private/var/folders/x/AppTranslocation/ABC/d/NovelBang.app/Contents/MacOS/NovelBang"
    t, reason = installer.find_target(True, moved, "darwin")
    assert t is None and "Applications" in reason

    folder = tmp_path / "Programs" / "NovelBang"
    folder.mkdir(parents=True)
    t, reason = installer.find_target(True, str(folder / "NovelBang.exe"), "win32")
    assert t is None and "portable" in reason
    (folder / "unins000.exe").touch()
    t, _ = installer.find_target(True, str(folder / "NovelBang.exe"), "win32")
    assert t.kind == "windows" and t.asset_name("9.0.0") == "NovelBang-9.0.0-x64-setup.exe"

    image = tmp_path / "NovelBang.AppImage"
    assert installer.find_target(True, "/tmp/x/NovelBang", "linux", {})[0] is None
    t, _ = installer.find_target(True, "/tmp/x/NovelBang", "linux", {"APPIMAGE": str(image)})
    assert t.kind == "appimage" and t.path == image and t.asset_name("9.0.0").endswith(".AppImage")


def test_pick_asset():
    digest = "sha256:" + "a" * 64
    assets = [{"name": "NovelBang-9.0.0-x64-setup.exe", "url": URL + "NovelBang-9.0.0-x64-setup.exe", "digest": digest}]
    assert installer.pick_asset(assets, "NovelBang-9.0.0-x64-setup.exe") == (assets[0]["url"], "a" * 64)
    with pytest.raises(UpdateError, match="no NovelBang-9.0.0-arm64.dmg"):
        installer.pick_asset(assets, "NovelBang-9.0.0-arm64.dmg")
    elsewhere = [{**assets[0], "url": "https://example.com/NovelBang-9.0.0-x64-setup.exe"}]
    with pytest.raises(UpdateError, match="release page"):
        installer.pick_asset(elsewhere, assets[0]["name"])
    with pytest.raises(UpdateError, match="checksum"):
        installer.pick_asset([{**assets[0], "digest": None}], assets[0]["name"])


def serve(monkeypatch, body: bytes):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    monkeypatch.setattr(installer.httpx, "stream", client.stream)


def test_download_checks_the_checksum(tmp_path, monkeypatch):
    serve(monkeypatch, b"new version")
    seen = []
    installer.download(URL + "x", tmp_path / "ok", hashlib.sha256(b"new version").hexdigest(), lambda d, t: seen.append(d))
    assert (tmp_path / "ok").read_bytes() == b"new version" and seen[-1] == 11
    with pytest.raises(UpdateError, match="damaged"):
        installer.download(URL + "x", tmp_path / "bad", "0" * 64)
    assert not (tmp_path / "bad").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the helper is a POSIX shell script")
def test_helper_waits_then_cleans_up_and_launches(tmp_path):
    image = tmp_path / "NovelBang.AppImage"
    marker = tmp_path / "launched"
    image.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    image.chmod(0o755)
    old = tmp_path / ".NovelBang-update-old"
    old.mkdir()
    app = subprocess.Popen(["sleep", "0.5"])
    # Reap it when it exits, as the app's own parent would: a zombie still answers kill -0.
    threading.Thread(target=app.wait, daemon=True).start()
    start = time.time()
    subprocess.run(installer.helper_command(Target("appimage", image, ""), app.pid, None, old), check=True, timeout=10)
    assert time.time() - start >= 0.4  # it waited for the app to quit
    assert marker.exists() and not old.exists()


def test_windows_helper_runs_the_installer_after_exit(tmp_path):
    local = tmp_path / "AppData" / "Local"
    t = Target("windows", local / "Programs" / "NovelBang", "")
    cmd = installer.helper_command(t, 4242, tmp_path / "it's-setup.exe", None, localappdata=str(local))
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    assert script.startswith("Wait-Process -Id 4242")
    assert "'/CURRENTUSER'" in script and "it''s-setup.exe" in script and "NovelBang.exe" in script
    elsewhere = installer.helper_command(Target("windows", tmp_path / "Program Files" / "NovelBang", ""), 1,
                                         tmp_path / "s.exe", None, localappdata=str(local))
    assert "'/ALLUSERS'" in base64.b64decode(elsewhere[-1]).decode("utf-16-le")


@pytest.mark.skipif(sys.platform == "win32", reason="AppImages are Linux only; Windows has no execute bit")
def test_appimage_is_replaced(tmp_path):
    image, new = tmp_path / "NovelBang.AppImage", tmp_path / ".NovelBang-update-x"
    image.write_text("old")
    new.write_text("new")
    installer.install_appimage(new, image)
    assert image.read_text() == "new" and image.stat().st_mode & 0o111 and not new.exists()


def make_app(path, version):
    (path / "Contents" / "MacOS").mkdir(parents=True)
    exe = path / "Contents" / "MacOS" / "NovelBang"
    exe.write_text(f"#!/bin/sh\necho {version}\n")
    exe.chmod(0o755)
    (path / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        "<key>CFBundleExecutable</key><string>NovelBang</string>"
        f"<key>CFBundleIdentifier</key><string>com.sansolue.novelbang</string></dict></plist>")
    subprocess.run(["codesign", "--force", "--sign", "-", str(path)], check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="needs hdiutil and codesign")
def test_macos_install_swaps_the_app(tmp_path):
    apps = tmp_path / "Applications"
    current = apps / "NovelBang.app"
    make_app(current, "old")
    stage = tmp_path / "stage"
    make_app(stage / "NovelBang.app", "new")
    dmg = tmp_path / "update.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-volname", "NovelBang", "-srcfolder", str(stage),
                    "-format", "UDZO", str(dmg)], check=True)
    old = installer.install_macos(dmg, current)
    assert "new" in (current / "Contents/MacOS/NovelBang").read_text()
    assert old.parent == apps and old.name.startswith(installer.LEFTOVER) and "old" in (old / "Contents/MacOS/NovelBang").read_text()
    assert sorted(p.name for p in apps.iterdir()) == sorted(["NovelBang.app", old.name])  # nothing else left


@pytest.mark.skipif(sys.platform != "darwin", reason="needs hdiutil")
def test_macos_install_refuses_a_dmg_without_the_app(tmp_path):
    current = tmp_path / "Applications" / "NovelBang.app"
    make_app(current, "old")
    (tmp_path / "stage").mkdir()
    (tmp_path / "stage" / "readme.txt").write_text("hi")
    dmg = tmp_path / "update.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-srcfolder", str(tmp_path / "stage"), "-format", "UDZO", str(dmg)],
                   check=True)
    with pytest.raises(UpdateError, match="no NovelBang.app"):
        installer.install_macos(dmg, current)
    assert "old" in (current / "Contents/MacOS/NovelBang").read_text()
    assert [p.name for p in current.parent.iterdir()] == ["NovelBang.app"]


def test_updater_end_to_end_for_an_appimage(tmp_path, monkeypatch):
    image = tmp_path / "NovelBang.AppImage"
    image.write_text("old")
    body = b"the new AppImage"
    target = Target("appimage", image, "NovelBang-{version}-x86_64.AppImage")
    monkeypatch.setattr(installer, "find_target", lambda: (target, ""))

    async def latest():
        name = "NovelBang-9.0.0-x86_64.AppImage"
        return {"version": "9.0.0", "assets": [{"name": name, "url": URL + name,
                                                 "digest": "sha256:" + hashlib.sha256(body).hexdigest()}]}

    monkeypatch.setattr(installer, "fetch_latest", latest)
    serve(monkeypatch, body)
    u = installer.Updater()
    u.start()
    for _ in range(100):
        if u.status()["state"] in ("ready", "failed"):
            break
        time.sleep(0.02)
    assert u.status() == {"state": "ready", "version": "9.0.0"}
    assert image.read_bytes() == body
    assert not list(tmp_path.glob(installer.LEFTOVER + "*"))


def test_updater_says_why_it_cant(monkeypatch):
    monkeypatch.setattr(installer, "find_target", lambda: (None, "This is the portable NovelBang"))
    u = installer.Updater()
    u.start()
    for _ in range(100):
        if u.status()["state"] == "failed":
            break
        time.sleep(0.02)
    assert u.status() == {"state": "failed", "error": "This is the portable NovelBang"}
    assert u.restart() is False
