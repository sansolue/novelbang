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
from pathlib import Path

import uvicorn

from .llm import LLMSettings, config_dir
from .server import create_app

log = logging.getLogger("novelbang")

# A fixed port keeps the page's origin, and so its localStorage, the same between launches.
PORT = 47865
OLLAMA_URL = "http://localhost:11434"
# Apps started from Finder don't get the shell's PATH.
EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", "/Applications/Ollama.app/Contents/Resources"]


def log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "NovelBang"
    return config_dir() / "logs"


def setup_logging() -> None:
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stream = open(folder / "novelbang.log", "a", buffering=1, encoding="utf-8")
    # A windowed app has no console; anything printed would otherwise be lost (or crash).
    if sys.stdout is None or not sys.stdout.isatty():
        sys.stdout = sys.stderr = stream
    logging.basicConfig(
        stream=stream, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )


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
        return None
    log.info("Starting %s serve", ollama)
    with open(log_dir() / "ollama.log", "a") as out:
        proc = subprocess.Popen([ollama, "serve"], stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(20):
        if reachable(f"{OLLAMA_URL}/api/version"):
            break
        time.sleep(0.5)
    return proc


def reveal(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


class DesktopAPI:
    """Called from the page as window.pywebview.api.*"""

    def __init__(self, workspace: Path):
        self._workspace = workspace

    def reveal_exports(self, project_id: str) -> bool:
        folder = (self._workspace / project_id / "exports").resolve()
        if folder.parent.parent != self._workspace or not folder.is_dir():
            return False
        reveal(folder)
        return True


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
            done.wait(10)
        except Exception:
            log.exception("Saving before quit failed")
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

    setup_logging()
    workspace = Path(os.environ.get("NOVELBANG_WORKSPACE") or Path.home() / "Novels").expanduser().resolve()
    port = free_port(PORT)
    url = f"http://127.0.0.1:{port}"
    log.info("Starting NovelBang %s on %s with workspace %s", sys.version.split()[0], url, workspace)

    ollama = start_ollama()
    server = uvicorn.Server(
        uvicorn.Config(create_app(workspace), host="127.0.0.1", port=port, log_level="warning", log_config=None)
    )
    thread = threading.Thread(target=server.run, name="server", daemon=True)
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)

    webview.settings["ALLOW_DOWNLOADS"] = True  # export files are saved with a Save dialog
    window = webview.create_window(
        "NovelBang", url, js_api=DesktopAPI(workspace), width=1400, height=900, min_size=(900, 600),
        text_select=True,
    )

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
