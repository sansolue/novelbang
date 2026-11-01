"""Installs a new release of the desktop app over the running one, then restarts it.

Only the built apps update themselves, not NovelBang run from source:

- macOS: the DMG is mounted, its NovelBang.app checked (a valid signature, and the
  same developer as this copy when this copy is signed) and swapped in for this one.
- Windows: the installer runs silently, into the same folder, once this app has quit.
  The portable .zip has no installer, so it gets the download link instead.
- Linux: the new AppImage replaces this one.

Every download is checked against the SHA-256 GitHub publishes for the release file.
The running copy keeps its files open, so on macOS and Linux the new one is put in
place straight away; a small helper process then waits for this one to exit, removes
the old copy and starts the new one. The window closes the usual way, saving first.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__
from .updates import RELEASES_PAGE, fetch_latest, is_newer

log = logging.getLogger("novelbang")

DOWNLOADS = f"{RELEASES_PAGE}/download/"  # where release files must come from
LEFTOVER = ".NovelBang-update-"  # prefix of what an interrupted update leaves next to the app


class UpdateError(Exception):
    pass


@dataclass
class Target:
    kind: str  # "macos", "windows" or "appimage"
    path: Path  # the .app bundle, the install folder, or the AppImage file
    asset: str  # the release file to install, with {version} to fill in

    def asset_name(self, version: str) -> str:
        return self.asset.format(version=version)


def find_target(frozen: bool | None = None, executable: str | None = None, system: str | None = None,
                environ: dict | None = None) -> tuple[Target | None, str]:
    """What this copy of NovelBang is, if it can update itself; else why not."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    exe = Path(executable or sys.executable)
    system = system or sys.platform
    environ = os.environ if environ is None else environ
    machine = platform.machine()
    if not frozen:
        return None, "NovelBang is running from source: update it with git pull."
    if system == "darwin":
        app = exe.parents[2]
        if app.suffix != ".app":
            return None, "This NovelBang isn't an app bundle."
        if "/AppTranslocation/" in str(app):
            return None, ("macOS is running NovelBang from a temporary copy. Drag it into Applications and "
                          "open it from there, and it can update itself.")
        if not os.access(app.parent, os.W_OK):
            return None, f"NovelBang can't write to {app.parent}."
        return Target("macos", app, f"NovelBang-{{version}}-{machine}.dmg"), ""
    if system == "win32":
        folder = exe.parent
        if not (folder / "unins000.exe").is_file():
            return None, "This is the portable NovelBang: unzip the new version in place of this one."
        return Target("windows", folder, "NovelBang-{version}-x64-setup.exe"), ""
    if not environ.get("APPIMAGE"):
        return None, "Only the NovelBang AppImage can update itself."
    appimage = Path(environ["APPIMAGE"])
    if not os.access(appimage.parent, os.W_OK):
        return None, f"NovelBang can't write to {appimage.parent}."
    return Target("appimage", appimage, f"NovelBang-{{version}}-{machine}.AppImage"), ""


def support() -> dict:
    t, reason = find_target()
    return {"installable": t is not None, "reason": reason}


def pick_asset(assets: list[dict], name: str) -> tuple[str, str]:
    """The download URL and SHA-256 of the release file called `name`."""
    for a in assets:
        if a.get("name") == name:
            url, digest = a.get("url") or "", a.get("digest") or ""
            if not url.startswith(DOWNLOADS):
                raise UpdateError(f"{name} isn't on NovelBang's release page")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise UpdateError(f"GitHub gives no checksum for {name}, so it can't be checked")
            return url, digest.removeprefix("sha256:")
    raise UpdateError(f"This release has no {name}")


def download(url: str, dest: Path, sha256: str, progress: Callable[[int, int], None] = lambda d, t: None) -> None:
    h, done = hashlib.sha256(), 0
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r, dest.open("wb") as f:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                progress(done, total)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if h.hexdigest() != sha256:
        dest.unlink(missing_ok=True)
        raise UpdateError("The download is damaged: its checksum doesn't match. Try again later.")


