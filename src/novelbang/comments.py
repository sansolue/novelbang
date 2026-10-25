"""Comments: the author's notes on a passage, kept in the chapter text but never in the book.

They're written in CriticMarkup, which other Markdown tools also read:

    {==the highlighted passage==}{>>the comment<<}
    {>>a comment on its own, at a point in the text<<}

Living in the text, a comment moves with its passage as the chapter is edited, and
search, history and any text editor see it. Exports, the preview, word counts and the
co-writer get the text without them (`strip`). The UI parses them the same way (app.js,
COMMENT_RE).
"""

from __future__ import annotations

import re

_PASSAGE = r"(?:(?!==\}|\{==).)*"
_NOTE = r"(?:(?!<<\}).)*"
COMMENT = re.compile(
    rf"\{{==(?P<passage>{_PASSAGE})==\}}[ \t]*\{{>>(?P<note>{_NOTE})<<\}}"  # passage and comment
    rf"|\{{>>(?P<lone>{_NOTE})<<\}}"  # a comment at a point
    rf"|\{{==(?P<marked>{_PASSAGE})==\}}",  # a highlight with no comment
    re.S,
)
# What's left of a comment that was never closed, which would print in the book.
_UNCLOSED = re.compile(r"\{==|==\}|\{>>|<<\}")
_POINT_END = re.compile(r"[\s.,;:!?)\]”’\"']|$")


def find(text: str) -> list[dict]:
    """Each comment in `text`: its passage, its note and its exact markup."""
    out = []
    for m in COMMENT.finditer(text):
        passage = m["passage"] if m["passage"] is not None else (m["marked"] or "")
        note = m["note"] if m["note"] is not None else (m["lone"] or "")
        out.append({"passage": passage, "note": note.strip(), "raw": m[0]})
    return out


def count(text: str) -> int:
    return sum(1 for _ in COMMENT.finditer(text)) if "{" in text else 0


def _without(text: str, matches: list[re.Match]) -> str:
    out: list[str] = []
    last = 0
    for m in matches:
        before = text[last : m.start()]
        if m["lone"] is not None and _POINT_END.match(text, m.end()):
            # "a word {>>note<<} more" → "a word more", not "a word  more"
            before = before.rstrip(" \t")
        # A comment inside another's passage goes too.
        out += [before, strip(m["passage"] if m["passage"] is not None else (m["marked"] or ""))]
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def strip(text: str) -> str:
    """The text as it goes in the book: passages kept, comments gone."""
    return _without(text, list(COMMENT.finditer(text))) if "{" in text else text


def unclosed(text: str) -> bool:
    """Whether a comment's markup was left half-written."""
    return bool(_UNCLOSED.search(strip(text)))


def resolve(text: str, index: int, raw: str) -> str:
    """`text` with its `index`th comment taken out (its passage stays).

    `raw` must be that comment's markup, so a stale request can't remove the wrong one.
    """
    matches = list(COMMENT.finditer(text))
    if not 0 <= index < len(matches) or matches[index][0] != raw:
        raise ValueError("That comment has changed since the list was shown")
    return _without(text, [matches[index]])
