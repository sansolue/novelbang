"""Print-ready PDF of the book's interior, for a paperback (KDP, IngramSpark, Lulu) or to read.

The page is the trim size from Book details (6×9 in by default). It has a title page,
then a copyright page, then the front matter, chapters and back matter, each starting
on a right-hand page. Running heads carry the author's name on left-hand pages and the
title on right-hand ones. They're left off chapter openings, which show only the page
number. The text is set justified in Crimson Text, with indented paragraphs, scene
breaks, pictures with captions, and each chapter's footnotes at its end.

Characters the bundled font lacks (Greek, Cyrillic…) are taken from a system font when
one has them.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from fpdf import FPDF

from ..project import TRIM_SIZES, Project, ProjectError, count_words, slugify
from .docx import image_size
from .render import render_chapters

FONT_DIR = Path(__file__).parent / "fonts"
FONT = "Crimson"
MARGIN = 0.75  # inches, all sides: enough inside margin for up to ~700 pages on KDP
HEAD_GAP = 0.32  # running head / page number distance from the text block
SYSTEM_FALLBACKS = (
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/times.ttf",
    "C:/Windows/Fonts/cambria.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf",
)
log = logging.getLogger("novelbang")
logging.getLogger("fpdf").setLevel(logging.WARNING)
logging.getLogger("fontTools").setLevel(logging.WARNING)


class BookPDF(FPDF):
    def __init__(self, width: float, height: float, verso_head: str, recto_head: str):
        super().__init__(unit="in", format=(width, height))
        self.verso_head, self.recto_head = verso_head, recto_head
        self.next_kind = "body"
        self.kinds: dict[int, str] = {}  # page → "plain" (nothing), "opening" (number only), "body"
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(True, MARGIN)
        for style, name in (("", "Regular"), ("I", "Italic"), ("B", "Bold"), ("BI", "BoldItalic")):
            self.add_font(FONT, style, str(FONT_DIR / f"CrimsonText-{name}.ttf"))

    def new_page(self, kind: str = "body", recto: bool = False) -> None:
        """A new page; `recto`: make it a right-hand (odd) page, leaving a blank one before if needed."""
        if recto and (self.page_no() + 1) % 2 == 0:
            self.next_kind = "plain"
            self.add_page()
        self.next_kind = kind
        self.add_page()

    def header(self) -> None:
        kind, self.next_kind = self.next_kind, "body"  # pages the text flows onto get the full furniture
        self.kinds[self.page_no()] = kind
        if kind != "body":
            return
        text = self.recto_head if self.page_no() % 2 else self.verso_head
        style = "" if self.page_no() % 2 else "I"
        self.set_font(FONT, style, 8.5)
        self.set_char_spacing(0.6 if not style else 0)
        self.set_xy(MARGIN, MARGIN - HEAD_GAP - 0.1)
        self.cell(self.w - 2 * MARGIN, 0.14, text.upper() if not style else text, align="C")
        self.set_char_spacing(0)
        self.set_xy(MARGIN, MARGIN)

    def footer(self) -> None:
        if self.kinds.get(self.page_no(), "body") == "plain":
            return
        self.set_font(FONT, "", 9)
        self.set_xy(MARGIN, self.h - MARGIN + HEAD_GAP - 0.04)
        self.cell(self.w - 2 * MARGIN, 0.14, str(self.page_no()), align="C")


class _Setter:
    """Sets a chapter's XHTML (from render.py) as justified book text."""

    def __init__(self, pdf: BookPDF, size: float):
        self.pdf, self.size = pdf, size
        self.indent = 0.22
        self.centred = False
        self.notes: list[ET.Element] = []
        self.note_ids: dict[str, int] = {}

    def inline(self, par, el: ET.Element, style: str = "") -> None:
        def write(text: str) -> None:
            text = re.sub(r"\s*\n\s*", " ", text)  # a line break in Markdown is just a wrapped line
            if text:
                self.pdf.set_font(FONT, style, self.size)
                par.write(text)

        write(el.text or "")
        for child in el:
            tag = child.tag
            if tag == "sup" and (a := child.find("a")) is not None and a.get("class") == "footnote-ref":
                ref = a.get("href", "").lstrip("#")
                n = self.note_ids.setdefault(ref, len(self.note_ids) + 1)
                self.pdf.set_font(FONT, style, self.size)
                self.pdf.char_vpos = "SUP"
                par.write(str(n))
                self.pdf.char_vpos = "LINE"
            elif tag == "br":
                par.write("\n")
            elif tag == "a" and child.get("class") == "footnote-backref":
                pass
            elif tag != "img":
                extra = ("I" if tag in ("em", "i") else "") + ("B" if tag in ("strong", "b") else "")
                merged = "".join(sorted(set(style + extra), key="BI".index))
                self.inline(par, child, merged)
            write(child.tail or "")

    def chapter(self, root: ET.Element, cols) -> None:
        first = True
        lh = 1.38
        line = self.size / 72 * lh

        def para(el: ET.Element, *, style: str = "", align: str = "J", indent: float | None = None,
                 left: float = 0, top: float = 0, bottom: float = 0, bullet: str = "", size: float | None = None) -> None:
            nonlocal first
            first_indent = 0 if first or indent == 0 else (indent if indent is not None else self.indent)
            with cols.paragraph(text_align=align, line_height=lh, first_line_indent=first_indent, indent=left,
                                top_margin=top, bottom_margin=bottom, bullet_string=bullet) as par:
                saved, self.size = self.size, size or self.size
                self.inline(par, el, style)
                self.size = saved
            first = False

        for el in root:
            tag, cls = el.tag, el.get("class", "")
            if tag == "div" and "footnote" in cls:
                self.notes = [li for li in el.iter("li") if li.get("id", "").startswith("fn:")]
            elif tag == "p" and "scene-break" in cls:
                scene = ET.Element("p")
                scene.text = "*   *   *"
                para(scene, align="C", indent=0, top=line * 0.6, bottom=line * 0.6)
                first = True
            elif tag == "p":
                para(el, align="C", indent=0, style="I") if self.centred else para(el)
            elif tag in ("h2", "h3", "h4", "h5", "h6"):
                para(el, style="I", align="C", indent=0, top=line, bottom=line * 0.4)
                first = True
            elif tag == "blockquote":
                for i, p in enumerate(el.iter("p")):
                    para(p, indent=0, left=self.indent * 2, top=line * 0.5 if i == 0 else 0, size=self.size * 0.95)
                first = True
            elif tag in ("ul", "ol"):
                for i, li in enumerate(el.findall("li"), 1):
                    target = next((p for p in li if p.tag == "p"), li)
                    para(target, align="L", indent=0, left=self.indent * 1.5, bullet=f"{i}." if tag == "ol" else "•")
                first = True
            elif tag == "figure":
                self.figure(el, cols, line)
                first = True
            else:
                para(el)

        if self.notes:
            heading = ET.Element("p")
            heading.text = "Notes"
            para(heading, style="I", align="C", indent=0, top=line * 1.5, bottom=line * 0.3, size=self.size * 0.9)
            for ref, n in sorted(self.note_ids.items(), key=lambda kv: kv[1]):
                li = next((x for x in self.notes if x.get("id") == ref), None)
                if li is None:
                    continue
                target = next((p for p in li if p.tag == "p"), li)
                number = ET.Element("p")
                number.text = f"{n}. "
                number.append(target)  # a copy of the note's text after its number
                para(number, align="J", indent=0, size=self.size * 0.85)

    def figure(self, fig: ET.Element, cols, line: float) -> None:
        img = fig.find("img")
        path = Path(img.get("data-path", "")) if img is not None else None
        size = image_size(path) if path and path.is_file() else None
        if size:
            w_px, h_px = size
            text_w = self.pdf.w - 2 * MARGIN
            max_h = (self.pdf.h - 2 * MARGIN) * 0.6
            width = min(text_w, w_px / 150, max_h * w_px / h_px)  # at least 150 dpi on paper
            cols.image(str(path), align="C", width=width, height=width * h_px / w_px,
                       top_margin=line * 0.5, bottom_margin=line * 0.3, alt_text=img.get("alt") or None)
        if (cap := fig.find("figcaption")) is not None:
            with cols.paragraph(text_align="C", line_height=1.3, bottom_margin=line * 0.5) as par:
                saved, self.size = self.size, self.size * 0.85
                self.inline(par, cap, "I")
                self.size = saved


