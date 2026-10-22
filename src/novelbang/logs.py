"""One log for the desktop app, the command line and the browser UI, for debugging.

It records what happened, when, and how long it took, but never what you wrote:
no chapter text, prompts, model replies or API keys. That keeps it safe to attach
to a bug report. The file rotates, so it stays under ~10 MB.

    NOVELBANG_DEBUG=1     also log every HTTP request
    NOVELBANG_LOG_DIR=…   write the log somewhere else
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
from collections.abc import Iterable
from pathlib import Path

from . import __version__

log = logging.getLogger("novelbang")

LOG_FILE = "novelbang.log"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_HANDLER_NAME = "novelbang-file"


_WORD = re.compile(r"\w+")
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\[nrt\"\\/]")
SHINGLE = 3  # words in a row that count as a quote


def _decoded(text: str) -> str:
    """`text` plus the strings of any JSON in it, escapes decoded: servers echo requests as JSON."""
    parts = [_ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else " ", text)]
    start = text.find("{")
    if start >= 0:
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            data = None

        def walk(v):
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        walk(data)
    return "\n".join(parts)


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = [w.lower() for w in _WORD.findall(text)]
    return {tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def quotes_writing(message: str, writing: Iterable[str]) -> bool:
    """Whether `message` repeats any of `writing` (prompts sent, text received)."""
    decoded = _decoded(message)
    said, lowered = _shingles(decoded), decoded.lower()
    for text in writing:
        if not text or not text.strip():
            continue
        if len(_WORD.findall(text)) < SHINGLE:
            # Too short for shingles (an instruction like "darker"): look for it whole.
            if text.strip().lower() in lowered:
                return True
        elif said & _shingles(text):
            return True
    return False


def without_writing(message: str, writing: Iterable[str], limit: int = 400) -> str:
    """`message` made safe for the log: on one line, and left out if it quotes the writing."""
    if quotes_writing(message, writing):
        return f"[{len(message)} characters left out: they quote the text sent to or received from the model]"
    message = " ".join(message.split())
    return message[:limit] + ("…" if len(message) > limit else "")


def log_dir() -> Path:
    if env := os.environ.get("NOVELBANG_LOG_DIR"):
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "NovelBang"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "NovelBang" / "Logs"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "novelbang"


def log_path() -> Path:
    return log_dir() / LOG_FILE


class _LogWriter:
    """Stands in for stdout/stderr in a windowed app, which has no console."""

    def __init__(self, level: int):
        self.level = level
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            if line.strip():
                logging.getLogger("novelbang.output").log(self.level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def setup_logging(console: bool = True) -> Path:
    """Log to the file (and, with `console`, warnings to the terminal). Safe to call twice."""
    root = logging.getLogger()
    path = log_path()
    if any(h.get_name() == _HANDLER_NAME for h in root.handlers):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    debug = bool(os.environ.get("NOVELBANG_DEBUG"))
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    file = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    file.set_name(_HANDLER_NAME)
    file.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file)
    if console and sys.stderr is not None:
        term = logging.StreamHandler()
        term.setLevel(logging.WARNING)
        term.setFormatter(logging.Formatter("NovelBang %(levelname)s: %(message)s"))
        root.addHandler(term)
    if sys.stdout is None or sys.stderr is None:
        sys.stdout, sys.stderr = _LogWriter(logging.INFO), _LogWriter(logging.ERROR)

    # Libraries that are chatty at INFO: one line per HTTP call.
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Even in debug mode, other libraries stay at INFO: only our own debug lines are vetted.
    for name in ("pywebview", "uvicorn", "asyncio", "multipart"):
        logging.getLogger(name).setLevel(logging.INFO)
    logging.captureWarnings(True)

    def excepthook(kind, value, tb):
        if issubclass(kind, KeyboardInterrupt):
            return sys.__excepthook__(kind, value, tb)
        log.critical("Uncaught exception", exc_info=(kind, value, tb))

    def thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.critical("Uncaught exception in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook

    os_name = f"macOS {platform.mac_ver()[0]}" if sys.platform == "darwin" else platform.platform()
    log.info(
        "NovelBang %s starting (Python %s, %s, %s)%s",
        __version__, platform.python_version(), os_name, platform.machine(), " [debug]" if debug else "",
    )
    return path
