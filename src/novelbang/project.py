"""A novel project is a plain folder of files:

    My Novel/
      novel.toml          metadata + chapter order (the only structured file)
      chapters/*.md       one Markdown file per chapter, prose only
      notes/*.md          outline, characters, world, style guide, anything else
      assets/cover.jpg    optional cover image (jpg or png)
      exports/            generated ebooks and web bundles
      trash/              deleted chapters/notes are moved here, never erased

Everything is human-readable and diff-friendly, so a project can live in git
or a synced folder and be edited with any text editor.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import tomllib
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import tomli_w

PROJECT_FILE = "novel.toml"
CHAPTER_KINDS = ("front", "chapter", "back")
CHAPTER_STATUSES = ("idea", "draft", "revised", "final")
COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png")
DEFAULT_NOTES = {
    "outline.md": "# Outline\n\nThe story beat by beat.\n",
    "characters.md": "# Characters\n\n## Protagonist\n\n- Name:\n- Wants:\n- Needs:\n- Flaw:\n",
    "world.md": "# World\n\nSetting, rules, places, history.\n",
    "style.md": (
        "# Style guide\n\n"
        "The AI co-writer reads this before every request.\n\n"
        "- Point of view: third person limited\n"
        "- Tense: past\n"
        "- Voice: \n"
    ),
}

_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,120}\.md$")
_WORD = re.compile(r"\b[\w'’-]+\b")


class ProjectError(Exception):
    pass


def slugify(text: str, fallback: str = "untitled") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:60] or fallback


def count_words(text: str) -> int:
    return len(_WORD.findall(text))


def convert_field(current: object, value: object) -> object:
    """Convert `value` to the type of a dataclass field's `current` value, or raise ValueError."""
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("1", "true", "on", "yes", "0", "false", "off", "no", ""):
            return value.lower() in ("1", "true", "on", "yes")
    elif isinstance(current, (int, float)):
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            try:
                return type(current)(value)
            except (TypeError, ValueError, OverflowError):
                pass
    elif isinstance(current, str):
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return str(value)
    raise ValueError(f"expected {type(current).__name__}, got {value!r}")


def check_filename(name: str) -> str:
    """Reject anything that could escape the project folder."""
    if not _SAFE_FILE.match(name) or ".." in name:
        raise ProjectError(f"Invalid file name: {name!r}")
    return name


@dataclass
class Metadata:
    title: str = "Untitled"
    subtitle: str = ""
    author: str = ""
    language: str = "en"
    description: str = ""
    genre: str = ""
    keywords: str = ""
    publisher: str = ""
    rights: str = ""
    isbn: str = ""
    series: str = ""
    series_index: str = ""
    identifier: str = field(default_factory=lambda: f"urn:uuid:{uuid.uuid4()}")
    number_chapters: bool = True
    target_words: int = 80000

    @classmethod
    def from_dict(cls, data: dict) -> Metadata:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def update(self, data: dict) -> None:
        """Apply user edits; unknown keys and the identifier are ignored, bad values rejected."""
        changes = {}
        for f in fields(self):
            if f.name == "identifier" or data.get(f.name) is None:
                continue
            try:
                changes[f.name] = convert_field(getattr(self, f.name), data[f.name])
            except ValueError as e:
                raise ProjectError(f"Invalid {f.name}: {e}") from None
        for key, value in changes.items():
            setattr(self, key, value)


@dataclass
class Chapter:
    file: str
    title: str
    kind: str = "chapter"
    status: str = "draft"
    synopsis: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Chapter:
        known = {f.name for f in fields(cls)}
        ch = cls(**{k: v for k, v in data.items() if k in known})
        check_filename(ch.file)
        if ch.kind not in CHAPTER_KINDS:
            ch.kind = "chapter"
        if ch.status not in CHAPTER_STATUSES:
            ch.status = "draft"
        return ch


