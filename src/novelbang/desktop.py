"""NovelBang as a desktop app: the same server, shown in a native window.

The server runs on a background thread and the UI loads in the system web view
(WebKit on macOS, Edge WebView2 on Windows), so nothing opens in a browser.
Needs the `desktop` extra:  uv run --extra desktop novelbang-desktop
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

import uvicorn

from .config import default_workspace, save_workspace, saved_workspace
from .llm import LLMSettings, config_dir
from .logs import log_dir, log_path, setup_logging
from .project import Project, Workspace
from .server import create_app

log = logging.getLogger("novelbang")

# A fixed port keeps the page's origin, and so its localStorage, the same between launches.
PORT = 47865
OLLAMA_URL = "http://localhost:11434"
# Apps started from Finder don't get the shell's PATH.
EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", "/Applications/Ollama.app/Contents/Resources"]


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            # As uvicorn does, so connections left from the last run (TIME_WAIT) don't block the port.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise RuntimeError("No free port")


def reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1).close()
        return True
    except OSError:
        return False


def start_ollama() -> subprocess.Popen | None:
    """Start `ollama serve` if the co-writer is set to use Ollama and it isn't running yet."""
    if os.environ.get("NOVELBANG_NO_OLLAMA"):
        return None
    if not LLMSettings.load().base_url.startswith(("http://localhost:11434", "http://127.0.0.1:11434")):
        return None
    if reachable(f"{OLLAMA_URL}/api/version"):
        return None
    path = os.pathsep.join([os.environ.get("PATH", ""), *EXTRA_PATHS])
    ollama = shutil.which("ollama", path=path)
    if not ollama:
        log.info("Ollama isn't running and isn't installed; the co-writer needs a model server")
        return None
    log.info("Starting %s serve (its output: %s)", ollama, log_dir() / "ollama.log")
    with open(log_dir() / "ollama.log", "a") as out:
        proc = subprocess.Popen([ollama, "serve"], stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(20):
        if reachable(f"{OLLAMA_URL}/api/version"):
            break
        time.sleep(0.5)
    else:
        log.warning("Ollama didn't answer within 10 s of starting (exit code %s)", proc.poll())
    return proc


def reveal(path: Path) -> None:
    """Open a folder, or show a file selected in its folder."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)] if path.is_file() else ["open", str(path)], check=False)
    elif sys.platform == "win32":
        if path.is_file():
            subprocess.run(["explorer", f"/select,{path}"], check=False)
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path.parent if path.is_file() else path)], check=False)


class DesktopAPI:
    """Called from the page as window.pywebview.api.* (only public methods are exposed)."""

    def __init__(self, workspace: Workspace, first_run: bool):
        self._ws = workspace
        self._first_run = first_run  # no folder chosen yet: the page asks where novels should live
        self._window = None  # set once the window exists

    def reveal_exports(self, project_id: str) -> bool:
        root = self._ws.root.resolve()
        folder = (root / project_id / "exports").resolve()
        if folder.parent.parent != root or not folder.is_dir():
            return False
        reveal(folder)
        return True

    def reveal_log(self) -> bool:
        if not log_path().is_file():
            return False
        reveal(log_path())
        return True

    def workspace_setup_needed(self) -> bool:
        return self._first_run

    def keep_workspace(self) -> str:
        """Settle on the current folder (the first-run default)."""
        save_workspace(self._ws.root)
        self._first_run = False
        return str(self._ws.root)

    def choose_workspace(self) -> dict | None:
        """Pick the folder of novels with the system folder dialog; None if cancelled."""
        import webview

        picked = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(self._ws.root))
        if not picked:
            return None
        return self._use(Path(picked[0] if isinstance(picked, (list, tuple)) else picked))

    def _use(self, path: Path) -> dict:
        path, note = path.resolve(), ""
        if Project.is_project(path):
            # An easy slip: picking one novel's folder rather than the folder of novels.
            note = f"“{path.name}” is a novel, so NovelBang uses the folder it's in: {path.parent}"
            path = path.parent
        if not os.access(path, os.W_OK):
            return {"error": f"NovelBang can't write to {path}. Choose a folder of your own."}
        old = self._ws.root
        self._ws.move_to(path)
        save_workspace(path)
        if self._first_run and old.resolve() != path:
            with suppress(OSError):
                old.rmdir()  # the default folder made before setup, if it's still empty
        self._first_run = False
        return {"workspace": str(path), "note": note}


def save_on_close(window) -> None:
    """Closing the window (or quitting) first lets the page save unsaved text, then closes."""
    closing = {"saved": False}

    def save_then_close() -> None:
        done, ok = threading.Event(), {"value": True}

        def finished(result):
            ok["value"] = result is not False
            done.set()

        try:
            window.evaluate_js("flushSave().then(() => !state.dirty)", finished)
            if not done.wait(10):
                log.warning("The page didn't confirm its save within 10 s; closing anyway")
        except Exception:
            log.exception("Saving before quit failed")
        if not ok["value"]:
            log.warning("Unsaved changes when quitting; asking whether to quit anyway")
        if ok["value"] or window.create_confirmation_dialog(
            "Quit without saving?", "NovelBang couldn't save your latest changes."
        ):
            closing["saved"] = True
            window.destroy()

    def on_closing() -> bool:
        if closing["saved"]:
            return True
        # evaluate_js would deadlock on this (the UI) thread, so save from another one.
        threading.Thread(target=save_then_close, daemon=True).start()
        return False

    window.events.closing += on_closing


def main() -> None:
    import webview

    setup_logging(console=False)
    workspace = default_workspace().expanduser().resolve()
    first_run = not os.environ.get("NOVELBANG_WORKSPACE") and saved_workspace() is None
    port = free_port(PORT)
    url = f"http://127.0.0.1:{port}"
    log.info("Desktop app on %s%s", url, "" if port == PORT else f" (port {PORT} was busy)")

    ollama = start_ollama()
    app = create_app(workspace)
    api = DesktopAPI(app.state.workspace, first_run)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    thread = threading.Thread(target=server.run, name="server", daemon=True)
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)

    webview.settings["ALLOW_DOWNLOADS"] = True  # export files are saved with a Save dialog
    window = webview.create_window(
        "NovelBang", url, js_api=api, width=1400, height=900, min_size=(900, 600),
        text_select=True,
    )

    api._window = window
    save_on_close(window)
    webview.start(private_mode=False, storage_path=str(config_dir() / "webview"))

    # Window closed: stop the server cleanly so its final history snapshot is taken.
    server.should_exit = True
    thread.join(timeout=15)
    if ollama and ollama.poll() is None:
        ollama.terminate()
    log.info("NovelBang closed")


if __name__ == "__main__":
    main()
