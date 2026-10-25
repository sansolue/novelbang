"""Import an existing manuscript (.docx, Markdown or plain text) as a new novel.

The text is split into chapters at its headings: Word's heading styles, Markdown's
#-headings, or, failing those, lines such as "Chapter 3" or "Prologue". Italic, bold,
scene breaks, footnotes, pictures and Word's comments come across; nothing else in the
text is dropped.
Prologues, dedications and the like become front matter; epilogues, afterwords and
acknowledgements become back matter.

.docx is read with the standard library (it's zipped XML), so there's nothing to install.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from . import comments
from .project import IMAGE_TYPES, Project, ProjectError, Workspace, count_words

IMPORT_TYPES = (".docx", ".md", ".markdown", ".txt")
MAX_IMPORT_BYTES = 100 * 2**20

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DC = "{http://purl.org/dc/elements/1.1/}"

FRONT = re.compile(r"^(prologue|dedication|epigraph|foreword|preface|introduction|author'?s note)\b", re.I)
BACK = re.compile(r"^(epilogue|afterword|acknowledge?ments?|about the author|also by|glossary|"
                  r"appendix|author'?s note|reading group|discussion questions)\b", re.I)
_NUMBER_WORDS = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
                 r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
                 r"eighty|ninety|hundred")
_CHAPTER_NUMBER = rf"(?:\d+|[ivxlc]+|(?:{_NUMBER_WORDS})(?:[- ](?:{_NUMBER_WORDS}))*)"
# A plain-text line that starts a chapter: "Chapter 3", "CHAPTER THREE: The Harbour", "Prologue".
CHAPTER_LINE = re.compile(rf"^(?:chapter\s+{_CHAPTER_NUMBER}\b.*|{FRONT.pattern[1:]}.*|{BACK.pattern[1:]}.*)$", re.I)
# "Chapter 3: The Harbour" → "The Harbour"
CHAPTER_PREFIX = re.compile(rf"^chapter\s+{_CHAPTER_NUMBER}\s*[:.—–-]\s*(?=\S)", re.I)
BARE_CHAPTER = re.compile(rf"^chapter\s+{_CHAPTER_NUMBER}$", re.I)
SCENE_BREAK = re.compile(r"^[\s*#~•·◆◇❖✦⁂—–_-]{1,12}$")
BYLINE = re.compile(r"^by\s+(.+)$", re.I)


@dataclass
class Block:
    kind: str  # "heading", "para", "break", "title"
    text: str = ""
    level: int = 0


@dataclass
class Draft:
    title: str = ""
    subtitle: str = ""
    author: str = ""
    blocks: list[Block] = field(default_factory=list)
    footnotes: dict[str, str] = field(default_factory=dict)  # id → Markdown text
    images: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # placeholder → (file name, data)


# ---- Markdown escaping --------------------------------------------------------

def _escape(text: str) -> str:
    text = re.sub(r"([\\`*_\[\]])", r"\\\1", text).replace("<", "&lt;")
    return re.sub(r"^(\s*)([#>+-]|\d+\.)(?=\s)", lambda m: m[1] + "\\" + m[2], text)


def _wrap(text: str, mark: str) -> str:
    """*text* with the spaces outside the marks, where Markdown needs them."""
    core = text.strip()
    if not core:
        return text
    lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()) :]
    return f"{lead}{mark}{core}{mark}{trail}"


# ---- .docx ----------------------------------------------------------------------

def _on(el: ET.Element | None) -> bool:
    return el is not None and el.get(f"{W}val", "true").lower() not in ("0", "false", "none")


def read_docx(data: bytes) -> Draft:
    try:
        z = zipfile.ZipFile(BytesIO(data))
        doc = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ProjectError("This isn't a Word document NovelBang can read (.docx)") from None
    names = set(z.namelist())

    def xml(name: str) -> ET.Element | None:
        return ET.fromstring(z.read(name)) if name in names else None

    # Paragraph styles: id → (name, based on, outline level, bold, italic)
    styles: dict[str, tuple[str, str | None, int | None, bool, bool]] = {}
    if (st := xml("word/styles.xml")) is not None:
        for s in st.iter(f"{W}style"):
            sid = s.get(f"{W}styleId", "")
            name = (s.find(f"{W}name").get(f"{W}val", "") if s.find(f"{W}name") is not None else sid).lower()
            based = s.find(f"{W}basedOn")
            lvl = s.find(f"{W}pPr/{W}outlineLvl")
            rpr = s.find(f"{W}rPr")
            styles[sid] = (
                name, based.get(f"{W}val") if based is not None else None,
                int(lvl.get(f"{W}val", "9")) if lvl is not None else None,
                rpr is not None and _on(rpr.find(f"{W}b")), rpr is not None and _on(rpr.find(f"{W}i")),
            )

    def style_level(sid: str | None) -> tuple[str, int | None]:
        """("title"/"heading"/"", heading level) for a paragraph style, following basedOn."""
        for _ in range(8):
            if not sid or sid not in styles:
                return "", None
            name, based, lvl, _, _ = styles[sid]
            if name in ("title", "book title"):
                return "title", 0
            if m := re.fullmatch(r"heading (\d)", name):
                return "heading", int(m[1])
            if lvl is not None and lvl < 9:
                return "heading", lvl + 1
            sid = based
        return "", None

    def char_style(sid: str | None) -> tuple[bool, bool]:
        if not sid or sid not in styles:
            return False, False
        name, _, _, b, i = styles[sid]
        return b or "strong" in name, i or "emphasis" in name

    rels: dict[str, str] = {}
    if (rx := xml("word/_rels/document.xml.rels")) is not None:
        for r in rx.iter(f"{REL}Relationship"):
            rels[r.get("Id", "")] = r.get("Target", "")

    # Word's comments, by id, as one line of plain text each.
    notes: dict[str, str] = {}
    if (cx := xml("word/comments.xml")) is not None:
        for c in cx.iter(f"{W}comment"):
            paras = ("".join(t.text or "" for t in p.iter(f"{W}t")).strip() for p in c.iter(f"{W}p"))
            if text := " ".join(t for t in paras if t):
                notes[c.get(f"{W}id", "")] = " ".join(text.replace("<<}", "<< }").split())
    placed: set[str] = set()  # comments already in the text

    draft = Draft()
    if (core := xml("docProps/core.xml")) is not None and (t := core.find(f"{DC}title")) is not None:
        draft.title = (t.text or "").strip()

    def runs_to_md(p: ET.Element, para_style: str | None) -> tuple[str, bool]:
        """A paragraph's Markdown, and whether a page break comes before or in it."""
        p_bold, p_ital = char_style(para_style)
        pieces: list[tuple[str, bool, bool]] = []  # (text, bold, italic); "\0img:" / "\0fn:" markers
        page_break = False

        def walk(node: ET.Element) -> None:
            nonlocal page_break
            for child in node:
                tag = child.tag
                if tag == f"{W}r":
                    rpr = child.find(f"{W}rPr")
                    cs = rpr.find(f"{W}rStyle").get(f"{W}val") if rpr is not None and rpr.find(f"{W}rStyle") is not None else None
                    c_bold, c_ital = char_style(cs)
                    bold = _on(rpr.find(f"{W}b")) if rpr is not None and rpr.find(f"{W}b") is not None else (p_bold or c_bold)
                    ital = _on(rpr.find(f"{W}i")) if rpr is not None and rpr.find(f"{W}i") is not None else (p_ital or c_ital)
                    for part in child:
                        if part.tag == f"{W}t":
                            pieces.append((part.text or "", bold, ital))
                        elif part.tag == f"{W}tab":
                            pieces.append((" ", False, False))
                        elif part.tag == f"{W}br":
                            if part.get(f"{W}type") == "page":
                                page_break = True
                            else:
                                pieces.append(("\0br", False, False))
                        elif part.tag == f"{W}footnoteReference":
                            pieces.append((f"\0fn:{part.get(f'{W}id')}", False, False))
                        elif part.tag == f"{W}commentReference":
                            pieces.append((f"\0cr:{part.get(f'{W}id')}", False, False))
                        elif part.tag in (f"{W}drawing", f"{W}pict"):
                            for blip in part.iter(f"{A}blip"):
                                if target := rels.get(blip.get(f"{R}embed", "")):
                                    pieces.append((f"\0img:{target}", False, False))
                elif tag in (f"{W}commentRangeStart", f"{W}commentRangeEnd"):
                    pieces.append((f"\0c{'s' if tag.endswith('Start') else 'e'}:{child.get(f'{W}id')}", False, False))
                elif tag in (f"{W}hyperlink", f"{W}ins", f"{W}smartTag", f"{W}sdt", f"{W}sdtContent", f"{W}fldSimple"):
                    walk(child)
                elif tag == f"{W}pPr":
                    if child.find(f"{W}pageBreakBefore") is not None and _on(child.find(f"{W}pageBreakBefore")):
                        page_break = True

        walk(p)
        out, group, fmt = [], "", (False, False)
        open_id = None  # the comment whose passage this paragraph is in
        waiting: list[str] = []  # comments that end inside that passage, placed after it

        def note(cid: str) -> str:
            placed.add(cid)
            return f"{{>>{notes[cid]}<<}}"

        def flush() -> None:
            nonlocal group
            if group:
                text = _escape(group)
                if fmt[1]:
                    text = _wrap(text, "*")
                if fmt[0]:
                    text = _wrap(text, "**")
                out.append(text)
            group = ""

        for text, bold, ital in pieces:
            if text.startswith("\0"):
                flush()
                if text == "\0br":
                    out.append("  \n")
                elif text.startswith("\0fn:"):
                    out.append(f"[^{text[4:]}]")
                elif text.startswith("\0c"):
                    # A passage holds one comment: one that ends inside another's passage
                    # goes just after it, and one on several paragraphs marks only the first.
                    cid = text[4:]
                    if cid not in notes or cid in placed:
                        continue
                    if text[2] == "s" and open_id is None:
                        open_id = cid
                        out.append("{==")
                    elif text[2] in "er" and cid == open_id:
                        out.append("==}" + note(cid) + "".join(map(note, waiting)))
                        open_id, waiting = None, []
                    elif text[2] in "er" and open_id is not None:
                        if cid not in waiting:
                            waiting.append(cid)
                    elif text[2] in "er":
                        out.append(note(cid))
                else:
                    target = posixpath.normpath(posixpath.join("word", text[5:]))
                    ext = Path(target).suffix.lower()
                    if target in names and ext in IMAGE_TYPES:
                        key = f"\0picture{len(draft.images)}\0"
                        draft.images[key] = (Path(target).name, z.read(target))
                        out.append(key)
                continue
            if (bold, ital) != fmt:
                flush()
                fmt = (bold, ital)
            group += text
        flush()
        if open_id is not None:
            out.append("==}" + note(open_id) + "".join(map(note, waiting)))
        return "".join(out), page_break

    body = doc.find(f"{W}body")
    for p in body.iter(f"{W}p") if body is not None else []:
        ppr = p.find(f"{W}pPr")
        sid = ppr.find(f"{W}pStyle").get(f"{W}val") if ppr is not None and ppr.find(f"{W}pStyle") is not None else None
        kind, level = style_level(sid)
        if ppr is not None and kind == "" and (lvl := ppr.find(f"{W}outlineLvl")) is not None:
            if (n := int(lvl.get(f"{W}val", "9"))) < 9:
                kind, level = "heading", n + 1
        md, _ = runs_to_md(p, sid)
        if kind in ("title", "heading"):
            plain = re.sub(r"\\(.)", r"\1", comments.strip(md)).replace("*", "").strip()
            if plain:
                draft.blocks.append(Block(kind, plain, level or 0))
        elif md.strip():
            draft.blocks.append(Block("para", md.strip()))

    if (fx := xml("word/footnotes.xml")) is not None:
        for fn in fx.iter(f"{W}footnote"):
            fid = fn.get(f"{W}id", "")
            if fn.get(f"{W}type") in ("separator", "continuationSeparator", "continuationNotice"):
                continue
            text = " ".join(m for p in fn.iter(f"{W}p") if (m := runs_to_md(p, None)[0].strip()))
            draft.footnotes[fid] = text
    return draft