class Project:
    def __init__(self, root: Path, meta: Metadata, chapters: list[Chapter]):
        self.root = root
        self.meta = meta
        self.chapters = chapters

    # ---- paths -------------------------------------------------------------

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def chapters_dir(self) -> Path:
        return self.root / "chapters"

    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    # ---- load / save -------------------------------------------------------

    @classmethod
    def is_project(cls, path: Path) -> bool:
        return (path / PROJECT_FILE).is_file()

    @classmethod
    def load(cls, root: Path) -> Project:
        path = root / PROJECT_FILE
        if not path.is_file():
            raise ProjectError(f"No {PROJECT_FILE} in {root}")
        data = tomllib.loads(path.read_text("utf-8"))
        meta = Metadata.from_dict(data.get("book", {}))
        chapters = [Chapter.from_dict(c) for c in data.get("chapters", [])]
        project = cls(root, meta, chapters)
        project._adopt_orphans()
        return project

    @classmethod
    def create(cls, workspace: Path, title: str, author: str = "") -> Project:
        base = slugify(title, "novel")
        root = workspace / base
        n = 2
        while root.exists():
            root = workspace / f"{base}-{n}"
            n += 1
        for sub in ("chapters", "notes", "assets", "exports"):
            (root / sub).mkdir(parents=True)
        for name, body in DEFAULT_NOTES.items():
            (root / "notes" / name).write_text(body, "utf-8")
        (root / ".gitignore").write_text("exports/\ntrash/\n", "utf-8")
        project = cls(root, Metadata(title=title or "Untitled", author=author), [])
        project.add_chapter("Chapter One")
        return project

    def save(self) -> None:
        data = {
            "book": asdict(self.meta),
            "chapters": [asdict(c) for c in self.chapters],
        }
        _atomic_write(self.root / PROJECT_FILE, tomli_w.dumps(data))

    def _adopt_orphans(self) -> None:
        """Markdown files dropped into chapters/ by hand join the end of the book.

        Nothing is written here: loading happens on every read, and a chapter file
        can be briefly absent (git checkout, a sync client, an editor's
        delete-and-rename save). Entries for missing files are kept so their title,
        synopsis and position survive; adopted files are persisted by the next save.
        """
        if not self.chapters_dir.is_dir():
            return
        known = {c.file for c in self.chapters}
        for path in sorted(self.chapters_dir.glob("*.md")):
            if path.name not in known and _SAFE_FILE.match(path.name):
                title = path.stem.lstrip("0123456789-_ ").replace("-", " ").strip()
                self.chapters.append(Chapter(file=path.name, title=title.title() or path.stem))

    # ---- chapters ----------------------------------------------------------

    def chapter(self, file: str) -> Chapter:
        for c in self.chapters:
            if c.file == file:
                return c
        raise ProjectError(f"No chapter {file!r}")

    def add_chapter(self, title: str, kind: str = "chapter", after: str | None = None) -> Chapter:
        # Resolve `after` before touching the disk so a bad value leaves no stray file.
        idx = [c.file for c in self.chapters].index(self.chapter(after).file) + 1 if after else len(self.chapters)
        self.chapters_dir.mkdir(exist_ok=True)
        slug = slugify(title, "chapter")
        n = len(self.chapters) + 1
        while True:
            name = f"{n:03d}-{slug}.md"
            if not (self.chapters_dir / name).exists():
                break
            n += 1
        (self.chapters_dir / name).write_text("", "utf-8")
        ch = Chapter(file=name, title=title, kind=kind if kind in CHAPTER_KINDS else "chapter")
        self.chapters.insert(idx, ch)
        self.save()
        return ch

    def update_chapter(self, file: str, **changes) -> Chapter:
        ch = self.chapter(file)
        for key in ("title", "kind", "status", "synopsis"):
            if key in changes and changes[key] is not None:
                setattr(ch, key, str(changes[key]))
        if ch.kind not in CHAPTER_KINDS:
            ch.kind = "chapter"
        if ch.status not in CHAPTER_STATUSES:
            ch.status = "draft"
        self.save()
        return ch

    def is_missing(self, file: str) -> bool:
        return not (self.chapters_dir / file).is_file()

    def read_chapter(self, file: str) -> str:
        self.chapter(file)
        if self.is_missing(file):
            raise ProjectError(f"Chapter file chapters/{file} is missing")
        return (self.chapters_dir / file).read_text("utf-8")

    def write_chapter(self, file: str, text: str) -> None:
        self.chapter(file)
        _atomic_write(self.chapters_dir / file, text)

    def delete_chapter(self, file: str) -> None:
        ch = self.chapter(file)
        self._to_trash(self.chapters_dir / ch.file)
        self.chapters.remove(ch)
        self.save()

    def reorder(self, files: list[str]) -> None:
        by_file = {c.file: c for c in self.chapters}
        if sorted(files) != sorted(by_file):
            raise ProjectError("Reorder must list every chapter exactly once")
        self.chapters = [by_file[f] for f in files]
        self.save()

    def numbered(self) -> list[tuple[Chapter, int | None]]:
        """Chapters paired with their printed number (front/back matter unnumbered)."""
        out, n = [], 0
        for c in self.chapters:
            if c.kind == "chapter":
                n += 1
                out.append((c, n))
            else:
                out.append((c, None))
        return out

    # ---- notes -------------------------------------------------------------

    def list_notes(self) -> list[str]:
        if not self.notes_dir.is_dir():
            return []
        return sorted(p.name for p in self.notes_dir.glob("*.md"))

    def read_note(self, name: str) -> str:
        path = self.notes_dir / check_filename(name)
        return path.read_text("utf-8") if path.is_file() else ""

    def write_note(self, name: str, text: str) -> None:
        self.notes_dir.mkdir(exist_ok=True)
        _atomic_write(self.notes_dir / check_filename(name), text)

    def delete_note(self, name: str) -> None:
        self._to_trash(self.notes_dir / check_filename(name))

    # ---- cover -------------------------------------------------------------

    def cover_path(self) -> Path | None:
        for name in COVER_NAMES:
            p = self.assets_dir / name
            if p.is_file():
                return p
        return None

    def set_cover(self, data: bytes, ext: str) -> Path:
        ext = ext.lower().lstrip(".")
        if ext not in ("jpg", "jpeg", "png"):
            raise ProjectError("Cover must be a JPEG or PNG image")
        self.assets_dir.mkdir(exist_ok=True)
        for name in COVER_NAMES:
            (self.assets_dir / name).unlink(missing_ok=True)
        path = self.assets_dir / f"cover.{'jpg' if ext == 'jpeg' else ext}"
        path.write_bytes(data)
        return path

    # ---- stats & checks ----------------------------------------------------

    def word_counts(self) -> dict[str, int]:
        return {
            c.file: 0 if self.is_missing(c.file) else count_words(self.read_chapter(c.file))
            for c in self.chapters
        }

    def publish_checks(self) -> list[dict]:
        """Things a reader-facing store (KDP, Kobo, Google Play, Royal Road...) will want."""
        issues: list[dict] = []

        def add(level: str, msg: str) -> None:
            issues.append({"level": level, "message": msg})

        m = self.meta
        if not m.title.strip() or m.title == "Untitled":
            add("error", "Book has no title.")
        if not m.author.strip():
            add("error", "Author name is missing.")
        if not m.description.strip():
            add("warn", "No description/blurb — stores display this on the book page.")
        if not m.language.strip():
            add("error", "Language code is missing (e.g. en, en-GB, fr).")
        if not self.cover_path():
            add("warn", "No cover image. Most stores want a JPEG around 1600×2560 px.")
        if not m.rights.strip():
            add("info", "No copyright line set (e.g. “© 2026 Jane Doe. All rights reserved.”).")
        if not m.isbn.strip():
            add("info", "No ISBN. Fine for KDP/web serials; some stores and print need one.")
        body = [c for c in self.chapters if c.kind == "chapter"]
        if not body:
            add("error", "The manuscript has no chapters.")
        counts = self.word_counts()
        for c in self.chapters:
            if self.is_missing(c.file):
                add("error", f"“{c.title}” has no file (chapters/{c.file} is missing).")
            elif counts[c.file] == 0:
                add("warn", f"“{c.title}” is empty.")
            elif c.status in ("idea", "draft"):
                add("info", f"“{c.title}” is still marked {c.status}.")
        return issues

    # ---- internal ----------------------------------------------------------

    def _to_trash(self, path: Path) -> None:
        if not path.exists():
            return
        trash = self.root / "trash"
        trash.mkdir(exist_ok=True)
        dest = trash / path.name
        n = 2
        while dest.exists():
            dest = trash / f"{path.stem}.{n}{path.suffix}"
            n += 1
        shutil.move(path, dest)


