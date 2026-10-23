"""A novel project is a plain folder of files:

    My Novel/
      novel.toml          metadata + chapter order (the only structured file)
      chapters/*.md       one Markdown file per chapter, prose only
      notes/*.md          outline, characters, world, style guide, anything else
      assets/cover.jpg    optional cover image (jpg or png)
      assets/images/      pictures used in chapters: ![alt](../assets/images/map.jpg "caption")
      exports/            generated ebooks and web bundles
      trash/              deleted chapters/notes are moved here, never erased

Everything is human-readable and diff-friendly, so a project can live in git
or a synced folder and be edited with any text editor.
"""

from __future__ import annotations

import hashlib
import json
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
from urllib.parse import unquote

import tomli_w

PROJECT_FILE = "novel.toml"
ARCHIVE_DIR = "Archived novels"
TRASH_DIR = "Deleted novels"
CHAPTER_KINDS = ("front", "chapter", "back")
CHAPTER_STATUSES = ("idea", "draft", "revised", "final")
# Common paperback trim sizes (width × height in inches) that KDP, IngramSpark and Lulu all print.
TRIM_SIZES = {"5x8": (5.0, 8.0), "5.25x8": (5.25, 8.0), "5.5x8.5": (5.5, 8.5), "6x9": (6.0, 9.0)}
COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png")
# Pictures every ebook store and reader accepts (EPUB core media types).
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}
IMAGES_DIR = "images"  # under assets/
# Above this, a picture is probably straight off a camera and worth resizing.
LARGE_IMAGE_BYTES = 2 * 1024 * 1024
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

# Where NovelBang keeps its own bookkeeping inside a project (hidden from the author's git).
META_DIR = ".novelbang"
SYNOPSIS_STATE = "synopses.json"
# A synopsis is out of date once this share of the chapter's text is new or gone.
STALE_RATIO = 0.25
# Chapters shorter than this don't need a synopsis.
SYNOPSIS_MIN_WORDS = 50

_PARA_SPLIT = re.compile(r"\n\s*\n")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,120}\.md$")
_WORD = re.compile(r"\b[\w'’-]+\b")
# Inline Markdown images: ![alt](src "title"); group 1 is the src.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'(][^)]*)?\)")


class ProjectError(Exception):
    pass


def slugify(text: str, fallback: str = "untitled") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:60] or fallback


def count_words(text: str) -> int:
    return len(_WORD.findall(_MD_IMAGE.sub(" ", text)))


def image_refs(text: str) -> list[str]:
    """The src of every inline Markdown image in `text`."""
    return _MD_IMAGE.findall(text)


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
    # The type, not the value: these messages reach the log, and values can be a blurb or an API key.
    raise ValueError(f"expected {type(current).__name__}, got {type(value).__name__}")


def fingerprint(text: str) -> dict:
    """A compact record of a chapter's paragraphs, to tell later how much it has changed."""
    paras: dict[str, int] = {}
    for p in _PARA_SPLIT.split(text):
        if p := " ".join(p.split()):
            key = hashlib.sha1(p.encode()).hexdigest()[:12]
            paras[key] = paras.get(key, 0) + len(p)
    return {"words": count_words(text), "paras": paras}


