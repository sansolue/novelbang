"""Markdown → XHTML shared by every exporter."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.entities import name2codepoint
from pathlib import Path
from urllib.parse import quote

import markdown

from ..project import Chapter, Project

_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_HR = re.compile(r"<hr\s*/?>")
_IMG = re.compile(r"<img\b[^>]*?/>")
_SRC = re.compile(r'\ssrc="([^"]*)"')
_TITLE = re.compile(r'\stitle="([^"]*)"')
_LONE_IMG = re.compile(r"<p>\s*(<img\b[^>]*?/>)\s*</p>")
_EMPTY_P = re.compile(r"<p>\s*</p>\n?")


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


def _rewrite_images(xhtml: str, src_for: Callable[[str], str | None]) -> str:
    """Point each <img> at `src_for(src)`, dropping it when that's None; a picture
    on its own line becomes a <figure>, with its Markdown title as the caption."""

    def img(m: re.Match) -> str:
        tag = m.group(0)
        src = _SRC.search(tag)
        new = src_for(html.unescape(src.group(1))) if src else None
        if new is None:
            return ""
        return tag.replace(src.group(0), f' src="{esc(new)}"')

    def figure(m: re.Match) -> str:
        tag = m.group(1)
        title = _TITLE.search(tag)
        caption = f"\n<figcaption>{title.group(1)}</figcaption>" if title else ""
        if title:
            tag = tag.replace(title.group(0), "")
        return f'<figure class="illustration">\n{tag}{caption}\n</figure>'

    xhtml = _IMG.sub(img, xhtml)
    return _EMPTY_P.sub("", _LONE_IMG.sub(figure, xhtml))


def esc(text: str) -> str:
    return html.escape(text, quote=True)


@dataclass
class RenderedChapter:
    chapter: Chapter
    number: int | None
    body: str  # XHTML fragment
    images: list[Path] = field(default_factory=list)  # pictures it shows, in order

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


def render_markdown(
    project: Project, text: str, image_src: Callable[[Path], str] | None = None
) -> tuple[str, list[Path], list[str]]:
    """Markdown → XHTML with pictures resolved: (body, pictures shown, srcs not found).

    `image_src` maps a picture in assets/ to the src the export uses for it;
    pictures that can't be found are left out.
    """
    images: list[Path] = []
    missing: list[str] = []

    def src_for(src: str) -> str | None:
        path = project.image_path(src)
        if path is None:
            if src not in missing:
                missing.append(src)
            return None
        if path not in images:
            images.append(path)
        return image_src(path) if image_src else src

    return _rewrite_images(md_to_xhtml(text), src_for), images, missing


def render_chapters(
    project: Project, image_src: Callable[[Path], str] | None = None
) -> list[RenderedChapter]:
    out = []
    for ch, num in project.numbered():
        body, images, _ = render_markdown(project, project.read_chapter(ch.file), image_src)
        out.append(RenderedChapter(ch, num, body, images))
    return out


def asset_href(project: Project, path: Path) -> str:
    """Where an exported book keeps a picture: assets/… mirroring the project, URL-quoted."""
    return "assets/" + quote(path.relative_to(project.assets_dir.resolve()).as_posix())


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
img { max-width: 100%; }
figure.illustration { margin: 1.5em 0; text-align: center; page-break-inside: avoid; break-inside: avoid; }
figure.illustration img { max-height: 95vh; }
figcaption { font-size: 0.85em; font-style: italic; margin-top: 0.5em; text-align: center; }
figure + p { text-indent: 0; }
"""
