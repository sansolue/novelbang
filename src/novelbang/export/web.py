"""Exports for reading and publishing on the web.

- manuscript HTML: one self-contained page, styled for print (browser → Save as PDF)
- website: a static site (index + one page per chapter) for any static host
  (GitHub Pages, Netlify, itch.io, your own server)
- serial pack: one clean HTML fragment + Markdown file per chapter, ready to
  paste into Royal Road, Wattpad, AO3, Substack, Medium, etc.
- markdown: the whole manuscript as one .md file (e.g. for pandoc → .docx)
"""

from __future__ import annotations

import base64
import shutil
import zipfile
from pathlib import Path
from urllib.parse import unquote

from ..project import IMAGE_TYPES, Project, slugify
from .render import BOOK_CSS, RenderedChapter, asset_href, esc, render_chapters

PRINT_CSS = """
@page { size: 6in 9in; margin: 0.75in 0.7in; }
@media screen { body { max-width: 38em; margin: 2em auto; padding: 0 1em; } }
@media print { .page { break-before: page; } a { color: inherit; text-decoration: none; } }
.toc ol { list-style: none; padding: 0; } .toc li { margin: 0.3em 0; }
.cover-page img { width: 100%; }
"""

SITE_CSS = """
:root { --fg: #222; --bg: #fdfbf7; --muted: #777; --accent: #8a4b2a; }
@media (prefers-color-scheme: dark) { :root { --fg: #ddd; --bg: #1c1b19; --muted: #999; --accent: #e0a37f; } }
body { background: var(--bg); color: var(--fg); max-width: 36em; margin: 0 auto; padding: 2em 1.2em 4em;
  font-size: 1.15rem; }
a { color: var(--accent); }
p { text-align: left; }
.site-nav { display: flex; justify-content: space-between; gap: 1em; margin: 3em 0 1em;
  font-family: system-ui, sans-serif; font-size: 0.9rem; }
.book-head { text-align: center; margin-bottom: 2em; }
.book-head img { max-width: 60%; border-radius: 4px; box-shadow: 0 4px 20px rgba(0,0,0,.25); }
.blurb p { text-indent: 0; margin-bottom: 1em; }
.toc ol { padding-left: 1.4em; font-family: system-ui, sans-serif; font-size: 1rem; }
.toc li { margin: 0.5em 0; }
.byline { color: var(--muted); font-family: system-ui, sans-serif; }
"""


def _slug(project: Project) -> str:
    return slugify(project.meta.title, "book")