def _fallback_fonts(pdf: BookPDF, text: str) -> None:
    """Use a system font for characters Crimson Text doesn't have, or say which can't be set."""
    from fontTools.ttLib import TTFont

    have = set(TTFont(str(FONT_DIR / "CrimsonText-Regular.ttf")).getBestCmap())
    missing = {ch for ch in set(text) if ord(ch) not in have and not ch.isspace() and ord(ch) >= 32}
    if not missing:
        return
    for i, path in enumerate(p for p in SYSTEM_FALLBACKS if Path(p).is_file()):
        try:
            font = TTFont(path, fontNumber=0)
            cmap = set(font.getBestCmap())
        except Exception:
            continue
        if {ord(c) for c in missing} <= cmap:
            pdf.add_font(f"Fallback{i}", "", path)
            pdf.set_fallback_fonts([f"Fallback{i}"], exact_match=False)
            log.info("PDF: using %s for %d characters Crimson Text lacks", Path(path).name, len(missing))
            return
    sample = "".join(sorted(missing)[:12])
    raise ProjectError(
        f"The PDF's font can't print some of the book's characters ({sample}). "
        "The EPUB and print-ready HTML exports can, using your reader's or browser's fonts."
    )


def build_pdf(project: Project, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or project.exports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    m = project.meta
    title = m.title.strip() or "Untitled"
    author = m.author.strip()
    width, height = TRIM_SIZES.get(m.trim_size, TRIM_SIZES["6x9"])
    size = 11 if width >= 6 else 10.5

    chapters = render_chapters(project, image_src=lambda path: str(path))
    pdf = BookPDF(width, height, author or title, title)
    pdf.set_title(title)
    pdf.set_author(author)
    pdf.set_lang(m.language or "en")
    pdf.set_creator("NovelBang")
    all_text = " ".join([title, m.subtitle, author, m.rights, m.publisher] + [c.chapter.title + c.body for c in chapters])
    _fallback_fonts(pdf, re.sub(r"<[^>]+>", " ", all_text))

    # Title page.
    pdf.new_page("plain")
    pdf.set_y(height * 0.3)
    pdf.set_font(FONT, "", 26)
    pdf.multi_cell(0, 0.42, title, align="C")
    if m.subtitle.strip():
        pdf.ln(0.1)
        pdf.set_font(FONT, "I", 14)
        pdf.multi_cell(0, 0.26, m.subtitle.strip(), align="C")
    if author:
        pdf.set_y(height * 0.62)
        pdf.set_font(FONT, "", 15)
        pdf.multi_cell(0, 0.26, author, align="C")
    if m.publisher.strip():
        pdf.set_y(height - MARGIN - 0.3)
        pdf.set_font(FONT, "", 10)
        pdf.cell(0, 0.2, m.publisher.strip(), align="C")

    # Copyright page, its text at the foot.
    lines = [m.rights.strip() or (f"© {date.today().year} {author}. All rights reserved." if author else "")]
    if m.isbn.strip():
        lines.append(f"ISBN {m.isbn.strip()}")
    if m.publisher.strip():
        lines.append(f"Published by {m.publisher.strip()}")
    lines = [l for l in lines if l]
    pdf.new_page("plain")
    pdf.set_font(FONT, "", 8.5)
    pdf.set_y(height - MARGIN - 0.2 * len(lines) - 0.1)
    for l in lines:
        pdf.multi_cell(0, 0.2, l, align="L")

    for rc in chapters:
        pdf.new_page("opening", recto=True)
        pdf.set_y(height * 0.24)
        if m.number_chapters and rc.number is not None:
            pdf.set_font(FONT, "", 9)
            pdf.set_char_spacing(1.5)
            pdf.cell(0, 0.2, rc.label.upper(), align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.set_char_spacing(0)
            pdf.ln(0.08)
        pdf.set_font(FONT, "", 19)
        pdf.multi_cell(0, 0.32, rc.chapter.title, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.45)
        root = ET.fromstring("<div>" + re.sub(r'src="([^"]*)"', r'src="" data-path="\1"', rc.body) + "</div>")
        setter = _Setter(pdf, size)
        # Short front matter (a dedication, an epigraph) sits centred rather than set as prose.
        setter.centred = rc.chapter.kind == "front" and count_words(re.sub(r"<[^>]+>", " ", rc.body)) < 80
        with pdf.text_columns(text_align="J", line_height=1.38) as cols:
            setter.chapter(root, cols)

    out = out_dir / f"{slugify(title, 'book')}-print.pdf"
    tmp = out.with_name(out.name + ".tmp")
    pdf.output(str(tmp))
    tmp.replace(out)
    return out
