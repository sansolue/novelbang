"""EPUB 3 writer (with an EPUB 2 NCX for older readers).

The output is accepted by Kindle Direct Publishing, Kobo, Apple Books,
Google Play Books and Draft2Digital.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

from ..project import Project, slugify
from .render import BOOK_CSS, RenderedChapter, esc, render_chapters

XHTML_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">
<head>
<meta charset="UTF-8" />
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css" />
</head>
<body{body_attrs}>
"""
XHTML_FOOT = "\n</body>\n</html>\n"


def _page(lang: str, title: str, body: str, epub_type: str = "") -> str:
    attrs = f' epub:type="{epub_type}"' if epub_type else ""
    return XHTML_HEAD.format(lang=esc(lang), title=esc(title), body_attrs=attrs) + body + XHTML_FOOT


def build_epub(project: Project, out_dir: Path | None = None) -> Path:
    m = project.meta
    lang = m.language or "en"
    out_dir = out_dir or project.exports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{slugify(m.title, 'book')}.epub"

    chapters = render_chapters(project)
    cover = project.cover_path()
    # (id, href, media-type, properties, in_spine)
    manifest: list[tuple[str, str, str, str, bool]] = []
    files: dict[str, str | bytes] = {}

    if cover:
        ext = cover.suffix.lower().lstrip(".")
        mime = "image/png" if ext == "png" else "image/jpeg"
        files[f"cover.{ext}"] = cover.read_bytes()
        manifest.append(("cover-image", f"cover.{ext}", mime, "cover-image", False))
        files["cover.xhtml"] = _page(
            lang,
            "Cover",
            f'<div class="cover"><img src="cover.{ext}" alt="{esc(m.title)}" /></div>',
            "cover",
        )
        manifest.append(("cover", "cover.xhtml", "application/xhtml+xml", "", True))

    title_body = f'<section class="title-page" epub:type="titlepage">\n<h1>{esc(m.title)}</h1>\n'
    if m.subtitle:
        title_body += f'<p class="subtitle">{esc(m.subtitle)}</p>\n'
    if m.author:
        title_body += f'<p class="author">{esc(m.author)}</p>\n'
    title_body += "</section>"
    files["title.xhtml"] = _page(lang, m.title, title_body)
    manifest.append(("title", "title.xhtml", "application/xhtml+xml", "", True))

    files["copyright.xhtml"] = _page(lang, "Copyright", _copyright_html(project), "copyright-page")
    manifest.append(("copyright", "copyright.xhtml", "application/xhtml+xml", "", True))

    files["nav.xhtml"] = _nav(project, chapters, lang)
    manifest.append(("nav", "nav.xhtml", "application/xhtml+xml", "nav", True))

    for i, rc in enumerate(chapters, 1):
        href = f"ch{i:03d}.xhtml"
        epub_type = {"front": "frontmatter", "back": "backmatter"}.get(rc.chapter.kind, "bodymatter chapter")
        body = f'<section epub:type="{epub_type}">\n{rc.heading_html(m.number_chapters)}\n{rc.body}\n</section>'
        files[href] = _page(lang, rc.chapter.title, body)
        manifest.append((f"ch{i:03d}", href, "application/xhtml+xml", "", True))

    files["style.css"] = BOOK_CSS
    manifest.append(("css", "style.css", "text/css", "", False))
    files["toc.ncx"] = _ncx(project, chapters)
    manifest.append(("ncx", "toc.ncx", "application/x-dtbncx+xml", "", False))
    files["content.opf"] = _opf(project, manifest, bool(cover))

    tmp = out.with_suffix(".epub.tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        # mimetype must be first and stored uncompressed.
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in files.items():
            z.writestr(f"OEBPS/{name}", data, compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(out)
    return out


CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _copyright_html(project: Project) -> str:
    m = project.meta
    lines = [f"<p><em>{esc(m.title)}</em></p>"]
    lines.append(f"<p>{esc(m.rights or f'© {datetime.now().year} {m.author}'.strip())}</p>")
    if m.isbn:
        lines.append(f"<p>ISBN: {esc(m.isbn)}</p>")
    if m.publisher:
        lines.append(f"<p>Published by {esc(m.publisher)}</p>")
    lines.append(
        "<p>This is a work of fiction. Names, characters, places and incidents are products "
        "of the author’s imagination or are used fictitiously.</p>"
    )
    return '<section class="copyright">\n' + "\n".join(lines) + "\n</section>"


def _nav(project: Project, chapters: list[RenderedChapter], lang: str) -> str:
    n = project.meta.number_chapters
    items = "\n".join(
        f'<li><a href="ch{i:03d}.xhtml">{esc(rc.toc_title(n))}</a></li>'
        for i, rc in enumerate(chapters, 1)
    )
    landmarks = '<li><a epub:type="titlepage" href="title.xhtml">Title Page</a></li>'
    if chapters:
        first_body = next(
            (i for i, rc in enumerate(chapters, 1) if rc.chapter.kind == "chapter"), 1
        )
        landmarks += f'\n<li><a epub:type="bodymatter" href="ch{first_body:03d}.xhtml">Start of Content</a></li>'
    body = (
        f'<nav epub:type="toc" id="toc">\n<h1>Contents</h1>\n<ol>\n{items}\n</ol>\n</nav>\n'
        f'<nav epub:type="landmarks" hidden="hidden">\n<ol>\n{landmarks}\n</ol>\n</nav>'
    )
    return _page(lang, "Contents", body)


def _ncx(project: Project, chapters: list[RenderedChapter]) -> str:
    n = project.meta.number_chapters
    points = "\n".join(
        f'  <navPoint id="np{i}" playOrder="{i}"><navLabel><text>{esc(rc.toc_title(n))}</text></navLabel>'
        f'<content src="ch{i:03d}.xhtml"/></navPoint>'
        for i, rc in enumerate(chapters, 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{esc(project.meta.identifier)}"/></head>
<docTitle><text>{esc(project.meta.title)}</text></docTitle>
<navMap>
{points}
</navMap>
</ncx>
"""


def _opf(project: Project, manifest: list[tuple[str, str, str, str, bool]], has_cover: bool) -> str:
    m = project.meta
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = [
        f'<dc:identifier id="bookid">{esc(m.identifier)}</dc:identifier>',
        f"<dc:title>{esc(m.title)}</dc:title>",
        f"<dc:language>{esc(m.language or 'en')}</dc:language>",
        f'<meta property="dcterms:modified">{modified}</meta>',
    ]
    if m.author:
        meta.append(f'<dc:creator id="author">{esc(m.author)}</dc:creator>')
        meta.append('<meta refines="#author" property="role" scheme="marc:relators">aut</meta>')
    if m.subtitle:
        meta.append('<meta property="title-type" refines="#subtitle">subtitle</meta>')
        meta.append(f'<dc:title id="subtitle">{esc(m.subtitle)}</dc:title>')
    for tag, value in (
        ("description", m.description),
        ("publisher", m.publisher),
        ("rights", m.rights),
    ):
        if value:
            meta.append(f"<dc:{tag}>{esc(value)}</dc:{tag}>")
    for subject in filter(None, [m.genre, *[k.strip() for k in m.keywords.split(",")]]):
        meta.append(f"<dc:subject>{esc(subject)}</dc:subject>")
    if m.isbn:
        meta.append(f"<dc:identifier>urn:isbn:{esc(m.isbn.replace('-', '').replace(' ', ''))}</dc:identifier>")
    if m.series:
        meta.append(f'<meta property="belongs-to-collection" id="series">{esc(m.series)}</meta>')
        meta.append('<meta refines="#series" property="collection-type">series</meta>')
        if m.series_index:
            meta.append(f'<meta refines="#series" property="group-position">{esc(str(m.series_index))}</meta>')
    if has_cover:
        meta.append('<meta name="cover" content="cover-image"/>')

    items = "\n    ".join(
        f'<item id="{i}" href="{h}" media-type="{t}"' + (f' properties="{p}"' if p else "") + "/>"
        for i, h, t, p, _ in manifest
    )
    spine = "\n    ".join(f'<itemref idref="{i}"/>' for i, _, _, _, s in manifest if s)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{esc(m.language or 'en')}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    {chr(10).join('    ' + x for x in meta).lstrip()}
  </metadata>
  <manifest>
    {items}
  </manifest>
  <spine toc="ncx">
    {spine}
  </spine>
</package>
"""