def _run(*args: str | Path) -> str:
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    if r.returncode:
        raise UpdateError(f"{Path(str(args[0])).name} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout + r.stderr


def _team(app: Path) -> str | None:
    """The Apple developer team that signed `app`, or None if it's ad-hoc signed."""
    out = subprocess.run(["codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True).stderr
    m = re.search(r"^TeamIdentifier=(\S+)$", out, re.M)
    return m[1] if m and m[1] != "not" else None


def install_macos(dmg: Path, app: Path) -> Path:
    """Swap the app in `dmg` in for `app`; returns where the old copy went."""
    work = Path(tempfile.mkdtemp(prefix="novelbang-update-"))
    mount, tag = work / "mnt", uuid.uuid4().hex[:8]
    new, old = app.with_name(f"{LEFTOVER}{tag}.app"), app.with_name(f"{LEFTOVER}old-{tag}.app")
    mount.mkdir()
    _run("hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, dmg)
    try:
        src = mount / "NovelBang.app"
        if not src.is_dir():
            raise UpdateError("The download has no NovelBang.app in it")
        _run("codesign", "--verify", "--deep", "--strict", src)
        if (team := _team(app)) and _team(src) != team:
            raise UpdateError("The new version isn't signed by the same developer as this one")
        _run("ditto", src, new)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    finally:
        if subprocess.run(["hdiutil", "detach", "-quiet", str(mount)]).returncode:
            subprocess.run(["hdiutil", "detach", "-quiet", "-force", str(mount)])
        shutil.rmtree(work, ignore_errors=True)
    app.rename(old)
    try:
        new.rename(app)
    except BaseException:
        old.rename(app)
        shutil.rmtree(new, ignore_errors=True)
        raise
    return old


def install_appimage(downloaded: Path, appimage: Path) -> None:
    downloaded.chmod(0o755)
    os.replace(downloaded, appimage)  # the running copy keeps the old file open until it exits


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def helper_command(t: Target, pid: int, downloaded: Path | None, old: Path | None,
                   localappdata: str | None = None) -> list[str]:
    """The process that waits for NovelBang `pid` to exit, then finishes the update and starts it."""
    if t.kind == "windows":
        exe = t.path / "NovelBang.exe"
        local = Path(localappdata or os.environ.get("LOCALAPPDATA", ""))
        scope = "/CURRENTUSER" if local.parts and t.path.is_relative_to(local) else "/ALLUSERS"
        script = (
            f"Wait-Process -Id {pid} -ErrorAction SilentlyContinue; "
            f"Start-Process -FilePath {_ps_quote(str(downloaded))} -Wait -ArgumentList "
            f"'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','{scope}'; "
            f"Remove-Item -LiteralPath {_ps_quote(str(downloaded))} -ErrorAction SilentlyContinue; "
            f"Start-Process -FilePath {_ps_quote(str(exe))}"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded]
    launch = ["open", str(t.path)] if t.kind == "macos" else [str(t.path)]
    script = 'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; [ -n "$2" ] && rm -rf "$2"; shift 2; exec "$@"'
    return ["/bin/sh", "-c", script, "novelbang-update", str(pid), str(old or ""), *launch]


def _clean_env() -> dict[str, str]:
    """This process's environment as it was before the app's own launcher changed it."""
    env = dict(os.environ)
    if "LD_LIBRARY_PATH_ORIG" in env:  # set aside by PyInstaller
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    else:
        env.pop("LD_LIBRARY_PATH", None)
    for name in ("APPIMAGE", "APPDIR", "ARGV0", "OWD"):  # the new AppImage sets its own
        env.pop(name, None)
    return env


def spawn(cmd: list[str]) -> None:
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen(cmd, creationflags=flags, close_fds=True, env=_clean_env())
    else:
        subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=_clean_env())


def clean_leftovers() -> None:
    """Remove what an update that never restarted left beside the app."""
    t, _ = find_target()
    if not t or t.kind == "windows":
        return
    for path in t.path.parent.glob(f"{LEFTOVER}*"):
        log.info("Removing %s, left by an earlier update", path.name)
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)


class Updater:
    """Downloads and installs the latest release in the background; the page polls status()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {"state": "idle"}
        self._finish: list[str] | None = None

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **state) -> None:
        with self._lock:
            self._state = state

    def start(self) -> dict:
        with self._lock:
            if self._state["state"] in ("checking", "downloading", "installing", "ready"):
                return dict(self._state)
            self._state = {"state": "checking"}
        threading.Thread(target=self._run, name="update", daemon=True).start()
        return self.status()

    def _run(self) -> None:
        try:
            t, reason = find_target()
            if not t:
                raise UpdateError(reason)
            release = asyncio.run(fetch_latest())
            version = release["version"]
            if not is_newer(version):
                raise UpdateError(f"NovelBang {__version__} is already the latest version")
            url, sha256 = pick_asset(release.get("assets", []), t.asset_name(version))
            # Beside the AppImage, so it can be renamed over it; elsewhere a temporary folder.
            folder = t.path.parent if t.kind == "appimage" else Path(tempfile.mkdtemp(prefix="novelbang-update-"))
            dest = folder / f"{LEFTOVER}{uuid.uuid4().hex[:8]}-{t.asset_name(version)}"
            log.info("Updating to %s: downloading %s", version, t.asset_name(version))
            self._set(state="downloading", version=version, done=0, total=0)
            download(url, dest, sha256, lambda d, n: self._set(state="downloading", version=version, done=d, total=n))
            self._set(state="installing", version=version)
            old = None
            if t.kind == "macos":
                old = install_macos(dest, t.path)
                dest.unlink(missing_ok=True)
            elif t.kind == "appimage":
                install_appimage(dest, t.path)
            self._finish = helper_command(t, os.getpid(), dest if t.kind == "windows" else None, old)
            log.info("Update to %s ready; it finishes when NovelBang restarts", version)
            self._set(state="ready", version=version)
        except UpdateError as e:
            log.warning("Update failed: %s", e)
            self._set(state="failed", error=str(e))
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.warning("Update failed: %s: %s", type(e).__name__, e)
            self._set(state="failed", error="The update couldn't be downloaded. Check your connection and try again.")
        except Exception:
            log.exception("Update crashed")
            self._set(state="failed", error="The update failed. Details are in the log.")

    def restart(self) -> bool:
        """Start the helper that finishes the update once the app has quit."""
        if self._finish is None:
            return False
        spawn(self._finish)
        return True
