"""Markdown → XHTML shared by every exporter."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.entities import name2codepoint

import markdown

from ..project import Chapter, Project

_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_HR = re.compile(r"<hr\s*/?>")


def _numeric_entities(text: str) -> str:
    """XHTML (and therefore EPUB) only knows the five XML entities."""

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in _XML_ENTITIES or name not in name2codepoint:
            return m.group(0)
        return f"&#{name2codepoint[name]};"

    return _ENTITY.sub(sub, text)


def md_to_xhtml(text: str) -> str:
    out = markdown.markdown(
        text,
        extensions=["smarty", "footnotes", "sane_lists"],
        output_format="xhtml",
    )
    out = _HR.sub('<p class="scene-break">* * *</p>', out)
    return _numeric_entities(out)


def esc(text: str) -> str:
    return html.escape(text, quote=True)


@dataclass
class RenderedChapter:
    chapter: Chapter
    number: int | None
    body: str  # XHTML fragment

    @property
    def label(self) -> str:
        return f"Chapter {self.number}" if self.number is not None else ""

    def heading_html(self, number_chapters: bool) -> str:
        cls = "chapter" if self.chapter.kind == "chapter" else self.chapter.kind + "-matter"
        label = (
            f'<p class="chapter-number">{esc(self.label)}</p>\n'
            if number_chapters and self.number is not None
            else ""
        )
        return f'<header class="{cls}">\n{label}<h1>{esc(self.chapter.title)}</h1>\n</header>'

    def toc_title(self, number_chapters: bool) -> str:
        if number_chapters and self.number is not None:
            return f"{self.number}. {self.chapter.title}"
        return self.chapter.title


def render_chapters(project: Project) -> list[RenderedChapter]:
    return [
        RenderedChapter(ch, num, md_to_xhtml(project.read_chapter(ch.file)))
        for ch, num in project.numbered()
    ]


BOOK_CSS = """
body { font-family: Georgia, "Times New Roman", serif; line-height: 1.5; margin: 0 5%; }
h1 { font-size: 1.6em; text-align: center; margin: 0 0 2em; font-weight: normal; }
.chapter-number { text-align: center; text-transform: uppercase; letter-spacing: 0.15em;
  font-size: 0.8em; margin: 3em 0 0.5em; text-indent: 0; }
header.chapter h1 { margin-top: 0; }
header.front-matter h1, header.back-matter h1 { margin-top: 3em; }
p { margin: 0; text-indent: 1.5em; text-align: justify; }
h1 + p, header + p, .scene-break + p, blockquote p, li p { text-indent: 0; }
.scene-break { text-align: center; text-indent: 0; margin: 1em 0; }
blockquote { margin: 1em 2em; font-style: italic; }
.title-page { text-align: center; margin-top: 30%; }
.title-page h1 { font-size: 2.2em; margin-bottom: 0.3em; }
.title-page .subtitle { font-size: 1.2em; font-style: italic; text-indent: 0; text-align: center; }
.title-page .author { margin-top: 3em; font-size: 1.2em; text-indent: 0; text-align: center; }
.copyright p { text-indent: 0; text-align: left; font-size: 0.85em; margin-bottom: 0.8em; }
.copyright { margin-top: 50%; }
nav ol { list-style: none; padding-left: 0; }
nav li { margin: 0.4em 0; }
.cover { text-align: center; margin: 0; padding: 0; }
.cover img { max-width: 100%; max-height: 100%; }
"""
