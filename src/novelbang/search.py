"""Find and replace across a novel: chapter text, chapter synopses and notes.

Straight and curly quotes match each other (typing ' finds ’), since the editor holds
whichever the writer typed. Picture file paths are never matched, so replacing a word
can't break a picture; the alt text and caption around them are matched like any text.
The UI's in-chapter find (app.js, findPattern) follows the same rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .project import _MD_IMAGE, Project, ProjectError

_QUOTES = {"'": "'‘’", "‘": "'‘’", "’": "'‘’", '"': '"“”', "“": '"“”', "”": '"“”'}
MAX_QUERY = 500
MAX_HITS = 2000  # shown across the whole book; counts are always complete
CONTEXT = 50  # characters of context either side of a hit


def pattern(query: str, match_case: bool = False, whole_word: bool = False) -> re.Pattern:
    if not query:
        raise ProjectError("Type something to search for")
    if len(query) > MAX_QUERY:
        raise ProjectError(f"Search text is limited to {MAX_QUERY} characters")
    body = "".join(f"[{_QUOTES[ch]}]" if ch in _QUOTES else re.escape(ch) for ch in query)
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"
    return re.compile(body, 0 if match_case else re.IGNORECASE)


def _protected(text: str) -> list[tuple[int, int]]:
    return [m.span(1) for m in _MD_IMAGE.finditer(text)]


def find(text: str, pat: re.Pattern) -> list[tuple[int, int]]:
    """(start, end) of each match, leaving out picture paths."""
    guarded = _protected(text)
    return [
        m.span() for m in pat.finditer(text)
        if m.end() > m.start() and not any(s < m.end() and m.start() < e for s, e in guarded)
    ]


def replace(text: str, pat: re.Pattern, replacement: str) -> tuple[str, int]:
    spans = find(text, pat)
    if not spans:
        return text, 0
    out, last = [], 0
    for s, e in spans:
        out += [text[last:s], replacement]
        last = e
    out.append(text[last:])
    return "".join(out), len(spans)


def _hit(text: str, field: str, start: int, end: int) -> dict:
    """A hit with the text around it, cut at line breaks and to whole words."""
    lo = max(text.rfind("\n", 0, start) + 1, start - CONTEXT)
    hi = text.find("\n", end)
    hi = min(len(text) if hi < 0 else hi, end + CONTEXT)
    before, after = text[lo:start], text[end:hi]
    if lo > 0 and text[lo - 1] != "\n" and " " in before:
        before = "…" + before[before.index(" ") + 1 :]
    if hi < len(text) and text[hi] != "\n" and " " in after:
        after = after[: after.rindex(" ")] + "…"
    return {"field": field, "start": start, "end": end, "before": before, "match": text[start:end], "after": after}


@dataclass
class Doc:
    type: str  # "chapter" or "note"
    name: str  # chapter file or note file name
    title: str


def documents(p: Project) -> list[Doc]:
    """Everything searchable, in book order: chapters, then notes."""
    docs = [Doc("chapter", c.file, c.title) for c in p.chapters if not p.is_missing(c.file)]
    return docs + [Doc("note", n, n.removesuffix(".md")) for n in p.list_notes()]


def _fields(p: Project, doc: Doc) -> list[tuple[str, str]]:
    if doc.type == "chapter":
        return [("text", p.read_chapter(doc.name)), ("synopsis", p.chapter(doc.name).synopsis)]
    return [("text", p.read_note(doc.name))]


def search(p: Project, pat: re.Pattern) -> dict:
    results, total, shown = [], 0, 0
    for doc in documents(p):
        hits, count = [], 0
        for field, text in _fields(p, doc):
            for s, e in find(text, pat):
                count += 1
                if shown < MAX_HITS:
                    hits.append(_hit(text, field, s, e))
                    shown += 1
        if count:
            results.append({"type": doc.type, "name": doc.name, "title": doc.title, "count": count, "hits": hits})
            total += count
    return {"total": total, "shown": shown, "docs": results}


def replace_in(p: Project, pat: re.Pattern, replacement: str, only: set[tuple[str, str]] | None = None) -> dict:
    """Replace in every document, or only the (type, name) pairs in `only`. Returns counts."""
    replaced, changed = 0, []
    for doc in documents(p):
        if only is not None and (doc.type, doc.name) not in only:
            continue
        n_doc = 0
        for field, text in _fields(p, doc):
            new, n = replace(text, pat, replacement)
            if not n:
                continue
            n_doc += n
            if field == "synopsis":
                # Set directly: the synopsis still describes the text as well as it did before.
                p.chapter(doc.name).synopsis = new
                p.save()
            elif doc.type == "chapter":
                p.write_chapter(doc.name, new)
            else:
                p.write_note(doc.name, new)
        if n_doc:
            replaced += n_doc
            changed.append({"type": doc.type, "name": doc.name})
    return {"replaced": replaced, "changed": changed}