# ---- Markdown and plain text -----------------------------------------------------

def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def read_markdown(text: str) -> Draft:
    draft = Draft()
    # Pandoc-style metadata block at the top.
    if m := re.match(r"^---\n(.*?)\n(?:---|\.\.\.)\n", text, re.S):
        for line in m[1].splitlines():
            if mm := re.match(r"^(title|author):\s*[\"']?(.*?)[\"']?\s*$", line, re.I):
                setattr(draft, mm[1].lower(), mm[2])
        text = text[m.end() :]
    para: list[str] = []
    in_fence = False

    def flush() -> None:
        if para:
            draft.blocks.append(Block("para", "\n".join(para).strip()))
            para.clear()

    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
        if not in_fence and (m := re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)):
            flush()
            draft.blocks.append(Block("heading", m[2].strip(), len(m[1])))
        elif not in_fence and i + 1 < len(lines) and line.strip() and re.fullmatch(r"=+\s*|-{3,}\s*", lines[i + 1]) \
                and not para:
            # Setext heading: the underline line is skipped below.
            flush()
            draft.blocks.append(Block("heading", line.strip(), 1 if lines[i + 1].startswith("=") else 2))
        elif not in_fence and re.fullmatch(r"=+\s*|-{3,}\s*", line) and draft.blocks and draft.blocks[-1].kind == "heading" \
                and not para and i > 0 and lines[i - 1].strip() == draft.blocks[-1].text:
            continue
        elif not line.strip() and not in_fence:
            flush()
        else:
            para.append(line)
    flush()
    return draft


