"""Version history for a project, kept in a private git repository.

The repository lives in `<project>/.novelbang/history.git` with the project
folder as its work tree. It is separate from any `.git` the author keeps
themselves, so automatic snapshots never mix into their own history, and it
ignores their global git config (hooks, signing, aliases) so it can't break.
Anyone who wants to dig deeper can still use normal git tools:

    git --git-dir=.novelbang/history.git --work-tree=. log
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .project import ProjectError, count_words

GIT_DIR = ".novelbang/history.git"
EXCLUDES = """\
/exports/
/trash/
/.novelbang/
/.git/
.*.tmp
.DS_Store
"""
KINDS = ("auto", "session", "checkpoint", "milestone", "restore")
_SUBJECT = re.compile(r"^\[([a-z-]+)\] ?(.*)$")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


class HistoryError(ProjectError):
    pass


def git_available() -> bool:
    return shutil.which("git") is not None


@dataclass
class Version:
    id: str
    time: int
    kind: str
    message: str
    words: int | None = None  # words in the requested file at this version
    delta: int | None = None  # change in words vs the previous listed version


class ProjectHistory:
    def __init__(self, root: Path):
        self.root = root
        self.git_dir = root / GIT_DIR
        self._lock = threading.Lock()

    # ---- git plumbing ------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_DIR=str(self.git_dir),
            GIT_WORK_TREE=str(self.root),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="NovelBang",
            GIT_AUTHOR_EMAIL="history@novelbang.local",
            GIT_COMMITTER_NAME="NovelBang",
            GIT_COMMITTER_EMAIL="history@novelbang.local",
        )
        return env

    def _git(self, *args: str, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
        if not git_available():
            raise HistoryError("Version history needs git installed (https://git-scm.com).")
        r = subprocess.run(
            ["git", *args], cwd=self.root, env=self._env(), input=input, capture_output=True
        )
        if check and r.returncode != 0:
            raise HistoryError(f"git {args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
        return r

    def _ensure(self) -> None:
        if (self.git_dir / "HEAD").is_file():
            return
        meta = self.root / ".novelbang"
        meta.mkdir(exist_ok=True)
        # Keep .novelbang/ out of the author's own git repo, if they have one.
        (meta / ".gitignore").write_text("*\n", "utf-8")
        self._git("init", "-q")
        self._git("config", "core.quotepath", "false")
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "exclude").write_text(EXCLUDES, "utf-8")

    def _has_commits(self) -> bool:
        return self._git("rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0

    # ---- snapshots ---------------------------------------------------------

    def snapshot(
        self,
        kind: str = "auto",
        message: str = "",
        describe: Callable[[list[tuple[str, str]]], str] | None = None,
        allow_empty: bool = False,
    ) -> str | None:
        """Commit the project's current state. Returns the commit id, or None if nothing changed."""
        if kind not in KINDS:
            raise HistoryError(f"Unknown snapshot kind {kind!r}")
        with self._lock:
            self._ensure()
            self._git("add", "-A")
            changes = self._staged()
            if not changes and not allow_empty:
                return None
            if not message:
                message = describe(changes) if describe else "Saved changes"
            self._git("commit", "-q", "--allow-empty", "--no-verify", "-m", f"[{kind}] {message}".strip())
            return self._git("rev-parse", "HEAD").stdout.decode().strip()

    def _staged(self) -> list[tuple[str, str]]:
        out = self._git("diff", "--cached", "--name-status", "--no-renames", "-z").stdout.decode()
        parts = out.split("\0")
        return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]

    # ---- reading history ---------------------------------------------------

    def versions(self, path: str, limit: int = 300) -> list[Version]:
        """Versions of one file, newest first. Milestones are always listed."""
        with self._lock:
            if not (self.git_dir / "HEAD").is_file() or not self._has_commits():
                return []
            touching = set(self._git("log", "--format=%H", "--", path).stdout.decode().split())
            out = self._git("log", f"-n{limit * 4}", "--format=%H%x1f%at%x1f%s").stdout.decode()
            versions = []
            for line in out.splitlines():
                sha, ts, subject = line.split("\x1f", 2)
                m = _SUBJECT.match(subject)
                kind, message = (m.group(1), m.group(2)) if m else ("auto", subject)
                if sha in touching or kind == "milestone":
                    versions.append(Version(sha, int(ts), kind, message))
                if len(versions) >= limit:
                    break
            texts = self._cat_many([f"{v.id}:{path}" for v in versions])
        for v, text in zip(versions, texts):
            v.words = None if text is None else count_words(text)
        # delta against the next-older listed version that had the file
        for i, v in enumerate(versions):
            older = next((o for o in versions[i + 1 :] if o.words is not None), None)
            if v.words is not None:
                v.delta = v.words - older.words if older else v.words
        # Milestones that didn't change this file still show, but without a delta.
        for i, v in enumerate(versions):
            if v.id not in touching:
                v.delta = None
        return versions

    def read(self, commit: str, path: str) -> str | None:
        if not _COMMIT.match(commit):
            raise HistoryError("Invalid version id")
        with self._lock:
            return self._cat_many([f"{commit}:{path}"])[0]

    def timestamp(self, commit: str) -> int:
        if not _COMMIT.match(commit):
            raise HistoryError("Invalid version id")
        with self._lock:
            r = self._git("show", "-s", "--format=%at", commit, check=False)
        if r.returncode != 0:
            raise HistoryError("Unknown version")
        return int(r.stdout)

    def _cat_many(self, specs: list[str]) -> list[str | None]:
        """Fetch many blobs with one git process."""
        if not specs:
            return []
        out = self._git("cat-file", "--batch", input=("\n".join(specs) + "\n").encode()).stdout
        results, pos = [], 0
        for _ in specs:
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            pos = end + 1
            if len(header) == 3 and header[1] == b"blob":
                size = int(header[2])
                results.append(out[pos : pos + size].decode("utf-8", errors="replace"))
                pos += size + 1
            else:  # "<spec> missing" — the file didn't exist in that version
                results.append(None)
        return results