def _html_page(title: str, lang: str, css: str, body: str, extra_head: str = "") -> str:
    return (
        f'<!DOCTYPE html>\n<html lang="{esc(lang)}">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n{extra_head}<style>{css}</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def build_manuscript_html(project: Project, out_dir: Path | None = None) -> Path:
    m = project.meta
    out_dir = out_dir or project.exports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    chapters = render_chapters(project, _data_uri)
    parts = []
    cover = project.cover_path()
    if cover:
        parts.append(f'<section class="cover-page"><img src="{_data_uri(cover)}" alt=""></section>')
    title = f'<section class="title-page page"><h1>{esc(m.title)}</h1>'
    if m.subtitle:
        title += f'<p class="subtitle">{esc(m.subtitle)}</p>'
    if m.author:
        title += f'<p class="author">{esc(m.author)}</p>'
    parts.append(title + "</section>")
    toc = "".join(
        f'<li><a href="#ch{i}">{esc(rc.toc_title(m.number_chapters))}</a></li>'
        for i, rc in enumerate(chapters, 1)
    )
    parts.append(f'<nav class="toc page"><h1>Contents</h1><ol>{toc}</ol></nav>')
    for i, rc in enumerate(chapters, 1):
        parts.append(
            f'<section class="page" id="ch{i}">\n{rc.heading_html(m.number_chapters)}\n{rc.body}\n</section>'
        )
    out = out_dir / f"{_slug(project)}.html"
    out.write_text(_html_page(m.title, m.language, BOOK_CSS + PRINT_CSS, "\n".join(parts)), "utf-8")
    return out


def build_website(project: Project, out_dir: Path | None = None) -> Path:
    """Returns a .zip of the site; the unzipped folder is left next to it."""
    m = project.meta
    out_dir = out_dir or project.exports_dir
    site = out_dir / f"{_slug(project)}-site"
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    chapters = render_chapters(project, lambda path: asset_href(project, path))
    _copy_images(project, chapters, site)
    css_link = '<link rel="stylesheet" href="style.css">\n'
    (site / "style.css").write_text(BOOK_CSS + SITE_CSS, "utf-8")

    cover_html = ""
    og = f'<meta property="og:title" content="{esc(m.title)}">\n'
    if m.description:
        og += f'<meta name="description" content="{esc(m.description[:300])}">\n'
        og += f'<meta property="og:description" content="{esc(m.description[:300])}">\n'
    cover = project.cover_path()
    if cover:
        shutil.copy(cover, site / cover.name)
        cover_html = f'<img src="{cover.name}" alt="Cover of {esc(m.title)}">'
        og += f'<meta property="og:image" content="{cover.name}">\n'

    pages = [f"{i:02d}-{slugify(rc.chapter.title, 'chapter')}.html" for i, rc in enumerate(chapters, 1)]
    toc = "\n".join(
        f'<li><a href="{p}">{esc(rc.toc_title(m.number_chapters))}</a></li>'
        for p, rc in zip(pages, chapters)
    )
    blurb = "".join(f"<p>{esc(par)}</p>" for par in m.description.split("\n\n") if par.strip())
    index_body = (
        f'<header class="book-head">{cover_html}<h1>{esc(m.title)}</h1>'
        + (f'<p class="subtitle">{esc(m.subtitle)}</p>' if m.subtitle else "")
        + (f'<p class="byline">by {esc(m.author)}</p>' if m.author else "")
        + f'</header>\n<div class="blurb">{blurb}</div>\n'
        f'<nav class="toc"><h2>Contents</h2><ol>{toc}</ol></nav>'
    )
    (site / "index.html").write_text(
        _html_page(m.title, m.language, "", index_body, css_link + og), "utf-8"
    )

    for i, (page, rc) in enumerate(zip(pages, chapters)):
        prev = f'<a href="{pages[i - 1]}">← Previous</a>' if i > 0 else "<span></span>"
        nxt = f'<a href="{pages[i + 1]}">Next →</a>' if i + 1 < len(pages) else "<span></span>"
        nav = f'<nav class="site-nav">{prev}<a href="index.html">Contents</a>{nxt}</nav>'
        body = f"{nav}\n{rc.heading_html(m.number_chapters)}\n{rc.body}\n{nav}"
        (site / page).write_text(
            _html_page(f"{rc.chapter.title} — {m.title}", m.language, "", body, css_link), "utf-8"
        )

    return _zip_dir(site)


def build_serial_pack(project: Project, out_dir: Path | None = None) -> Path:
    m = project.meta
    out_dir = out_dir or project.exports_dir
    pack = out_dir / f"{_slug(project)}-serial"
    if pack.exists():
        shutil.rmtree(pack)
    pack.mkdir(parents=True)
    chapters = render_chapters(project, lambda path: asset_href(project, path))
    _copy_images(project, chapters, pack)
    for i, rc in enumerate(chapters, 1):
        stem = f"{i:02d}-{slugify(rc.chapter.title, 'chapter')}"
        title = rc.toc_title(m.number_chapters)
        (pack / f"{stem}.html").write_text(f"<h1>{esc(title)}</h1>\n{rc.body}\n", "utf-8")
        text = project.read_chapter(rc.chapter.file).strip().replace("../assets/", "assets/")
        (pack / f"{stem}.md").write_text(f"# {title}\n\n{text}\n", "utf-8")
    readme = (
        f"{m.title} — serial pack\n\n"
        "One file per chapter, in reading order.\n"
        "- .html: open in a browser, select all, copy, and paste into the platform's rich editor\n"
        "  (Royal Road, Wattpad, AO3 'Rich Text', Substack, Medium) to keep italics and scene breaks.\n"
        "- .md: for platforms that accept Markdown.\n"
        + (
            "\nPictures are in assets/. Pasted text can't bring them along: upload each one with\n"
            "the platform's image button where it appears in the chapter.\n"
            if any(rc.images for rc in chapters)
            else ""
        )
    )
    (pack / "README.txt").write_text(readme, "utf-8")
    return _zip_dir(pack)


def build_markdown(project: Project, out_dir: Path | None = None) -> Path:
    m = project.meta
    out_dir = out_dir or project.exports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    head = [
        "---",
        f"title: {_yaml(m.title)}",
        *([f"subtitle: {_yaml(m.subtitle)}"] if m.subtitle else []),
        f"author: {_yaml(m.author)}",
        f"lang: {m.language}",
        *([f"rights: {_yaml(m.rights)}"] if m.rights else []),
        "---",
        "",
    ]
    body = []
    for ch, num in project.numbered():
        title = f"{num}. {ch.title}" if (m.number_chapters and num) else ch.title
        attrs = "" if ch.kind == "chapter" else " {.unnumbered}"
        body.append(f"# {title}{attrs}\n\n{project.read_chapter(ch.file).strip()}\n")
    out = out_dir / f"{_slug(project)}.md"
    out.write_text("\n".join(head) + "\n".join(body), "utf-8")
    return out


def _data_uri(path: Path) -> str:
    return f"data:{IMAGE_TYPES[path.suffix.lower()]};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _copy_images(project: Project, chapters: list[RenderedChapter], folder: Path) -> None:
    for path in {p for rc in chapters for p in rc.images}:
        dest = folder / unquote(asset_href(project, path))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, dest)


def _yaml(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _zip_dir(folder: Path) -> Path:
    out = folder.with_suffix(".zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(folder.parent))
    return out