def read_text(text: str) -> Draft:
    draft = Draft()
    for chunk in re.split(r"\n\s*\n", text):
        lines = [l.strip() for l in chunk.split("\n") if l.strip()]
        if not lines:
            continue
        # A chapter line on its own (or heading its paragraph) starts a chapter.
        if len(lines[0]) <= 80 and CHAPTER_LINE.match(lines[0]):
            draft.blocks.append(Block("heading", lines[0], 1))
            lines = lines[1:]
        if not lines:
            continue
        before_chapters = not any(b.kind == "heading" for b in draft.blocks)
        if before_chapters and 1 < len(lines) <= 4 and all(len(l) <= 50 for l in lines):
            # A title page ("THE IRON SEA" / "by R. Vale"): a block per line.
            draft.blocks.extend(Block("para", _escape(l)) for l in lines)
        else:
            draft.blocks.append(Block("para", _escape(" ".join(lines))))
    return draft


# ---- splitting into chapters -------------------------------------------------------

def _title_page(paras: list[str]) -> tuple[str, str, str] | None:
    """(title, subtitle, author) when the text before the first chapter is just a title page:
    a title, maybe a subtitle, maybe "by …". Anything more is story, and is kept."""
    plain = [re.sub(r"\\(.)", r"\1", p).replace("*", "").strip() for p in paras]
    if not plain or len(plain) > 3 or any(count_words(p) > 12 or "\n" in p for p in plain):
        return None
    author = ""
    if m := BYLINE.match(plain[-1]):
        author = m[1].strip()
        plain = plain[:-1]
    if len(plain) > 2 or not (plain or author):
        return None
    return (plain[0] if plain else ""), (plain[1] if len(plain) > 1 else ""), author