def word_diff(old: str, new: str) -> list[tuple[str, str]]:
    """Diff two texts as [(op, text)] with op in '=', '-', '+'.

    Paragraphs are matched first, then changed paragraphs are compared word by
    word, which keeps this fast on long chapters and the output readable.
    """
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    out: list[tuple[str, str]] = []

    def emit(op: str, text: str) -> None:
        if not text:
            return
        if out and out[-1][0] == op:
            out[-1] = (op, out[-1][1] + text)
        else:
            out.append((op, text))

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            emit("=", "".join(a[i1:i2]))
        elif tag == "delete":
            emit("-", "".join(a[i1:i2]))
        elif tag == "insert":
            emit("+", "".join(b[j1:j2]))
        else:
            wa = re.findall(r"\s+|[^\s]+", "".join(a[i1:i2]))
            wb = re.findall(r"\s+|[^\s]+", "".join(b[j1:j2]))
            for t, x1, x2, y1, y2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
                if t == "equal":
                    emit("=", "".join(wa[x1:x2]))
                else:
                    emit("-", "".join(wa[x1:x2]))
                    emit("+", "".join(wb[y1:y2]))
    return out


class HistoryTracker:
    """Decides when to take automatic snapshots.

    A project with unsaved-to-history edits is snapshotted once the author has
    been idle for IDLE seconds, or every MAX_INTERVAL seconds during long
    uninterrupted sessions.
    """

    IDLE = 60
    MAX_INTERVAL = 300

    def __init__(self) -> None:
        self._pending: dict[Path, tuple[float, float]] = {}  # root -> (first change, last change)
        self._seen: set[Path] = set()
        self._lock = threading.Lock()

    def touched(self, root: Path) -> None:
        now = time.monotonic()
        with self._lock:
            first, _ = self._pending.get(root, (now, now))
            self._pending[root] = (first, now)

    def first_visit(self, root: Path) -> bool:
        with self._lock:
            if root in self._seen:
                return False
            self._seen.add(root)
            return True

    def due(self, force: bool = False) -> list[Path]:
        now = time.monotonic()
        with self._lock:
            ready = [
                root
                for root, (first, last) in self._pending.items()
                if force or now - last >= self.IDLE or now - first >= self.MAX_INTERVAL
            ]
            for root in ready:
                del self._pending[root]
            return ready