class Workspace:
    """A directory holding many novel projects."""

    def __init__(self, root: Path, on_change: Callable[[Project], None] | None = None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.on_change = on_change  # called after every successful edit() block
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def edit(self, project_id: str) -> Iterator[Project]:
        """Load a project for a change, serialising writers so load→modify→save can't interleave."""
        with self._locks_guard:
            lock = self._locks.setdefault(project_id, threading.Lock())
        with lock:
            project = self.get(project_id)
            yield project
        if self.on_change:
            self.on_change(project)

    def list(self) -> list[Project]:
        projects = []
        for path in sorted(self.root.iterdir()):
            if path.is_dir() and Project.is_project(path):
                try:
                    projects.append(Project.load(path))
                except Exception:  # a broken novel.toml shouldn't hide the others
                    continue
        return projects

    def get(self, project_id: str) -> Project:
        if "/" in project_id or "\\" in project_id or project_id.startswith("."):
            raise ProjectError("Invalid project id")
        path = self.root / project_id
        if not Project.is_project(path):
            raise ProjectError(f"No project {project_id!r}")
        return Project.load(path)

    def create(self, title: str, author: str = "") -> Project:
        return Project.create(self.root, title, author)


def _atomic_write(path: Path, text: str) -> None:
    # A unique temp name per write, so concurrent writers never clobber each other's file.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)  # usual umask-based mode
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