@dataclass
class ImportedChapter:
    title: str
    kind: str
    text: str


def _uncap(text: str) -> str:
    """"THE IRON SEA" → "The Iron Sea"; text in mixed case is left alone."""
    if not text.isupper() or len(text) < 3:
        return text
    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split(" "))


def _kind(title: str) -> str:
    if FRONT.match(title) and not re.match(r"^author'?s note", title, re.I):
        return "front"
    return "back" if BACK.match(title) else "chapter"


def split_chapters(draft: Draft) -> tuple[list[ImportedChapter], bool]:
    """Chapters, and whether their titles are just "Chapter N" (so the book shouldn't add its own)."""
    # Text in Word's Title style names the book, ahead of the file's properties.
    if titled := next((b for b in draft.blocks if b.kind == "title"), None):
        draft.title = titled.text
    blocks = [b for b in draft.blocks if b.kind != "title"]
    levels = sorted({b.level for b in blocks if b.kind == "heading"})
    # A single top-level heading above the chapter headings is the book's title.
    if len(levels) >= 2:
        top = [b for b in blocks if b.kind == "heading" and b.level == levels[0]]
        if len(top) == 1 and blocks.index(top[0]) == next(i for i, b in enumerate(blocks) if b.kind == "heading"):
            draft.title = draft.title or top[0].text
            blocks.remove(top[0])
            levels = levels[1:]
    if not levels:
        # No headings: lines like "Chapter 3" or "Prologue" on their own.
        for b in blocks:
            if b.kind == "para" and len(b.text) <= 80 and "\n" not in b.text and CHAPTER_LINE.match(re.sub(r"\\(.)", r"\1", b.text).replace("*", "")):
                b.kind, b.level, b.text = "heading", 1, re.sub(r"\\(.)", r"\1", b.text).replace("*", "").strip()
        levels = [1] if any(b.kind == "heading" for b in blocks) else []
    split_level = levels[0] if levels else None

    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for b in blocks:
        if b.kind == "heading" and b.level == split_level:
            sections.append((b.text, []))
        elif b.kind == "heading":
            sections[-1][1].append("#" * min(b.level - (split_level or 0) + 1, 6) + " " + b.text)
        elif SCENE_BREAK.match(re.sub(r"\\(.)", r"\1", b.text)) and not b.text.startswith("\0"):
            if sections[-1][1]:
                sections[-1][1].append("***")
        else:
            sections[-1][1].append(b.text)

    # Before the first chapter: a short title page is read for title and author; anything
    # longer is kept as front matter.
    _, opening = sections.pop(0)
    if sections and (page := _title_page(opening)):
        title, subtitle, byline = page
        draft.title = draft.title or title
        draft.subtitle = draft.subtitle or subtitle
        draft.author = draft.author or byline
        opening = []
    if opening:
        sections.insert(0, ("Opening" if sections else draft.title or "Manuscript", opening))
    if not sections:
        sections = [(draft.title or "Chapter One", [])]

    draft.title = _uncap(draft.title)
    draft.author = _uncap(draft.author)
    chapters = []
    for title, paras in sections:
        title = _uncap(title or "")
        while paras and paras[-1] == "***":
            paras.pop()
        text = "\n\n".join(paras)
        notes = [f"[^{fid}]: {draft.footnotes[fid]}" for fid in re.findall(r"\[\^([^\]]+)\]", text) if fid in draft.footnotes]
        if notes:
            text += "\n\n" + "\n".join(dict.fromkeys(notes))
        chapters.append(ImportedChapter(title or "Untitled", _kind(title or ""), text + "\n" if text else ""))
    body = [c for c in chapters if c.kind == "chapter"]
    bare = bool(body) and all(BARE_CHAPTER.match(c.title) for c in body)
    if not bare:
        for c in chapters:
            if c.kind == "chapter":
                c.title = CHAPTER_PREFIX.sub("", c.title) or c.title
    return chapters, bare