def change_ratio(old: dict, text: str) -> float:
    """Share of the text that is new or gone since `old` was taken (0 = same, 1 = rewritten)."""
    new = fingerprint(text)["paras"]
    before = old.get("paras", {})
    total_new, total_old = sum(new.values()), sum(before.values())
    if not total_new and not total_old:
        return 0.0
    added = sum(n for k, n in new.items() if k not in before)
    removed = sum(n for k, n in before.items() if k not in new)
    return max(added / (total_new or 1), removed / (total_old or 1))


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
    trim_size: str = "6x9"  # printed page, in inches: the PDF and print HTML exports
    picture_size: int = 1600  # long side, in px, pictures are shrunk to when added (0 = keep)

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
        if changes.get("trim_size", self.trim_size) not in TRIM_SIZES:
            raise ProjectError(f"Invalid trim_size: choose one of {', '.join(TRIM_SIZES)}")
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
        root = _free_path(workspace, slugify(title, "novel"))
        for sub in ("chapters", "notes", "assets", "exports"):
            (root / sub).mkdir(parents=True)
        for name, body in DEFAULT_NOTES.items():
            (root / "notes" / name).write_text(body, "utf-8", newline="\n")
        (root / ".gitignore").write_text("exports/\ntrash/\n", "utf-8", newline="\n")
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
        if changes.get("synopsis") is not None and not self.is_missing(file):
            # Whoever saved the synopsis vouches for it against the text as it is now.
            self._record_synopsis(file, self.read_chapter(file))
        return ch

    # ---- synopsis freshness -----------------------------------------------

    def _synopsis_path(self) -> Path:
        return self.root / META_DIR / SYNOPSIS_STATE

    def _synopsis_fingerprints(self) -> dict[str, dict]:
        try:
            return json.loads(self._synopsis_path().read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _record_synopsis(self, file: str, text: str) -> None:
        data = self._synopsis_fingerprints()
        data[file] = fingerprint(text)
        meta = self.root / META_DIR
        meta.mkdir(exist_ok=True)
        if not (meta / ".gitignore").exists():
            (meta / ".gitignore").write_text("*\n", "utf-8", newline="\n")
        _atomic_write(self._synopsis_path(), json.dumps(data, indent=1))

    def synopsis_state(self, chapter: Chapter, text: str, fingerprints: dict[str, dict] | None = None) -> str:
        """'none' (too short to need one), 'missing', 'stale' or 'fresh'."""
        if count_words(text) < SYNOPSIS_MIN_WORDS:
            return "fresh" if chapter.synopsis.strip() else "none"
        if not chapter.synopsis.strip():
            return "missing"
        fp = (fingerprints if fingerprints is not None else self._synopsis_fingerprints()).get(chapter.file)
        # A synopsis with no fingerprint was written by hand in novel.toml: trust it.
        if fp and change_ratio(fp, text) > STALE_RATIO:
            return "stale"
        return "fresh"

    def chapter_stats(self) -> dict[str, dict]:
        """Word count and synopsis state per chapter, reading each file once."""
        fps = self._synopsis_fingerprints()
        stats = {}
        for c in self.chapters:
            text = "" if self.is_missing(c.file) else self.read_chapter(c.file)
            stats[c.file] = {"words": count_words(text), "synopsis_state": self.synopsis_state(c, text, fps)}
        return stats

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

    # ---- images ------------------------------------------------------------

    @property
    def images_dir(self) -> Path:
        return self.assets_dir / IMAGES_DIR

    def add_image(self, data: bytes, name: str) -> str:
        """Store a picture under assets/images/ and return the Markdown src a chapter uses for it."""
        ext = Path(name).suffix.lower()
        if ext not in IMAGE_TYPES:
            raise ProjectError("Pictures must be JPEG, PNG or GIF")
        ext = ".jpg" if ext == ".jpeg" else ext
        self.images_dir.mkdir(parents=True, exist_ok=True)
        stem = slugify(Path(name).stem, "picture")
        path, n = self.images_dir / f"{stem}{ext}", 2
        while path.exists():
            path, n = self.images_dir / f"{stem}-{n}{ext}", n + 1
        path.write_bytes(data)
        return f"../assets/{IMAGES_DIR}/{path.name}"

    def image_path(self, src: str) -> Path | None:
        """The picture a chapter's image src points to, if it's a usable file inside assets/.

        Srcs are relative to the chapter (../assets/images/x.jpg), so they also
        resolve in any other Markdown editor. Remote URLs aren't allowed: ebooks
        must carry their pictures inside the file.
        """
        if "://" in src or src.startswith(("/", "data:")):
            return None
        path = (self.chapters_dir / unquote(src)).resolve()
        if path.suffix.lower() not in IMAGE_TYPES or not path.is_file():
            return None
        return path if path.is_relative_to(self.assets_dir.resolve()) else None

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
        images: dict[Path, int] = {}
        for c in self.chapters:
            if self.is_missing(c.file):
                continue
            for src in image_refs(self.read_chapter(c.file)):
                if path := self.image_path(src):
                    images[path] = path.stat().st_size
                elif "://" in src:
                    add("error", f"“{c.title}” uses an online picture ({src}). Ebooks must carry their "
                        "pictures inside the file — download it and add it to the chapter instead.")
                else:
                    add("error", f"“{c.title}” uses a picture that can't be found or isn't a JPEG, PNG "
                        f"or GIF in assets/: {src}")
        for path, size in images.items():
            if size > LARGE_IMAGE_BYTES:
                add("warn", f"Picture {path.name} is {size / 2**20:.1f} MB. Resizing it to about 1600 px "
                    "on the long side keeps the ebook small.")
        total = sum(images.values())
        if total > 10 * 2**20:
            add("warn", f"Pictures add {total / 2**20:.0f} MB to the ebook. KDP charges a delivery fee "
                "per MB on its 70% royalty plan, and big files are slow to download.")
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

    def move_to(self, root: Path) -> None:
        """Switch to another folder of novels. Edits already under way finish in the old one."""
        root.mkdir(parents=True, exist_ok=True)
        with self._locks_guard:
            self.root = root

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
        _check_id(project_id)
        path = self.root / project_id
        if not Project.is_project(path):
            raise ProjectError(f"No project {project_id!r}")
        return Project.load(path)

    def create(self, title: str, author: str = "") -> Project:
        return Project.create(self.root, title, author)

    # ---- managing whole novels ----------------------------------------------
    # Nothing is erased: deleted novels go to "Deleted novels/", archived ones to
    # "Archived novels/", both plain folders in the workspace. (Novel folders are
    # slugs, which never contain a space, so these names can't clash with one.)

    @property
    def archive_dir(self) -> Path:
        return self.root / ARCHIVE_DIR

    @property
    def trash_dir(self) -> Path:
        return self.root / TRASH_DIR

    def list_archived(self) -> list[Project]:
        if not self.archive_dir.is_dir():
            return []
        return [Project.load(p) for p in sorted(self.archive_dir.iterdir()) if p.is_dir() and Project.is_project(p)]

    def get_archived(self, project_id: str) -> Project:
        _check_id(project_id)
        path = self.archive_dir / project_id
        if not Project.is_project(path):
            raise ProjectError(f"No archived project {project_id!r}")
        return Project.load(path)

    @contextmanager
    def _locked(self, project_id: str) -> Iterator[None]:
        with self._locks_guard:
            lock = self._locks.setdefault(project_id, threading.Lock())
        with lock:
            yield

    def rename(self, project_id: str, title: str) -> Project:
        """Retitle a novel and rename its folder to match (when that name is free)."""
        title = title.strip()
        if not title:
            raise ProjectError("A novel needs a title")
        with self._locked(project_id):
            p = self.get(project_id)
            p.meta.title = title
            p.save()
            if slugify(title, "novel") != p.id:
                dest = _free_path(self.root, slugify(title, "novel"))
                p.root.rename(dest)
                p = Project.load(dest)
        return p

    def duplicate(self, project_id: str) -> Project:
        """A copy to experiment on: same text, notes and pictures; its own identity and history."""
        with self._locked(project_id):
            src = self.get(project_id)
            title = f"{src.meta.title} (copy)"
            dest = _free_path(self.root, slugify(title, "novel"))

            def skip(folder: str, names: list[str]) -> set[str]:
                top = Path(folder).resolve() == src.root.resolve()
                left_out = {"exports", "trash", ".git"} if top else set()
                if Path(folder).name == META_DIR:
                    left_out.add("history.git")  # the copy's history starts now
                return {n for n in names if n in left_out or n.endswith(".tmp")}

            shutil.copytree(src.root, dest, ignore=skip)
            (dest / "exports").mkdir(exist_ok=True)
        copy = Project.load(dest)
        copy.meta.title = title
        copy.meta.identifier = f"urn:uuid:{uuid.uuid4()}"  # stores treat it as a different book
        copy.save()
        return copy

    def archive(self, project_id: str) -> None:
        with self._locked(project_id):
            p = self.get(project_id)
            self.archive_dir.mkdir(exist_ok=True)
            p.root.rename(_free_path(self.archive_dir, p.id))

    def unarchive(self, project_id: str) -> Project:
        with self._locked(project_id):
            p = self.get_archived(project_id)
            dest = _free_path(self.root, p.id)
            p.root.rename(dest)
        return Project.load(dest)

    def delete(self, project_id: str, archived: bool = False) -> Path:
        """Move a novel to "Deleted novels/"; returns where it went."""
        with self._locked(project_id):
            p = self.get_archived(project_id) if archived else self.get(project_id)
            self.trash_dir.mkdir(exist_ok=True)
            dest = _free_path(self.trash_dir, p.id)
            p.root.rename(dest)
        return dest


def _check_id(project_id: str) -> None:
    if not project_id or "/" in project_id or "\\" in project_id or project_id.startswith("."):
        raise ProjectError("Invalid project id")


def _free_path(folder: Path, name: str) -> Path:
    """folder/name, or folder/name-2, -3… if that's taken."""
    path, n = folder / name, 2
    while path.exists():
        path, n = folder / f"{name}-{n}", n + 1
    return path


def _atomic_write(path: Path, text: str) -> None:
    # A unique temp name per write, so concurrent writers never clobber each other's file.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)  # usual umask-based mode
    try:
        # "\n" on every OS, so a folder shared between a Mac and a PC doesn't flip line endings.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