# ---- into a project -----------------------------------------------------------------

def import_manuscript(ws: Workspace, name: str, data: bytes, author: str = "") -> Project:
    ext = Path(name).suffix.lower()
    if len(data) > MAX_IMPORT_BYTES:
        raise ProjectError(f"That file is over {MAX_IMPORT_BYTES // 2**20} MB")
    if ext == ".docx":
        draft = read_docx(data)
    elif ext in (".md", ".markdown"):
        draft = read_markdown(_decode(data))
    elif ext == ".txt":
        draft = read_text(_decode(data))
    elif ext in (".doc", ".rtf", ".odt", ".pages"):
        raise ProjectError(f"NovelBang can't read {ext} files. Save it as a Word document (.docx) and import that.")
    elif ext == ".scriv" or name.endswith(".scrivx"):
        raise ProjectError("For a Scrivener project, use File → Compile → Microsoft Word (.docx), then import that.")
    else:
        raise ProjectError("Import a Word document (.docx), Markdown (.md) or plain text (.txt) file")

    chapters, bare_titles = split_chapters(draft)
    if not any(c.text.strip() for c in chapters):
        raise ProjectError("That file has no text to import")
    title = draft.title.strip() or Path(name).stem.replace("_", " ").strip() or "Imported novel"
    project = ws.create(title, (draft.author or author).strip())
    blank = project.chapters.pop(0)  # the empty "Chapter One" a new novel starts with
    (project.chapters_dir / blank.file).unlink()
    srcs = {key: project.add_image(img, img_name) for key, (img_name, img) in draft.images.items()}
    for c in chapters:
        text = c.text
        for key, src in srcs.items():
            text = text.replace(key, f"![]({src})")
        ch = project.add_chapter(c.title, c.kind)
        project.write_chapter(ch.file, text)
    project.meta.number_chapters = not bare_titles
    project.meta.subtitle = _uncap(draft.subtitle.strip())
    project.save()
    return Project.load(project.root)
